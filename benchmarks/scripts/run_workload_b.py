#!/usr/bin/env python3
"""Workload B: file/workspace operations (benchmark-plan.md, Workload B).

Compares executor variants on file read/write at several sizes, source-tree
tarball unpack, local git clone, git worktree creation, and a reflink copy
probe. Reports per-op wall latency, executor CPU separated from child CPU
(getrusage self vs children), and peak RSS. Results go to gitignored
benchmark-results/ as JSONL.

Usage (from repo root, with the project .venv):
    uv run python benchmarks/scripts/run_workload_b.py --variant swerex-local
    uv run python benchmarks/scripts/run_workload_b.py --variant direct --quick
"""

import argparse
import asyncio
import json
import os
import resource
import shlex
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

FILE_SIZES = (4 * 1024, 1024 * 1024, 100 * 1024 * 1024)
QUICK_FILE_SIZES = (4 * 1024, 1024 * 1024)
TREE_FILES = 500
QUICK_TREE_FILES = 30
CLONE_SRC_CANDIDATE = "/tmp/swerex-ref"


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

    def __init__(self, timeout=120.0):
        from swerex.runtime.abstract import (
            BashAction,
            CloseBashSessionRequest,
            CreateBashSessionRequest,
            ReadFileRequest,
            WriteFileRequest,
        )
        from swerex.runtime.local import LocalRuntime

        self._timeout = timeout
        self._BashAction = BashAction
        self._CloseReq = CloseBashSessionRequest
        self._CreateReq = CreateBashSessionRequest
        self._ReadReq = ReadFileRequest
        self._WriteReq = WriteFileRequest
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

    async def read_file(self, path):
        resp = await self._rt.read_file(self._ReadReq(path=str(path)))
        return resp.content

    async def write_file(self, path, content):
        await self._rt.write_file(self._WriteReq(path=str(path), content=content))


class DirectExecutor:
    """Baseline: plain file IO and one subprocess per command."""

    name = "direct"
    supports_sessions = False

    def __init__(self, timeout=120.0):
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
            return p.returncode, p.stdout + p.stderr

        return await asyncio.to_thread(_run)

    async def close_session(self, session):
        pass

    async def read_file(self, path):
        return await asyncio.to_thread(Path(path).read_text)

    async def write_file(self, path, content):
        def _write():
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)

        await asyncio.to_thread(_write)


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


async def workload_file_sizes(rec, ex, tmpdir, sizes):
    for size in sizes:
        path = os.path.join(tmpdir, f"blob-{size}")
        content = "x" * size
        t0 = time.perf_counter()
        try:
            await ex.write_file(path, content)
            ok, note = True, ""
        except Exception as e:  # noqa: BLE001 - record failure, keep going
            ok, note = False, f"{type(e).__name__}: {e}"
        rec.add(f"file_write_{size}", time.perf_counter() - t0,
                out_bytes=size, ok=ok, note=note)
        t0 = time.perf_counter()
        try:
            back = await ex.read_file(path)
            ok = len(back) == size
            note = "" if ok else f"short read: {len(back)} < {size}"
        except Exception as e:  # noqa: BLE001 - record failure, keep going
            ok, note = False, f"{type(e).__name__}: {e}"
        rec.add(f"file_read_{size}", time.perf_counter() - t0,
                out_bytes=size, ok=ok, note=note)


def _make_source_tree(root, n_files):
    root = Path(root)
    for i in range(n_files):
        sub = root / f"pkg{i % 20}" / f"mod{i % 50}"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / f"file{i}.py").write_text(
            f"# generated source file {i}\nVALUE = {i}\n" + "x = 'data'\n" * 100
        )
    return n_files


async def workload_unpack(rec, ex, session, tmpdir, n_files):
    tree_src = os.path.join(tmpdir, "tree-src")
    os.makedirs(tree_src, exist_ok=True)
    await asyncio.to_thread(_make_source_tree, tree_src, n_files)
    tarball = os.path.join(tmpdir, "tree.tar.gz")
    with tarfile.open(tarball, "w:gz") as tf:
        tf.add(tree_src, arcname="tree")
    dest = os.path.join(tmpdir, "tree-dest")
    os.makedirs(dest, exist_ok=True)
    out = await _timed_run(
        rec, ex, session, "unpack_tarball",
        f"tar -xzf {shlex.quote(tarball)} -C {shlex.quote(dest)}",
    )
    count = sum(1 for _ in Path(dest, "tree").rglob("*") if _.is_file())
    if count != n_files:
        rec.ops[-1]["ok"] = False
        rec.ops[-1]["note"] = f"file count {count} != {n_files} {out[-200:]}"
    else:
        rec.ops[-1]["note"] = f"files={count}"


async def workload_clone(rec, ex, session, tmpdir, src):
    dest = os.path.join(tmpdir, "clone")
    await _timed_run(
        rec, ex, session, "git_clone_local",
        f"git clone --quiet {shlex.quote(src)} {shlex.quote(dest)}",
    )
    cloned = os.path.isdir(dest) and len(os.listdir(dest)) > 0
    if not cloned:
        rec.ops[-1]["ok"] = False
        rec.ops[-1]["note"] = f"clone dest looks empty: {src}"
    else:
        rec.ops[-1]["note"] = f"src={src}"


async def workload_worktree(rec, ex, session, tmpdir, repo):
    wt = os.path.join(tmpdir, "wt")
    await _timed_run(
        rec, ex, session, "git_worktree_add",
        f"git -C {shlex.quote(str(repo))} worktree add --detach "
        f"{shlex.quote(wt)} HEAD",
    )
    try:
        subprocess.run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", wt],
            capture_output=True, timeout=60,
        )
    except Exception:  # noqa: BLE001 - best-effort cleanup
        pass


async def workload_reflink(rec, ex, session, tmpdir):
    src = os.path.join(tmpdir, "reflink-src")
    dest = os.path.join(tmpdir, "reflink-dest")
    Path(src).write_text("y" * (1024 * 1024))
    if sys.platform == "darwin":
        cmd = f"cp -c {shlex.quote(src)} {shlex.quote(dest)}"
    else:
        cmd = f"cp --reflink=always {shlex.quote(src)} {shlex.quote(dest)}"
    t0 = time.perf_counter()
    try:
        exit_code, output = await ex.run(session, cmd)
    except Exception as e:  # noqa: BLE001 - record failure, keep going
        # Session executors raise on nonzero exit (check="raise"); a
        # filesystem that cannot clone is an "unsupported" answer, not a
        # harness failure -- but only for the known signal.
        if "not supported" in str(e).lower():
            rec.add("reflink_copy", time.perf_counter() - t0, ok=True,
                    note=f"unsupported: {str(e)[-200:]}")
        else:
            rec.add("reflink_copy", time.perf_counter() - t0,
                    ok=False, note=f"{type(e).__name__}: {e}")
        return
    wall = time.perf_counter() - t0
    if exit_code != 0:
        rec.add("reflink_copy", wall, exit_code=exit_code, ok=True,
                note=f"unsupported: {output.strip()[-200:]}")
    elif not (os.path.isfile(dest)
              and os.path.getsize(dest) == os.path.getsize(src)):
        rec.add("reflink_copy", wall, exit_code=exit_code, ok=False,
                note="dest missing or size mismatch after cp")
    else:
        rec.add("reflink_copy", wall, out_bytes=os.path.getsize(dest),
                exit_code=exit_code, note="reflink ok")


async def main_async(args):
    ex = EXECUTORS[args.variant](timeout=args.timeout)
    rec = Recorder()
    repo_root = Path(__file__).resolve().parents[2]
    cpu_self_0 = _cpu_seconds(resource.RUSAGE_SELF)
    cpu_child_0 = _cpu_seconds(resource.RUSAGE_CHILDREN)
    t_start = time.perf_counter()

    sizes = QUICK_FILE_SIZES if args.quick else FILE_SIZES
    n_files = QUICK_TREE_FILES if args.quick else TREE_FILES
    if args.clone_src:
        clone_src = args.clone_src
    elif os.path.isdir(CLONE_SRC_CANDIDATE):
        clone_src = CLONE_SRC_CANDIDATE
    else:
        clone_src = str(repo_root)
    worktree_repo = args.worktree_repo or str(repo_root)
    session = "bench-main-b"
    if ex.supports_sessions:
        await ex.create_session(session)
    with tempfile.TemporaryDirectory(prefix="workload-b-") as tmpdir:
        await workload_file_sizes(rec, ex, tmpdir, sizes)
        await workload_unpack(rec, ex, session, tmpdir, n_files)
        await workload_clone(rec, ex, session, tmpdir, clone_src)
        await workload_worktree(rec, ex, session, tmpdir, worktree_repo)
        await workload_reflink(rec, ex, session, tmpdir)
    if ex.supports_sessions:
        await ex.close_session(session)

    wall_total = time.perf_counter() - t_start
    cpu_self = _cpu_seconds(resource.RUSAGE_SELF) - cpu_self_0
    cpu_child = _cpu_seconds(resource.RUSAGE_CHILDREN) - cpu_child_0
    failures = rec.failures()
    summary = {
        "type": "summary",
        "variant": ex.name,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "quick": args.quick,
        "file_sizes": list(sizes),
        "tree_files": n_files,
        "clone_src": clone_src,
        "worktree_repo": worktree_repo,
        "wall_total_s": wall_total,
        "executor_cpu_s": cpu_self,
        "child_cpu_s": cpu_child,
        "peak_rss_bytes": _maxrss_bytes(),
        "failures": len(failures),
        "by_op": rec.summarize(),
    }

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = os.path.join(args.out_dir, f"workload-b-{ex.name}-{ts}.jsonl")
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
    parser.add_argument("--quick", action="store_true",
                        help="small fast verification pass, not a headline result")
    parser.add_argument("--clone-src", default=None,
                        help="git source for the clone op (default: /tmp/swerex-ref if present, else this repo)")
    parser.add_argument("--worktree-repo", default=None,
                        help="repo for the worktree op (default: this repo)")
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--out-dir", default="benchmark-results")
    args = parser.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
