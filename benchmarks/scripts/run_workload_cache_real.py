#!/usr/bin/env python3
"""Real-repo P2: shared cache / bootstrap injection cold-vs-warm (ROADMAP P2).

Measures, each xN reps (wall + child CPU-seconds + bytes where meaningful):

- cargo_build_cold_<n>: dev-profile `cargo build --offline`? No — full cold:
  source copied to tmpdir (never touches the real tree/target), fresh
  CARGO_TARGET_DIR + empty CARGO_HOME, real network for the registry.
- cargo_build_warm_noop: rebuild with no changes (same tmp target dir).
- cargo_build_warm_touch: rebuild after touching one execd-core source file.
- uv_install_cold: `uv pip install` of SWE-agent reqs into a fresh venv with
  a fresh UV_CACHE_DIR.
- uv_install_warm: same install into another fresh venv reusing that cache.
- git_clone_full: full clone of marshmallow @ pinned SHA to fresh tmpdir.
- git_clone_reference: same clone with --reference to the vendored testbed.

Never mutates the repo, the real target/, ~/.cargo, or the shared uv cache.
Results go to gitignored benchmark-results/ as JSONL.

Usage (from repo root, host otherwise idle):
    python3 benchmarks/scripts/run_workload_cache_real.py --quick   # 1 rep
    python3 benchmarks/scripts/run_workload_cache_real.py
"""

import argparse
import json
import os
import resource
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

REPS = 3
QUICK_REPS = 1
MARSHMALLOW_URL = "https://github.com/marshmallow-code/marshmallow.git"
MARSHMALLOW_SHA = "bfd2593d4b416122e30cdefe0c72d322ef471611"


def _run(cmd, timeout, **kw):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                       **kw)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def _child_cpu():
    r = resource.getrusage(resource.RUSAGE_CHILDREN)
    return r.ru_utime + r.ru_stime


def _dir_bytes(root):
    total = 0
    for dp, _, fns in os.walk(root, followlinks=False):
        for fn in fns:
            try:
                total += os.lstat(os.path.join(dp, fn)).st_size
            except OSError:
                continue
    return total


class Recorder:
    def __init__(self):
        self.ops = []

    def add(self, op, wall_s, exit_code=0, ok=True, note="", cpu_s=0.0,
            n_bytes=0):
        self.ops.append({"op": op, "wall_s": wall_s, "exit_code": exit_code,
                         "ok": ok, "note": note, "cpu_s": cpu_s,
                         "n_bytes": n_bytes})

    def summarize(self):
        by_op = {}
        for rec in self.ops:
            by_op.setdefault(rec["op"], []).append(rec)
        lines = []
        for op, recs in by_op.items():
            walls = sorted(r["wall_s"] for r in recs)
            cpus = sorted(r["cpu_s"] for r in recs)
            mean = sum(walls) / len(walls)
            lines.append(f"  {op:28s} n={len(walls):3d} "
                         f"wall_mean={mean:9.2f}s wall_min={walls[0]:9.2f}s "
                         f"cpu_mean={sum(cpus) / len(cpus):9.2f}s "
                         f"fails={sum(1 for r in recs if not r['ok'])}")
        return "\n".join(lines)


def op_cargo(rec, reps, timeout, tmp_base):
    src = Path(tempfile.mkdtemp(prefix="cargo-src-", dir=tmp_base))
    for name in ("Cargo.toml", "Cargo.lock", "crates"):
        p = Path(name)
        dest = src / name
        if p.is_dir():
            shutil.copytree(p, dest, symlinks=True)
        else:
            shutil.copy2(p, dest)
    home = Path(tempfile.mkdtemp(prefix="cargo-home-", dir=tmp_base))
    target = Path(tempfile.mkdtemp(prefix="cargo-target-", dir=tmp_base))
    env = dict(os.environ, CARGO_HOME=str(home), CARGO_TARGET_DIR=str(target),
               CARGO_NET_RETRY="2")
    for i in range(reps):
        if i > 0:  # fresh target+registry each cold rep, same source copy
            shutil.rmtree(target, ignore_errors=True)
            shutil.rmtree(home, ignore_errors=True)
            target.mkdir(parents=True)
            home.mkdir(parents=True)
        c0, t0 = _child_cpu(), time.perf_counter()
        rc, out = _run(["cargo", "build", "--locked"], timeout, cwd=src,
                       env=env)
        wall = time.perf_counter() - t0
        ok = rc == 0 and (target / "debug").is_dir()
        rec.add("cargo_build_cold", wall, exit_code=rc, ok=ok,
                cpu_s=_child_cpu() - c0, n_bytes=_dir_bytes(target),
                note=f"rep={i + 1}/{reps}" + ("" if ok else f" {out[-200:]}"))
    for i in range(reps):
        c0, t0 = _child_cpu(), time.perf_counter()
        rc, out = _run(["cargo", "build", "--locked"], timeout, cwd=src,
                       env=env)
        rec.add("cargo_build_warm_noop", time.perf_counter() - t0,
                exit_code=rc, ok=rc == 0, cpu_s=_child_cpu() - c0,
                note=f"rep={i + 1}/{reps}")
    touchy = src / "crates" / "execd-core" / "src" / "lib.rs"
    if not touchy.exists():
        touchy = sorted((src / "crates").rglob("*.rs"))[0]
    for i in range(reps):
        touchy.touch()
        c0, t0 = _child_cpu(), time.perf_counter()
        rc, out = _run(["cargo", "build", "--locked"], timeout, cwd=src,
                       env=env)
        rec.add("cargo_build_warm_touch", time.perf_counter() - t0,
                exit_code=rc, ok=rc == 0, cpu_s=_child_cpu() - c0,
                note=f"rep={i + 1}/{reps} touched={touchy.name}")


def op_uv(rec, reps, timeout, tmp_base):
    cache = Path(tempfile.mkdtemp(prefix="uv-cache-", dir=tmp_base))
    req = Path("_vendor/SWE-agent/pyproject.toml")
    if not req.exists():
        rec.add("uv_install_cold", 0.0, ok=False, note="no vendor checkout")
        return
    for i in range(reps):
        if i > 0:
            shutil.rmtree(cache, ignore_errors=True)
            cache.mkdir(parents=True)
        venv = Path(tempfile.mkdtemp(prefix="uv-venv-cold-", dir=tmp_base))
        env = dict(os.environ, UV_CACHE_DIR=str(cache))
        c0, t0 = _child_cpu(), time.perf_counter()
        rc1, _ = _run([sys.executable, "-m", "venv", str(venv)], timeout)
        rc2, out2 = _run(
            ["uv", "pip", "install", "--python", str(venv / "bin" / "python"),
             "-e", "_vendor/SWE-agent"], timeout, env=env)
        wall = time.perf_counter() - t0
        ok = rc1 == 0 and rc2 == 0
        rec.add("uv_install_cold", wall, exit_code=rc2, ok=ok,
                cpu_s=_child_cpu() - c0, n_bytes=_dir_bytes(cache),
                note=f"rep={i + 1}/{reps}" + ("" if ok else f" {out2[-200:]}"))
    for i in range(reps):
        venv = Path(tempfile.mkdtemp(prefix="uv-venv-warm-", dir=tmp_base))
        env = dict(os.environ, UV_CACHE_DIR=str(cache))
        c0, t0 = _child_cpu(), time.perf_counter()
        rc1, _ = _run([sys.executable, "-m", "venv", str(venv)], timeout)
        rc2, out2 = _run(
            ["uv", "pip", "install", "--python", str(venv / "bin" / "python"),
             "-e", "_vendor/SWE-agent"], timeout, env=env)
        rec.add("uv_install_warm", time.perf_counter() - t0, exit_code=rc2,
                ok=rc1 == 0 and rc2 == 0, cpu_s=_child_cpu() - c0,
                n_bytes=_dir_bytes(cache), note=f"rep={i + 1}/{reps}")


def op_clone(rec, reps, timeout, tmp_base):
    ref = Path("_vendor/marshmallow-testbed")
    for i in range(reps):
        dest = os.path.join(tmp_base, f"clone-full-{i}")
        c0, t0 = _child_cpu(), time.perf_counter()
        rc, out = _run(["git", "clone", "-q", MARSHMALLOW_URL, dest],
                       timeout)
        if rc == 0:
            rc, out = _run(["git", "-C", dest, "checkout", "-q",
                            MARSHMALLOW_SHA], timeout)
        wall = time.perf_counter() - t0
        ok = rc == 0
        rec.add("git_clone_full", wall, exit_code=rc, ok=ok,
                cpu_s=_child_cpu() - c0,
                n_bytes=_dir_bytes(os.path.join(dest, ".git")) if ok else 0,
                note=f"rep={i + 1}/{reps}" + ("" if ok else f" {out[-200:]}"))
    for i in range(reps):
        dest = os.path.join(tmp_base, f"clone-ref-{i}")
        c0, t0 = _child_cpu(), time.perf_counter()
        rc, out = _run(["git", "clone", "-q", "--reference", str(ref),
                        MARSHMALLOW_URL, dest], timeout)
        if rc == 0:
            rc, out = _run(["git", "-C", dest, "checkout", "-q",
                            MARSHMALLOW_SHA], timeout)
        wall = time.perf_counter() - t0
        ok = rc == 0
        rec.add("git_clone_reference", wall, exit_code=rc, ok=ok,
                cpu_s=_child_cpu() - c0,
                n_bytes=_dir_bytes(os.path.join(dest, ".git")) if ok else 0,
                note=f"rep={i + 1}/{reps}" + ("" if ok else f" {out[-200:]}"))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--timeout", type=float, default=1200.0)
    ap.add_argument("--out-dir", default="benchmark-results")
    ap.add_argument("--ops", default="cargo,uv,clone",
                    help="comma subset of cargo,uv,clone")
    args = ap.parse_args()
    reps = QUICK_REPS if args.quick else REPS
    tmp_base = tempfile.mkdtemp(prefix="cache-real-")
    rec = Recorder()
    t_start, cpu0 = time.perf_counter(), _child_cpu()
    ops = set(args.ops.split(","))
    if "cargo" in ops:
        op_cargo(rec, reps, args.timeout, tmp_base)
    if "uv" in ops:
        op_uv(rec, reps, args.timeout, tmp_base)
    if "clone" in ops:
        op_clone(rec, reps, args.timeout, tmp_base)
    wall = time.perf_counter() - t_start
    fails = sum(1 for r in rec.ops if not r["ok"])
    print(f"variant=direct wall={wall:.1f}s "
          f"child_cpu={_child_cpu() - cpu0:.2f}s failures={fails}")
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = out_dir / f"workload-cache-real-{stamp}.jsonl"
    with open(path, "w") as f:
        for r in rec.ops:
            f.write(json.dumps({"ts": stamp, **r}) + "\n")
    print(f"wrote {path}")
    print(rec.summarize())
    shutil.rmtree(tmp_base, ignore_errors=True)
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
