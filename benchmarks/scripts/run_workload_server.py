#!/usr/bin/env python3
"""Workload S: server/remote path microbenchmark (P0.5 hotpath items 1, 5, 6).

Starts a local `swerex-remote` server subprocess on a free localhost port,
drives N tiny remote commands via RemoteRuntime plus one ~10MiB upload,
and reports per-op latency and failures to stdout + JSONL in
benchmark-results/.

After the server subprocess is terminated and reaped, server resource
accounting is recorded: server_cpu_s (getrusage RUSAGE_CHILDREN cpu
delta across the run -- the server is the dominant child, so this is an
approximation, stored as server_cpu_s_approx) and server_peak_rss_bytes
(RUSAGE_CHILDREN ru_maxrss after wait, normalized macOS-bytes vs
Linux-KiB). Both appear in the JSONL summary and the stdout table line.

With --concurrent-sessions N (default 0 = off), after the sequential
tiny phase the workload creates N sessions, runs one `true` in each
concurrently via asyncio.gather (per-op remote_concurrent_true plus
total remote_concurrent_total), then closes all sessions.

Stdlib + swerex only: the RemoteRuntime client needs aiohttp (which
upstream remote.py already imports but does not declare), and the server
readiness probe uses stdlib urllib. The server subprocess inherits this
process's environment, so SWEREX_OPT_*=1 flags apply to both ends.

Usage (from repo root):
    .venv/bin/python benchmarks/scripts/run_workload_server.py --quick
    .venv-opt/bin/python benchmarks/scripts/run_workload_server.py --quick \\
        --server-python .venv-opt/bin/python
    SWEREX_OPT_REUSE_SESSION=1 SWEREX_OPT_BOUNDED_IDEMPOTENCY=1 \\
    SWEREX_OPT_STREAM_UPLOAD=1 .venv-opt/bin/python \\
        benchmarks/scripts/run_workload_server.py --quick \\
        --server-python .venv-opt/bin/python
    .venv/bin/python benchmarks/scripts/run_workload_server.py --quick \\
        --server-cmd './target/release/agent-execd --host 127.0.0.1 \\
        --port {port} --auth-token {token}' --label rust
"""

import argparse
import asyncio
import json
import os
import resource
import shlex

# Quiet swerex's rich DEBUG logging (read at swerex import time) unless the
# caller overrides it; keeps the latency tables readable.
os.environ.setdefault("SWE_REX_LOG_STREAM_LEVEL", "WARNING")

import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

AUTH_TOKEN = "bench-token-p05"
SERVER_FLAGS = (
    "SWEREX_OPT_SKIP_SYNTAX_CHECK",
    "SWEREX_OPT_SINGLE_SUBMIT",
    "SWEREX_OPT_NO_FIXED_SLEEP",
    "SWEREX_OPT_REUSE_SESSION",
    "SWEREX_OPT_BOUNDED_IDEMPOTENCY",
    "SWEREX_OPT_STREAM_UPLOAD",
)


def _percentile(sorted_vals, pct):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * pct / 100
    lo, hi = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    frac = k - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


class Recorder:
    def __init__(self):
        self.ops = []

    def add(self, op, wall_s, out_bytes=0, exit_code=0, ok=True, note=""):
        self.ops.append(
            {
                "op": op,
                "wall_s": wall_s,
                "out_bytes": out_bytes,
                "exit_code": exit_code,
                "ok": ok,
                "note": note,
            }
        )

    def summarize(self):
        by_op = {}
        for rec in self.ops:
            by_op.setdefault(rec["op"], []).append(rec["wall_s"])
        summary = {}
        for op, walls in by_op.items():
            walls.sort()
            summary[op] = {
                "n": len(walls),
                "mean_s": sum(walls) / len(walls),
                "p50_s": _percentile(walls, 50),
                "p95_s": _percentile(walls, 95),
                "p99_s": _percentile(walls, 99),
                "max_s": walls[-1],
            }
        return summary

    def failures(self):
        return [rec for rec in self.ops if not rec["ok"]]


def _cpu_seconds(who):
    r = resource.getrusage(who)
    return r.ru_utime + r.ru_stime


def _child_maxrss_bytes():
    # ru_maxrss is bytes on macOS, KiB on Linux.
    scale = 1 if sys.platform == "darwin" else 1024
    return resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * scale


def _pick_free_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _wait_for_server(base_url, timeout=30.0):
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


def _write_upload_source(path, size_bytes):
    chunk = b"x" * (1024 * 1024)
    with open(path, "wb") as f:
        remaining = size_bytes
        while remaining > 0:
            piece = chunk[: min(len(chunk), remaining)]
            f.write(piece)
            remaining -= len(piece)


async def _timed_remote(rec, op, coro_factory, **fields):
    t0 = time.perf_counter()
    try:
        result = await coro_factory()
    except Exception as e:  # noqa: BLE001 - record failure, keep going
        rec.add(op, time.perf_counter() - t0, ok=False, note=f"{type(e).__name__}: {e}")
        return None
    rec.add(op, time.perf_counter() - t0, **fields)
    return result


async def main_async(args):
    from swerex.runtime.abstract import (
        BashAction,
        CloseBashSessionRequest,
        Command,
        CreateBashSessionRequest,
        UploadRequest,
    )
    from swerex.runtime.remote import RemoteRuntime

    import swerex

    n_tiny = 5 if args.quick else args.n_tiny
    upload_bytes = args.upload_mib * 1024 * 1024
    server_python = os.path.abspath(args.server_python)
    parent = os.path.dirname(server_python)
    label = args.label or (
        os.path.basename(os.path.dirname(parent))
        if os.path.basename(parent) == "bin"
        else os.path.basename(parent)
    )
    flags = {name: os.environ.get(name, "") for name in SERVER_FLAGS}

    port = _pick_free_port()
    base_url = f"http://127.0.0.1:{port}"
    server_log = tempfile.NamedTemporaryFile(
        prefix="swerex-server-", suffix=".log", delete=False
    )
    server_log.close()
    if args.server_cmd:
        server_cmd = [
            tok.replace("{port}", str(port)).replace("{token}", AUTH_TOKEN)
            for tok in shlex.split(args.server_cmd)
        ]
        server_python = os.path.abspath(server_cmd[0])
    else:
        server_cmd = [
            server_python,
            "-m",
            "swerex.server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--auth-token",
            AUTH_TOKEN,
        ]
    cpu_child_0 = _cpu_seconds(resource.RUSAGE_CHILDREN)
    proc = subprocess.Popen(
        server_cmd,
        stdout=open(server_log.name, "w"),
        stderr=subprocess.STDOUT,
    )
    t_start = time.perf_counter()
    rec = Recorder()
    rt = None
    workdir = tempfile.mkdtemp(prefix="swerex-workload-s-")
    rc = 0
    try:
        _wait_for_server(base_url, timeout=args.server_timeout)
        rec.add("server_startup", time.perf_counter() - t_start, note=f"port={port}")

        rt = RemoteRuntime(host="http://127.0.0.1", port=port, auth_token=AUTH_TOKEN)
        session = "bench-srv"

        ok = await _timed_remote(
            rec, "session_create", lambda: rt.create_session(CreateBashSessionRequest(session=session))
        )
        if ok is None:
            raise RuntimeError("session_create failed; aborting workload")

        for _ in range(n_tiny):
            async def _run():
                obs = await rt.run_in_session(BashAction(command="true", session=session))
                return obs

            obs = await _timed_remote(rec, "remote_tiny_true", _run)
            if obs is not None:
                rec.ops[-1]["exit_code"] = obs.exit_code or 0
                rec.ops[-1]["out_bytes"] = len(obs.output or "")
                if obs.exit_code != 0:
                    rec.ops[-1]["ok"] = False
                    rec.ops[-1]["note"] = f"exit_code={obs.exit_code}"

        k = args.concurrent_sessions
        if k > 0:
            cc_names = [f"bench-cc-{i}" for i in range(k)]
            for name in cc_names:
                t_cc = time.perf_counter()
                try:
                    await rt.create_session(CreateBashSessionRequest(session=name))
                except Exception as e:  # noqa: BLE001 - record failure, keep going
                    rec.add(
                        "remote_concurrent_create",
                        time.perf_counter() - t_cc,
                        ok=False,
                        note=f"{type(e).__name__}: {e}",
                    )

            async def _cc_one(name):
                t1 = time.perf_counter()
                try:
                    obs = await rt.run_in_session(BashAction(command="true", session=name))
                except Exception as e:  # noqa: BLE001 - per-op failure
                    return (time.perf_counter() - t1, 0, 0, False, f"{type(e).__name__}: {e}")
                code = obs.exit_code or 0
                ok = code == 0
                return (
                    time.perf_counter() - t1,
                    code,
                    len(obs.output or ""),
                    ok,
                    "" if ok else f"exit_code={code}",
                )

            t_cc0 = time.perf_counter()
            cc_results = await asyncio.gather(*(_cc_one(n) for n in cc_names))
            rec.add("remote_concurrent_total", time.perf_counter() - t_cc0, note=f"k={k}")
            for wall, code, out_len, ok, note in cc_results:
                rec.add(
                    "remote_concurrent_true",
                    wall,
                    out_bytes=out_len,
                    exit_code=code,
                    ok=ok,
                    note=note,
                )
            for name in cc_names:
                try:
                    await rt.close_session(CloseBashSessionRequest(session=name))
                except Exception as e:  # noqa: BLE001 - record failure, keep going
                    rec.add("remote_concurrent_close", 0.0, ok=False, note=f"{type(e).__name__}: {e}")

        src_path = os.path.join(workdir, "upload-src.bin")
        _write_upload_source(src_path, upload_bytes)
        target_path = os.path.join(workdir, "uploaded.bin")
        up = await _timed_remote(
            rec,
            f"remote_upload_{args.upload_mib}mib",
            lambda: rt.upload(UploadRequest(source_path=src_path, target_path=target_path)),
            out_bytes=upload_bytes,
        )
        if up is not None:
            resp = await _timed_remote(
                rec,
                "remote_verify_upload",
                lambda: rt.execute(Command(command=["wc", "-c", target_path])),
            )
            if resp is not None:
                try:
                    got = int(resp.stdout.strip().split()[0])
                except (ValueError, IndexError):
                    got = -1
                rec.ops[-1]["exit_code"] = resp.exit_code or 0
                if got != upload_bytes:
                    rec.ops[-1]["ok"] = False
                    rec.ops[-1]["note"] = f"size mismatch: got {got}, want {upload_bytes}"

        await _timed_remote(
            rec, "session_close", lambda: rt.close_session(CloseBashSessionRequest(session=session))
        )
    except Exception as e:  # noqa: BLE001 - top-level guard, still report
        rec.add("workload", time.perf_counter() - t_start, ok=False, note=f"{type(e).__name__}: {e}")
    finally:
        if rt is not None:
            try:
                await rt.close()
            except Exception as e:  # noqa: BLE001 - best effort
                rec.add("runtime_close", 0.0, ok=False, note=f"{type(e).__name__}: {e}")
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)

    wall_total = time.perf_counter() - t_start
    # Server is reaped above, so RUSAGE_CHILDREN now includes it (it is the
    # dominant child; other reaped children add noise -- hence approx).
    server_cpu_s = _cpu_seconds(resource.RUSAGE_CHILDREN) - cpu_child_0
    server_peak_rss_bytes = _child_maxrss_bytes()
    failures = rec.failures()
    summary = {
        "type": "summary",
        "workload": "server",
        "label": label,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "client_python": sys.executable,
        "server_python": server_python,
        "swerex_file": getattr(swerex, "__file__", ""),
        "swerex_version": getattr(swerex, "__version__", ""),
        "flags": flags,
        "n_tiny": n_tiny,
        "upload_mib": args.upload_mib,
        "concurrent_sessions": args.concurrent_sessions,
        "server_log": server_log.name,
        "wall_total_s": wall_total,
        "server_cpu_s": server_cpu_s,
        "server_cpu_s_approx": server_cpu_s,
        "server_cpu_note": "RUSAGE_CHILDREN cpu delta; server is the dominant child",
        "server_peak_rss_bytes": server_peak_rss_bytes,
        "failures": len(failures),
        "by_op": rec.summarize(),
    }

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = os.path.join(args.out_dir, f"workload-server-{label}-{ts}.jsonl")
    os.makedirs(args.out_dir, exist_ok=True)
    with open(out_path, "w") as f:
        for op_rec in rec.ops:
            f.write(json.dumps({"type": "op", **op_rec}) + "\n")
        f.write(json.dumps(summary) + "\n")

    print(
        f"label={label} wall={wall_total:.1f}s "
        f"server_cpu={server_cpu_s:.2f}s "
        f"server_peak_rss={server_peak_rss_bytes / 1e6:.1f}MB "
        f"failures={len(failures)}"
    )
    print(f"client={sys.executable} server={server_python}")
    print("flags=" + " ".join(f"{k}={v or '0'}" for k, v in flags.items()))
    print(f"wrote {out_path}")
    for op, stats in summary["by_op"].items():
        print(
            f"  {op:24s} n={stats['n']:5d} mean={stats['mean_s'] * 1000:8.2f}ms "
            f"p50={stats['p50_s'] * 1000:8.2f}ms p95={stats['p95_s'] * 1000:8.2f}ms"
        )
    for fail in failures[:10]:
        print(f"  FAIL {fail}")
    if failures:
        print(f"  server log: {server_log.name}")
        rc = 1
    return rc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n-tiny", type=int, default=50)
    parser.add_argument("--quick", action="store_true", help="fast pass: 5 tiny commands")
    parser.add_argument(
        "--concurrent-sessions",
        type=int,
        default=0,
        help="concurrency sweep size: create N sessions and run one `true` "
        "in each concurrently via asyncio.gather (default 0 = off)",
    )
    parser.add_argument("--upload-mib", type=int, default=10)
    parser.add_argument(
        "--server-python",
        default=sys.executable,
        help="interpreter used to run the swerex-remote server (default: this interpreter)",
    )
    parser.add_argument(
        "--server-cmd",
        default="",
        help="shell command used to launch the server instead of "
        "`<server-python> -m swerex.server`; shlex-split after formatting "
        "{port}/{token} placeholders (e.g. --server-cmd './target/release/agent-execd "
        "--host 127.0.0.1 --port {port} --auth-token {token}')",
    )
    parser.add_argument("--server-timeout", type=float, default=30.0)
    parser.add_argument("--label", default="")
    parser.add_argument("--out-dir", default="benchmark-results")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
