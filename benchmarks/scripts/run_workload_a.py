#!/usr/bin/env python3
"""Workload A: executor microbenchmarks (benchmark-plan.md, Workload A).

Compares executor variants on tiny commands, session lifecycle, output
sizes, env persistence, and concurrent sessions. Reports per-op latency,
executor CPU separated from child CPU (getrusage self vs children), and
peak RSS. Results go to gitignored benchmark-results/ as JSONL.

Usage (from repo root, with the project .venv):
    uv run python benchmarks/scripts/run_workload_a.py --variant swerex-local
    uv run python benchmarks/scripts/run_workload_a.py --variant direct --n-tiny 200
"""

import argparse
import asyncio
import json
import os
import resource
import subprocess
import sys
import time
from datetime import datetime, timezone

OUTPUT_SIZES = (1024, 64 * 1024, 1024 * 1024)


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


class SwerexLocalExecutor:
    name = "swerex-local"
    supports_sessions = True

    def __init__(self, timeout=30.0):
        from swerex.runtime.abstract import (
            BashAction,
            CloseBashSessionRequest,
            CreateBashSessionRequest,
        )
        from swerex.runtime.local import LocalRuntime

        self._timeout = timeout
        self._BashAction = BashAction
        self._CloseReq = CloseBashSessionRequest
        self._CreateReq = CreateBashSessionRequest
        self._rt = LocalRuntime()

    async def create_session(self, session):
        await self._rt.create_session(self._CreateReq(session=session))

    async def run(self, session, command):
        obs = await self._rt.run_in_session(
            self._BashAction(command=command, session=session, timeout=self._timeout)
        )
        return obs.exit_code or 0, obs.output

    async def close_session(self, session):
        await self._rt.close_session(self._CloseReq(session=session))


class DirectExecutor:
    """Baseline: one subprocess per command, no persistent session."""

    name = "direct"
    supports_sessions = False

    def __init__(self, timeout=30.0):
        self._timeout = timeout

    async def create_session(self, session):
        pass

    async def run(self, session, command):
        def _run():
            p = subprocess.run(
                command,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
            return p.returncode, p.stdout

        return await asyncio.to_thread(_run)

    async def close_session(self, session):
        pass


EXECUTORS = {"swerex-local": SwerexLocalExecutor, "direct": DirectExecutor}


async def _timed_run(rec, ex, session, op, command, expect_exit=0):
    t0 = time.perf_counter()
    try:
        exit_code, output = await ex.run(session, command)
        ok = exit_code == expect_exit
    except Exception as e:  # noqa: BLE001 - record failure, keep going
        wall = time.perf_counter() - t0
        rec.add(op, wall, ok=False, note=f"{type(e).__name__}: {e}")
        return ""
    wall = time.perf_counter() - t0
    rec.add(op, wall, out_bytes=len(output), exit_code=exit_code, ok=ok)
    return output


async def workload_session_lifecycle(rec, ex, n=20):
    for i in range(n):
        name = f"bench-lc-{i}"
        t0 = time.perf_counter()
        await ex.create_session(name)
        await ex.close_session(name)
        rec.add("session_create_close", time.perf_counter() - t0)


async def workload_tiny(rec, ex, session, n):
    for _ in range(n):
        await _timed_run(rec, ex, session, "tiny_true", "true")
    for _ in range(20):
        await _timed_run(rec, ex, session, "pwd", "pwd")


async def workload_output_sizes(rec, ex, session, reps=5):
    for size in OUTPUT_SIZES:
        cmd = f"head -c {size} /dev/zero | tr '\\0' 'x'"
        for _ in range(reps):
            out = await _timed_run(rec, ex, session, f"output_{size}", cmd)
            if out and len(out.strip()) < size:
                rec.ops[-1]["ok"] = False
                rec.ops[-1]["note"] = f"short output: {len(out)} < {size}"


async def workload_env_persist(rec, ex, session):
    await ex.run(session, "export BENCH_MARKER=alive-42")
    out = await _timed_run(rec, ex, session, "env_persist", "echo $BENCH_MARKER")
    if "alive-42" not in out:
        rec.ops[-1]["ok"] = False
        rec.ops[-1]["note"] = f"marker missing: {out!r}"


async def workload_concurrent(rec, ex, k):
    if ex.supports_sessions:
        names = [f"bench-cc-{i}" for i in range(k)]
        t0 = time.perf_counter()
        for name in names:
            await ex.create_session(name)
        create_wall = time.perf_counter() - t0
        rec.add("concurrent_create_total", create_wall, note=f"k={k}")

        async def one(name):
            t1 = time.perf_counter()
            code, _ = await ex.run(name, "true")
            return time.perf_counter() - t1, code

        t0 = time.perf_counter()
        results = await asyncio.gather(*(one(n) for n in names))
        total = time.perf_counter() - t0
        for wall, code in results:
            rec.add("concurrent_true", wall, exit_code=code, ok=code == 0)
        rec.add("concurrent_true_total", total, note=f"k={k}")
        for name in names:
            await ex.close_session(name)
    else:
        async def one():
            t1 = time.perf_counter()
            code, _ = await ex.run("", "true")
            return time.perf_counter() - t1, code

        t0 = time.perf_counter()
        results = await asyncio.gather(*(one() for _ in range(k)))
        total = time.perf_counter() - t0
        for wall, code in results:
            rec.add("concurrent_true", wall, exit_code=code, ok=code == 0)
        rec.add("concurrent_true_total", total, note=f"k={k}")


async def main_async(args):
    ex = EXECUTORS[args.variant](timeout=args.timeout)
    rec = Recorder()
    cpu_self_0 = _cpu_seconds(resource.RUSAGE_SELF)
    cpu_child_0 = _cpu_seconds(resource.RUSAGE_CHILDREN)
    t_start = time.perf_counter()

    session = "bench-main"
    if ex.supports_sessions:
        await workload_session_lifecycle(rec, ex)
        await ex.create_session(session)
    await workload_tiny(rec, ex, session, args.n_tiny)
    await workload_output_sizes(rec, ex, session)
    if ex.supports_sessions:
        await workload_env_persist(rec, ex, session)
        await workload_concurrent(rec, ex, args.concurrent)
        await ex.close_session(session)
    else:
        await workload_concurrent(rec, ex, args.concurrent)

    wall_total = time.perf_counter() - t_start
    cpu_self = _cpu_seconds(resource.RUSAGE_SELF) - cpu_self_0
    cpu_child = _cpu_seconds(resource.RUSAGE_CHILDREN) - cpu_child_0
    failures = rec.failures()
    summary = {
        "type": "summary",
        "variant": ex.name,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "n_tiny": args.n_tiny,
        "concurrent_k": args.concurrent,
        "wall_total_s": wall_total,
        "executor_cpu_s": cpu_self,
        "child_cpu_s": cpu_child,
        "peak_rss_bytes": _maxrss_bytes(),
        "failures": len(failures),
        "by_op": rec.summarize(),
    }

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = os.path.join(args.out_dir, f"workload-a-{ex.name}-{ts}.jsonl")
    os.makedirs(args.out_dir, exist_ok=True)
    with open(out_path, "w") as f:
        for op_rec in rec.ops:
            f.write(json.dumps({"type": "op", **op_rec}) + "\n")
        f.write(json.dumps(summary) + "\n")

    print(f"variant={ex.name} wall={wall_total:.1f}s "
          f"executor_cpu={cpu_self:.2f}s child_cpu={cpu_child:.2f}s "
          f"peak_rss={summary['peak_rss_bytes'] / 1e6:.1f}MB "
          f"failures={len(failures)}")
    print(f"wrote {out_path}")
    for op, stats in summary["by_op"].items():
        print(f"  {op:24s} n={stats['n']:5d} mean={stats['mean_s'] * 1000:8.2f}ms "
              f"p50={stats['p50_s'] * 1000:8.2f}ms p95={stats['p95_s'] * 1000:8.2f}ms")
    for fail in failures[:10]:
        print(f"  FAIL {fail}")
    return 1 if failures else 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", choices=sorted(EXECUTORS), required=True)
    parser.add_argument("--n-tiny", type=int, default=200)
    parser.add_argument("--concurrent", type=int, default=100)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--out-dir", default="benchmark-results")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
