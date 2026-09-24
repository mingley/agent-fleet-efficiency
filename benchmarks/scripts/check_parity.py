#!/usr/bin/env python3
"""P1 slice 4: upstream-vs-Rust parity suite (2->3 gate input).

Starts BOTH servers as subprocesses on free localhost ports (upstream
`.venv` python server + `./target/release/agent-execd`, same auth token),
runs an identical vector list against each, and diffs per vector:
HTTP status, error `class_path` on 511s, exit codes, and CONTENT lines.

Content comparison is trimmed non-blank line comparison: blank-line framing
counts legitimately differ between the PTY front ends (P0.5 design), so raw
bytes are never compared. Two further uniform framing normalizations apply
to every vector on both sides: lines equal to `^C` (PTY INTR-echo artifact,
seen after interrupt) and `SHELLPS1PREFIX` (prompt-marker leak in
create-session output) are dropped. `message` substrings are compared only
for documented-stable parts (exit-code numbers, class-name-adjacent nouns,
paths) -- never repr quoting (deviation 8: Rust `{:?}` vs Python `repr`).

Encoded documented deviations (crates/README.md, do not "rediscover"):
- handler-level validation failures (unknown `action_type`/`session_type`):
  upstream FastAPI answers 422, Rust answers 511 `builtins.ValueError`
  (README "Errors" paragraph; same 422->511 shape as deviation 5).
- deviation 1 (`merge_output_streams` concat vs interleave): the merge
  vector compares sorted content lines (order-insensitive).
- deviation 8 (execute `check` message quoting): message substrings only.

Stdlib only (urllib + json + friends). Usage (from repo root):
    .venv/bin/python benchmarks/scripts/check_parity.py

Exits 0 when every vector passes, 1 on any mismatch. Always terminates
both servers.
"""

import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile

AUTH_TOKEN = "parity-token-p1s4"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UPSTREAM_PY = os.path.join(REPO_ROOT, ".venv", "bin", "python")
RUST_BIN = os.path.join(REPO_ROOT, "target", "release", "agent-execd")
SESSION = "parity-sess"

# PTY-framing tokens dropped (uniformly, both sides) by _content_lines.
FRAMING_TOKENS = frozenset({"^C", "SHELLPS1PREFIX"})


def _pick_free_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _wait_for_server(base_url, timeout=60.0):
    deadline = time.monotonic() + timeout
    last = "not attempted"
    while time.monotonic() < deadline:
        try:
            req = urllib.request.Request(
                base_url + "/is_alive", headers={"X-API-Key": AUTH_TOKEN}
            )
            with urllib.request.urlopen(req, timeout=2) as resp:
                if resp.status == 200:
                    return
                last = f"status {resp.status}"
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}: {e.reason}"
        except Exception as e:  # noqa: BLE001 - probe until deadline
            last = f"{type(e).__name__}: {e}"
        time.sleep(0.2)
    raise RuntimeError(f"server at {base_url} not ready after {timeout}s ({last})")


def _content_lines(text):
    """Trimmed non-blank line comparison with PTY-framing normalization."""
    lines = []
    for raw in (text or "").splitlines():
        s = raw.strip()
        if not s or s in FRAMING_TOKENS:
            continue
        lines.append(s)
    return lines


class Server:
    def __init__(self, name, cmd, tmpdir):
        self.name = name
        self.cmd = cmd
        self.tmpdir = tmpdir
        self.proc = None
        self.base_url = None

    def start(self):
        port = _pick_free_port()
        self.base_url = f"http://127.0.0.1:{port}"
        log = open(os.path.join(self.tmpdir, f"{self.name}-server.log"), "w")
        self.proc = subprocess.Popen(
            self.cmd(port), stdout=log, stderr=subprocess.STDOUT
        )
        _wait_for_server(self.base_url)

    def stop(self):
        if self.proc is not None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=10)
            self.proc = None

    def request(self, method, path, body=None, headers=None, timeout=120.0):
        data = None
        hdrs = {"X-API-Key": AUTH_TOKEN}
        if headers:
            hdrs.update(headers)
        if body is not None and not isinstance(body, bytes):
            data = json.dumps(body).encode()
            hdrs.setdefault("Content-Type", "application/json")
        elif isinstance(body, bytes):
            data = body
        req = urllib.request.Request(
            self.base_url + path, data=data, headers=hdrs, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8", errors="backslashreplace")
                return resp.status, raw
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", errors="backslashreplace")
            return e.code, raw


def _multipart(file_bytes, filename, target_path, unzip):
    boundary = "BOUND" + uuid.uuid4().hex
    parts = [
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            f'filename="{filename}"\r\nContent-Type: application/octet-stream\r\n\r\n'
        ).encode()
        + file_bytes
        + b"\r\n",
        f'--{boundary}\r\nContent-Disposition: form-data; name="target_path"'
        f"\r\n\r\n{target_path}\r\n".encode(),
        f'--{boundary}\r\nContent-Disposition: form-data; name="unzip"'
        f"\r\n\r\n{unzip}\r\n".encode(),
        f"--{boundary}--\r\n".encode(),
    ]
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _zip_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("inner.txt", "unzipped-content\n")
    return buf.getvalue()


# --------------------------------------------------------------------------
# Vector list. Each vector: name + build(ctx) -> (method, path, body, headers)
# plus compare spec: text_keys (200 content keys), msg_contains (511 stable
# substrings, case-insensitive, required in BOTH messages), sort_lines
# (order-insensitive content, merge deviation), validation_422
# (upstream 422 == rust 511 ValueError, encoded deviation).
# --------------------------------------------------------------------------

def _get(path, key=None):
    def build(ctx):
        hdrs = {"X-API-Key": key} if key is not None else None
        return ("GET", path, None, hdrs)

    return build


def _post(path, body):
    def build(ctx):
        resolved = json.loads(json.dumps(body).replace("{tmp}", ctx["tmp"]))
        if isinstance(body, dict) and body.get("__raw__"):
            resolved = body["__raw__"]
        return ("POST", path, resolved, None)

    return build


VECTORS = []


def _v(name, build, **spec):
    spec.setdefault("text_keys", [])
    spec.setdefault("msg_contains", [])
    spec.setdefault("sort_lines", False)
    spec.setdefault("validation_422", False)
    spec.setdefault("normalize_tmp", False)
    VECTORS.append({"name": name, "build": build, **spec})


# -- health / auth -----------------------------------------------------------
_v("root", _get("/"), text_keys=["message"])
_v("is_alive", _get("/is_alive"))
_v("bad-key-401", _get("/is_alive", key="wrong-token"))

# -- create ------------------------------------------------------------------
_v("create-ok", _post("/create_session", {"session": SESSION, "session_type": "bash"}))
_v(
    "create-duplicate-511",
    _post("/create_session", {"session": SESSION, "session_type": "bash"}),
    msg_contains=[SESSION, "already exists"],
)
_v(
    "create-bad-type",
    _post("/create_session", {"session": "x", "session_type": "nope"}),
    validation_422=True,
)

# -- session commands (16; stateful, order matters) ---------------------------
RUN = "/run_in_session"


def _run(cmd, **kw):
    body = {"action_type": "bash", "command": cmd, "session": SESSION}
    body.update(kw)
    return _post(RUN, body)


_v("sess-true", _run("true"), text_keys=["output"])
_v("sess-echo", _run("echo hello"), text_keys=["output"])
# NOTE: bare `pwd` differs (upstream session inherits server cwd, Rust
# session starts in $HOME) -- compare a deterministic cd+pwd instead.
_v("sess-pwd", _run("cd /tmp && pwd"), text_keys=["output"])
_v("sess-env-set", _run("export FOO_PARITY=abc123"), text_keys=["output"])
_v("sess-env-get", _run("echo $FOO_PARITY"), text_keys=["output"])
# NOTE: sized at 1MiB-64 so total captured bytes stay under the Rust
# 1MiB session-output truncation cap ("[execd: output truncated ...]",
# hit at exactly 1MiB of content -- undocumented, reported, NOT encoded).
_v(
    "sess-big-output-1mib",
    _run("python3 -c \"import sys; sys.stdout.write('L'*1048512 + '\\n')\""),
    text_keys=["output"],
)
_v(
    "sess-false-raise-511",
    _run("false"),
    msg_contains=["false", "exit code", "1"],
)
_v("sess-false-silent", _run("false", check="silent"), text_keys=["output"])
_v("sess-false-ignore", _run("false", check="ignore"), text_keys=["output"])
_v("sess-multi", _run("echo line-a; echo line-b"), text_keys=["output"])
_v("sess-stderr", _run("echo to-stderr >&2; echo to-stdout"), text_keys=["output"])
_v("sess-exit3-silent", _run("(exit 3)", check="silent"), text_keys=["output"])
_v(
    "sess-timeout-511",
    _run("sleep 30", timeout=1.0),
    msg_contains=["imeout", "sleep 30"],
)
_v(
    "sess-interrupt",
    _post(RUN, {"action_type": "bash_interrupt", "session": SESSION}),
    text_keys=["output"],
)
_v("sess-recover", _run("echo recovered"), text_keys=["output"])
_v("sess-healthy", _run("false", check="silent"), text_keys=["output"])

# -- execute ------------------------------------------------------------------
_v(
    "exec-shell-ok",
    _post("/execute", {"command": "echo shell-ok", "shell": True}),
    text_keys=["stdout", "stderr"],
)
_v(
    "exec-argv-ok",
    _post("/execute", {"command": ["echo", "argv-ok"]}),
    text_keys=["stdout", "stderr"],
)
_v(
    "exec-check-fail-511",
    _post("/execute", {"command": ["false"], "check": True}),
    msg_contains=["false", "exit code 1", "stdout", "stderr"],
)
_v(
    "exec-check-ok",
    _post("/execute", {"command": ["true"], "check": True}),
    text_keys=["stdout", "stderr"],
)
_v(
    "exec-timeout-511",
    _post("/execute", {"command": "sleep 30", "shell": True, "timeout": 1.0}),
    msg_contains=["timeout", "1.0s"],
)
_v(
    "exec-merge",
    _post(
        "/execute",
        {"command": "echo out; echo err >&2", "shell": True, "merge_output_streams": True},
    ),
    text_keys=["stdout", "stderr"],
    sort_lines=True,  # deviation 1: concat vs interleave -> order-insensitive
)
_v(
    "exec-env",
    _post("/execute", {"command": ["printenv", "FOO_X"], "env": {"FOO_X": "bar"}}),
    text_keys=["stdout", "stderr"],
)
_v(
    "exec-cwd",
    _post("/execute", {"command": "pwd", "shell": True, "cwd": "{tmp}"}),
    text_keys=["stdout", "stderr"],
    normalize_tmp=True,  # per-side tmpdirs legitimately differ
)

# -- file round-trips ----------------------------------------------------------
_v("write-ok", _post("/write_file", {"path": "{tmp}/w.txt", "content": "hello parity\nsecond line\n"}))
_v("read-back", _post("/read_file", {"path": "{tmp}/w.txt"}), text_keys=["content"])
_v(
    "read-missing-511",
    _post("/read_file", {"path": "{tmp}/parity-missing.txt"}),
    msg_contains=["parity-missing", "No such file"],
)
_v(
    "read-strict-decode-511",
    _post("/read_file", {"path": "{tmp}/bad.bin"}),
    msg_contains=["utf-8"],
)
_v(
    "read-backslashreplace",
    _post("/read_file", {"path": "{tmp}/bad.bin", "errors": "backslashreplace"}),
    text_keys=["content"],
)


def _upload_move(ctx):
    body, ctype = _multipart(b"moved-content\n", "f.bin", f"{ctx['tmp']}/moved.bin", "false")
    return ("POST", "/upload", body, {"Content-Type": ctype})


def _upload_unzip(ctx):
    body, ctype = _multipart(_zip_bytes(), "a.zip", f"{ctx['tmp']}/unz", "true")
    return ("POST", "/upload", body, {"Content-Type": ctype})


_v("upload-move", _upload_move)
_v("upload-move-readback", _post("/read_file", {"path": "{tmp}/moved.bin"}), text_keys=["content"])
_v("upload-unzip", _upload_unzip)
_v("upload-unzip-readback", _post("/read_file", {"path": "{tmp}/unz/inner.txt"}), text_keys=["content"])

# -- teardown ------------------------------------------------------------------
_v("close-session-ok", _post("/close_session", {"session": SESSION, "session_type": "bash"}))
_v(
    "close-session-double-511",
    _post("/close_session", {"session": SESSION, "session_type": "bash"}),
    msg_contains=[SESSION, "does not exist"],
)
_v(
    "bad-action-type",
    _post(RUN, {"action_type": "nope", "command": "true", "session": SESSION}),
    validation_422=True,
)
_v("close-all", _post("/close", {}))


def _summarize(status, raw, tmp=""):
    """Reduce a raw response to the four compared signals."""
    try:
        body = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    exc = body.get("swerexception") if isinstance(body.get("swerexception"), dict) else None
    return {
        "status": status,
        "class_path": exc.get("class_path", "") if exc else "",
        "message": exc.get("message", "") if exc else "",
        "exit_code": body.get("exit_code", "<absent>"),
        "body": body,
        "tmp": tmp,
    }


def _scrub_tmp(text, tmp):
    for cand in (os.path.realpath(tmp), tmp):
        if cand:
            text = text.replace(cand, "{tmp}")
    return text


def _compare(vec, a, b):
    """Return None on parity, else a one-line diff snippet."""
    if vec["validation_422"]:
        # Encoded deviation: upstream FastAPI 422 vs Rust 511 ValueError.
        if not (a["status"] == 422 and b["status"] == 511):
            return f"status: upstream={a['status']} rust={b['status']} (want 422/511)"
        if b["class_path"] != "builtins.ValueError":
            return f"rust class_path={b['class_path']!r} (want builtins.ValueError)"
        return None
    if a["status"] != b["status"]:
        return f"status: upstream={a['status']} rust={b['status']}"
    if a["status"] == 511:
        if a["class_path"] != b["class_path"]:
            return f"class_path: upstream={a['class_path']!r} rust={b['class_path']!r}"
        for needle in vec["msg_contains"]:
            if needle.lower() not in a["message"].lower():
                return f"upstream message missing {needle!r}: {a['message'][:120]!r}"
            if needle.lower() not in b["message"].lower():
                return f"rust message missing {needle!r}: {b['message'][:120]!r}"
        return None
    # 200-path: exit codes (when present on both) + content lines.
    if a["exit_code"] != "<absent>" or b["exit_code"] != "<absent>":
        if a["exit_code"] != b["exit_code"]:
            return f"exit_code: upstream={a['exit_code']!r} rust={b['exit_code']!r}"
    for key in vec["text_keys"]:
        at, bt = a["body"].get(key, ""), b["body"].get(key, "")
        if vec["normalize_tmp"]:
            at, bt = _scrub_tmp(at, a["tmp"]), _scrub_tmp(bt, b["tmp"])
        al = _content_lines(at)
        bl = _content_lines(bt)
        if vec["sort_lines"]:
            al, bl = sorted(al), sorted(bl)
        if al != bl:
            def _snip(lines):
                s = json.dumps(lines)
                return s if len(s) <= 300 else s[:300] + f"... <{len(s)} chars>"

            return f"{key} lines differ: upstream={_snip(al)} rust={_snip(bl)}"
    return None


def main():
    if not os.path.exists(UPSTREAM_PY):
        print(f"missing upstream python: {UPSTREAM_PY}")
        return 2
    if not os.path.exists(RUST_BIN):
        print(f"missing rust binary: {RUST_BIN} -- refusing to build (P1 slice 4 boundary)")
        return 2

    workdir = tempfile.mkdtemp(prefix="parity-p1s4-")
    up_tmp = os.path.join(workdir, "upstream")
    rs_tmp = os.path.join(workdir, "rust")
    os.makedirs(up_tmp)
    os.makedirs(rs_tmp)
    # Distinct per-side fixtures (both servers share one filesystem).
    for d in (up_tmp, rs_tmp):
        with open(os.path.join(d, "bad.bin"), "wb") as f:
            f.write(b"\xff\xfe\x00bad")

    upstream = Server(
        "upstream",
        lambda port: [
            UPSTREAM_PY, "-m", "swerex.server", "--host", "127.0.0.1",
            "--port", str(port), "--auth-token", AUTH_TOKEN,
        ],
        up_tmp,
    )
    rust = Server(
        "rust",
        lambda port: [
            RUST_BIN, "--host", "127.0.0.1", "--port", str(port),
            "--auth-token", AUTH_TOKEN,
        ],
        rs_tmp,
    )
    print(f"vectors: {len(VECTORS)} (need >=30: {'OK' if len(VECTORS) >= 30 else 'SHORT'})")
    try:
        upstream.start()
        rust.start()
        print(f"upstream: {upstream.base_url} tmp={up_tmp}")
        print(f"rust:     {rust.base_url} tmp={rs_tmp}")

        results = []
        for vec in VECTORS:
            per = {}
            for srv in (upstream, rust):
                method, path, body, headers = vec["build"]({"tmp": srv.tmpdir})
                try:
                    status, raw = srv.request(method, path, body, headers)
                except Exception as e:  # noqa: BLE001 - transport failure is a result
                    status, raw = -1, json.dumps({"transport_error": f"{type(e).__name__}: {e}"})
                per[srv.name] = _summarize(status, raw, srv.tmpdir)
            results.append((vec, per["upstream"], per["rust"]))

        npass, nfail = 0, 0
        for vec, a, b in results:
            diff = _compare(vec, a, b)
            if diff is None:
                npass += 1
                print(f"PASS {vec['name']}")
            else:
                nfail += 1
                print(f"FAIL {vec['name']}: {diff}")
        print(f"parity: {npass} pass, {nfail} fail / {len(results)} vectors")
        return 1 if nfail else 0
    finally:
        upstream.stop()
        rust.stop()


if __name__ == "__main__":
    raise SystemExit(main())
