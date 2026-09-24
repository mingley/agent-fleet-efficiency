#!/usr/bin/env python3
"""Workload C: build/test cache measurements (ROADMAP P2, benchmark-plan.md).

Builds deterministic offline fixtures in a tmpdir (no network) and measures
cold vs warm vs touch-one rebuild/test times for Rust/Cargo, Python/unittest,
and C/make (plus sccache/ccache arms when those tools exist). Reports per-op
wall latency with executor-vs-child CPU deltas (getrusage self vs children)
and peak RSS. Results go to gitignored benchmark-results/ as JSONL.

Builds run via direct subprocess; there are no executor variants.

Usage (from repo root):
    python3 benchmarks/scripts/run_workload_c.py --quick
    python3 benchmarks/scripts/run_workload_c.py
    python3 benchmarks/scripts/run_workload_c.py --quick --with-c
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

N_MODULES = 50
QUICK_N_MODULES = 10


def _percentile(sorted_vals, pct):
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * pct / 100
    lo, hi = int(k), min(int(k) + 1, len(sorted_vals) - 1)
    frac = k - lo
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


def _maxrss_bytes():
    # ru_maxrss is bytes on macOS, KiB on Linux.
    scale = 1 if sys.platform == "darwin" else 1024
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale


def _cpu_seconds(who):
    r = resource.getrusage(who)
    return r.ru_utime + r.ru_stime


class Recorder:
    def __init__(self):
        self.ops = []

    def add(self, op, wall_s, out_bytes=0, exit_code=0, ok=True, note="",
            cpu_self_s=0.0, cpu_child_s=0.0):
        self.ops.append(
            {
                "op": op,
                "wall_s": wall_s,
                "out_bytes": out_bytes,
                "exit_code": exit_code,
                "ok": ok,
                "note": note,
                "cpu_self_s": cpu_self_s,
                "cpu_child_s": cpu_child_s,
            }
        )

    def skip(self, op, note):
        self.add(op, 0.0, ok=True, note=f"skipped: {note}")

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


def _timed_cmd(rec, op, cmd, cwd=None, env=None, timeout=600.0, note=""):
    """Run cmd via direct subprocess, recording wall + CPU deltas."""
    self0 = _cpu_seconds(resource.RUSAGE_SELF)
    child0 = _cpu_seconds(resource.RUSAGE_CHILDREN)
    t0 = time.perf_counter()
    try:
        p = subprocess.run(
            cmd, cwd=cwd, env=env, capture_output=True, text=True,
            timeout=timeout,
        )
        wall = time.perf_counter() - t0
        ok = p.returncode == 0
        tail = (p.stdout + p.stderr)[-300:]
        if ok:
            full_note = note
        else:
            full_note = (note + " " if note else "") + f"rc={p.returncode} {tail}"
        rec.add(op, wall, out_bytes=len(p.stdout) + len(p.stderr),
                exit_code=p.returncode, ok=ok, note=full_note,
                cpu_self_s=_cpu_seconds(resource.RUSAGE_SELF) - self0,
                cpu_child_s=_cpu_seconds(resource.RUSAGE_CHILDREN) - child0)
        return p
    except subprocess.TimeoutExpired as e:
        wall = time.perf_counter() - t0
        rec.add(op, wall, ok=False,
                note=f"timeout after {timeout}s: {e}",
                cpu_self_s=_cpu_seconds(resource.RUSAGE_SELF) - self0,
                cpu_child_s=_cpu_seconds(resource.RUSAGE_CHILDREN) - child0)
        return None
    except Exception as e:  # noqa: BLE001 - record failure, keep going
        wall = time.perf_counter() - t0
        rec.add(op, wall, ok=False, note=f"{type(e).__name__}: {e}",
                cpu_self_s=_cpu_seconds(resource.RUSAGE_SELF) - self0,
                cpu_child_s=_cpu_seconds(resource.RUSAGE_CHILDREN) - child0)
        return None


# ---------------------------------------------------------------- Rust/Cargo

def _make_rust_fixture(root, n_modules):
    root = Path(root)
    src = root / "src"
    src.mkdir(parents=True, exist_ok=True)
    (root / "Cargo.toml").write_text(
        '[package]\nname = "workload_c_fixture"\nversion = "0.1.0"\n'
        'edition = "2021"\n\n[lib]\npath = "src/lib.rs"\n'
    )
    mods = "\n".join(f"pub mod m{i};" for i in range(n_modules))
    calls = f"m0::f0(i as u64)" + "".join(
        f".wrapping_add(m{i}::f{i}(i as u64))" for i in range(1, n_modules)
    )
    (src / "lib.rs").write_text(
        f"{mods}\n\n"
        "pub fn total(seed: u64) -> u64 {\n"
        f"    let mut acc = seed;\n"
        f"    for i in 0..16u64 {{ acc = acc.wrapping_add({calls}); }}\n"
        "    acc\n"
        "}\n\n"
        "#[test]\n"
        "fn total_is_deterministic() {\n"
        "    assert_eq!(total(7), total(7));\n"
        "    assert_ne!(total(7), total(8));\n"
        "}\n"
    )
    for i in range(n_modules):
        (src / f"m{i}.rs").write_text(
            f"pub fn f{i}(x: u64) -> u64 {{\n"
            f"    let mut v = x.wrapping_mul(6364136223846793005)"
            f".wrapping_add({1000 + i});\n"
            "    for _ in 0..64 {\n"
            "        v = v.wrapping_mul(2862933555777941757)"
            ".wrapping_add(3037000493);\n"
            "        v ^= v >> 17;\n"
            "    }\n"
            "    v\n"
            "}\n"
        )


def workload_rust(rec, tmpdir, n_modules, timeout):
    proj = os.path.join(tmpdir, "rust-proj")
    os.makedirs(proj, exist_ok=True)
    _make_rust_fixture(proj, n_modules)
    if shutil.which("cargo") is None:
        for op in ("rust_build_cold", "rust_build_warm",
                    "rust_build_touch_one", "rust_test"):
            rec.skip(op, "cargo not found")
        rec.skip("rust_build_sccache_cold", "cargo not found")
        rec.skip("rust_build_sccache_warm", "cargo not found")
        return

    # Offline: std-only code with no dependencies must never touch network.
    base_env = dict(os.environ, CARGO_NET_OFFLINE="true",
                    CARGO_TERM_QUIET="true")

    shutil.rmtree(os.path.join(proj, "target"), ignore_errors=True)
    _timed_cmd(rec, "rust_build_cold", ["cargo", "build", "--offline"],
               cwd=proj, env=base_env, timeout=timeout,
               note=f"modules={n_modules}")
    _timed_cmd(rec, "rust_build_warm", ["cargo", "build", "--offline"],
               cwd=proj, env=base_env, timeout=timeout,
               note="no-change rebuild")
    time.sleep(1.05)  # ensure mtime granularity behind `touch`
    Path(proj, "src", "m0.rs").touch()
    _timed_cmd(rec, "rust_build_touch_one", ["cargo", "build", "--offline"],
               cwd=proj, env=base_env, timeout=timeout,
               note="touched src/m0.rs")
    _timed_cmd(rec, "rust_test", ["cargo", "test", "--offline"],
               cwd=proj, env=base_env, timeout=timeout, note="1 test")

    if shutil.which("sccache") is None:
        rec.skip("rust_build_sccache_cold", "sccache not found")
        rec.skip("rust_build_sccache_warm", "sccache not found")
        return
    scc_env = dict(base_env, RUSTC_WRAPPER="sccache")
    shutil.rmtree(os.path.join(proj, "target"), ignore_errors=True)
    _timed_cmd(rec, "rust_build_sccache_cold",
               ["cargo", "build", "--offline"],
               cwd=proj, env=scc_env, timeout=timeout,
               note="RUSTC_WRAPPER=sccache")
    _timed_cmd(rec, "rust_build_sccache_warm",
               ["cargo", "build", "--offline"],
               cwd=proj, env=scc_env, timeout=timeout,
               note="RUSTC_WRAPPER=sccache no-change")


# ---------------------------------------------------------- Python/unittest

def _make_python_fixture(root, n_modules):
    root = Path(root)
    pkg = root / "mypkg"
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(
        "\n".join(f"from .mod{i} import VALUE_{i}" for i in range(n_modules))
        + "\n"
    )
    for i in range(n_modules):
        (pkg / f"mod{i}.py").write_text(
            f'"""Generated module {i}."""\n\nVALUE_{i} = {i}\n\n'
            f"def transform_{i}(x):\n"
            "    v = x\n"
            "    for _ in range(50):\n"
            f"        v = (v * 1103515245 + {12345 + i}) & 0x7FFFFFFF\n"
            "    return v\n"
        )
    tests = root / "tests"
    tests.mkdir(exist_ok=True)
    (tests / "__init__.py").write_text("")
    (tests / "test_mods.py").write_text(
        "import unittest\n\nimport mypkg\n\n\n"
        "class ModTest(unittest.TestCase):\n"
        "    def test_values(self):\n"
        f"        self.assertEqual(mypkg.VALUE_0, 0)\n"
        f"        self.assertEqual(mypkg.VALUE_{n_modules - 1}, "
        f"{n_modules - 1})\n\n"
        "    def test_transforms(self):\n"
        f"        self.assertEqual(mypkg.mod0.transform_0(7), "
        "mypkg.mod0.transform_0(7))\n"
        f"        self.assertEqual(mypkg.mod{n_modules - 1}"
        f".transform_{n_modules - 1}(7), "
        f"mypkg.mod{n_modules - 1}.transform_{n_modules - 1}(7))\n"
    )


def _rm_pycache(root):
    for dirpath, dirnames, _ in os.walk(root):
        for d in [d for d in dirnames if d == "__pycache__"]:
            shutil.rmtree(os.path.join(dirpath, d), ignore_errors=True)


def workload_python(rec, tmpdir, n_modules, timeout):
    proj = os.path.join(tmpdir, "py-proj")
    os.makedirs(proj, exist_ok=True)
    _make_python_fixture(proj, n_modules)
    cmd = [sys.executable, "-m", "unittest", "discover", "-s", "tests",
           "-t", "."]
    _rm_pycache(proj)
    _timed_cmd(rec, "python_test_cold", cmd, cwd=proj, timeout=timeout,
               note=f"modules={n_modules}, no __pycache__")
    _timed_cmd(rec, "python_test_warm", cmd, cwd=proj, timeout=timeout,
               note="rerun no-change")
    time.sleep(1.05)
    Path(proj, "mypkg", "mod0.py").touch()
    _timed_cmd(rec, "python_test_touch_one", cmd, cwd=proj,
               timeout=timeout, note="touched mypkg/mod0.py")

    # uv cache: note only, never install anything (no network).
    if shutil.which("uv") is None:
        rec.skip("python_uv_cache", "uv not found; no network installs")
        return
    try:
        p = subprocess.run(["uv", "cache", "dir"], capture_output=True,
                           text=True, timeout=30)
        cachedir = p.stdout.strip() if p.returncode == 0 else ""
        if cachedir and os.path.isdir(cachedir):
            rec.add("python_uv_cache", 0.0, ok=True,
                    note=f"uv cache present at {cachedir}")
        elif cachedir:
            rec.add("python_uv_cache", 0.0, ok=True,
                    note=f"uv cache dir {cachedir} absent/empty; "
                    "no network installs")
        else:
            rec.skip("python_uv_cache",
                     f"uv cache dir unknown: {(p.stderr or '').strip()[-150:]}")
    except Exception as e:  # noqa: BLE001 - note only, never fails
        rec.skip("python_uv_cache", f"uv probe failed: {e}")


# ------------------------------------------------------------------ C/make

def _make_c_fixture(root, n_files):
    root = Path(root)
    decls = "\n".join(f"int f{i}(void);" for i in range(n_files))
    calls = " + ".join(f"f{i}()" for i in range(n_files))
    (root / "main.c").write_text(
        '#include <stdio.h>\n' + decls + "\nint main(void) {\n"
        f'    printf("%d\\n", {calls});\n    return 0;\n}}\n'
    )
    for i in range(n_files):
        (root / f"f{i}.c").write_text(
            f"int f{i}(void) {{\n"
            f"    int v = {i};\n"
            "    for (int k = 0; k < 200; k++) v = (v * 1103515245 + 12345)"
            " & 0x7fffffff;\n"
            "    return v;\n}\n"
        )
    objs = " ".join(f"f{i}.o" for i in range(n_files)) + " main.o"
    (root / "Makefile").write_text(
        "CC ?= cc\nCFLAGS ?= -O2 -Wall\nOBJS = " + objs + "\n\n"
        "prog: $(OBJS)\n\t$(CC) $(CFLAGS) -o prog $(OBJS)\n\n"
        "%.o: %.c\n\t$(CC) $(CFLAGS) -c $< -o $@\n\n"
        "clean:\n\trm -f $(OBJS) prog\n\n.PHONY: clean\n"
    )


def workload_c(rec, tmpdir, n_files, timeout):
    if shutil.which("cc") is None or shutil.which("make") is None:
        missing = [t for t in ("cc", "make") if shutil.which(t) is None]
        rec.skip("c_section", f"{'/'.join(missing)} not found")
        return
    proj = os.path.join(tmpdir, "c-proj")
    os.makedirs(proj, exist_ok=True)
    _make_c_fixture(proj, n_files)
    subprocess.run(["make", "clean"], cwd=proj, capture_output=True,
                   timeout=60)
    _timed_cmd(rec, "c_build_cold", ["make"], cwd=proj, timeout=timeout,
               note=f"files={n_files}")
    _timed_cmd(rec, "c_build_warm", ["make"], cwd=proj, timeout=timeout,
               note="no-change rebuild")
    time.sleep(1.05)
    Path(proj, "f0.c").touch()
    _timed_cmd(rec, "c_build_touch_one", ["make"], cwd=proj,
               timeout=timeout, note="touched f0.c")
    if shutil.which("ccache") is None:
        rec.skip("c_build_ccache", "ccache not found")
        return
    subprocess.run(["make", "clean"], cwd=proj, capture_output=True,
                   timeout=60)
    ccache_env = dict(os.environ, CC="ccache cc")
    _timed_cmd(rec, "c_build_ccache", ["make"], cwd=proj, env=ccache_env,
               timeout=timeout, note="CC='ccache cc'")


# ------------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true",
                        help="small fast verification pass, not a headline "
                        "result (10 modules; skips C unless --with-c)")
    parser.add_argument("--with-c", action="store_true",
                        help="include the C section in --quick runs")
    parser.add_argument("--timeout", type=float, default=600.0,
                        help="per-command timeout in seconds")
    parser.add_argument("--out-dir", default="benchmark-results")
    args = parser.parse_args()

    n = QUICK_N_MODULES if args.quick else N_MODULES
    run_c = (not args.quick) or args.with_c

    rec = Recorder()
    cpu_self_0 = _cpu_seconds(resource.RUSAGE_SELF)
    cpu_child_0 = _cpu_seconds(resource.RUSAGE_CHILDREN)
    t_start = time.perf_counter()

    with tempfile.TemporaryDirectory(prefix="workload-c-") as tmpdir:
        workload_rust(rec, tmpdir, n, args.timeout)
        workload_python(rec, tmpdir, n, args.timeout)
        if run_c:
            workload_c(rec, tmpdir, n, args.timeout)
        else:
            rec.skip("c_section", "--quick without --with-c")

    wall_total = time.perf_counter() - t_start
    cpu_self = _cpu_seconds(resource.RUSAGE_SELF) - cpu_self_0
    cpu_child = _cpu_seconds(resource.RUSAGE_CHILDREN) - cpu_child_0
    failures = rec.failures()
    summary = {
        "type": "summary",
        "variant": "direct",
        "workload": "c",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "quick": args.quick,
        "n_modules": n,
        "c_section": run_c,
        "wall_total_s": wall_total,
        "executor_cpu_s": cpu_self,
        "child_cpu_s": cpu_child,
        "peak_rss_bytes": _maxrss_bytes(),
        "failures": len(failures),
        "by_op": rec.summarize(),
    }

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = os.path.join(args.out_dir, f"workload-c-direct-{ts}.jsonl")
    os.makedirs(args.out_dir, exist_ok=True)
    with open(out_path, "w") as f:
        for op_rec in rec.ops:
            f.write(json.dumps({"type": "op", **op_rec}) + "\n")
        f.write(json.dumps(summary) + "\n")

    print(f"variant=direct wall={wall_total:.1f}s "
          f"executor_cpu={cpu_self:.2f}s child_cpu={cpu_child:.2f}s "
          f"peak_rss={summary['peak_rss_bytes'] / 1e6:.1f}MB "
          f"failures={len(failures)}")
    print(f"wrote {out_path}")
    for op, stats in summary["by_op"].items():
        print(f"  {op:24s} n={stats['n']:5d} "
              f"mean={stats['mean_s'] * 1000:8.2f}ms "
              f"p50={stats['p50_s'] * 1000:8.2f}ms "
              f"p95={stats['p95_s'] * 1000:8.2f}ms")
    for fail in failures[:10]:
        print(f"  FAIL {fail}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
