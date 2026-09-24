#!/usr/bin/env python3
"""Workspace provisioning comparison (ROADMAP P3, container-feasible subset).

Builds a deterministic source-tree fixture in a tmpdir (mixed file sizes,
git init + 2 commits with fixed author/date), then compares provisioning
ops, each x3 reps (1 rep with --quick):

- full git clone (local path, fresh dest each rep)
- git worktree add + remove (on the fixture repo)
- cp -r tree copy
- tar create+extract pipe
- reflink copy attempt (best-effort probe; unsupported -> ok/skipped)

All git commands run ONLY inside tmpdir fixture dirs, never in this repo.
No caches are dropped (no root); rep 1 is reported as cold page-cache and
reps 2-3 as warm, separately. Results go to gitignored benchmark-results/
as JSONL.

Usage (from repo root):
    python3 benchmarks/scripts/run_workload_provision.py --quick
    python3 benchmarks/scripts/run_workload_provision.py --out-dir benchmark-results
"""

import argparse
import json
import os
import resource
import shlex
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

TREE_FILES = 10_000
QUICK_TREE_FILES = 1_000
REPS = 3
QUICK_REPS = 1

GIT_ENV = {
    "GIT_AUTHOR_NAME": "bench",
    "GIT_AUTHOR_EMAIL": "bench@example.com",
    "GIT_COMMITTER_NAME": "bench",
    "GIT_COMMITTER_EMAIL": "bench@example.com",
    "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
    "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
}
GIT_ENV_2 = dict(GIT_ENV, GIT_AUTHOR_DATE="2000-01-02T00:00:00+00:00",
                 GIT_COMMITTER_DATE="2000-01-02T00:00:00+00:00")


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
            rep_order = list(walls)  # rep1, rep2, rep3 in run order
            walls = sorted(walls)
            warm = rep_order[1:]
            summary[op] = {
                "n": len(walls),
                "mean_s": sum(walls) / len(walls),
                "p50_s": _percentile(walls, 50),
                "p95_s": _percentile(walls, 95),
                "p99_s": _percentile(walls, 99),
                "max_s": walls[-1],
                # Cold-vs-warm: rep 1 ran on a cold page cache, reps 2-3
                # warm. Caches are never dropped; both are reported.
                "cold_rep1_s": rep_order[0] if rep_order else None,
                "warm_mean_s": (sum(warm) / len(warm)) if warm else None,
            }
        return summary

    def failures(self):
        return [rec for rec in self.ops if not rec["ok"]]


def _run(cmd, timeout, cwd=None, shell=False, env=None):
    """Run one subprocess; returns (exit_code, combined_output)."""
    merged = dict(os.environ, **(env or {}))
    p = subprocess.run(
        cmd, shell=shell, capture_output=True, text=True,
        timeout=timeout, cwd=cwd, env=merged,
    )
    return p.returncode, p.stdout + p.stderr


def _file_content(i):
    # Deterministic mixed sizes: 70% small, 20% medium, 10% large.
    cls = i % 10
    if cls < 7:
        n_lines = 5 + (i % 11)
    elif cls < 9:
        n_lines = 60 + (i % 40)
    else:
        n_lines = 900 + (i % 200)
    head = f"# generated source file {i}\nVALUE = {i}\n"
    body = "".join(f"x{i}_{j} = 'data-{i}-{j}'\n" for j in range(n_lines))
    return head + body


def _write_tree_files(root, lo, hi):
    for i in range(lo, hi):
        sub = root / f"pkg{i % 50}" / f"mod{i % 100}"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / f"file{i}.py").write_text(_file_content(i))


def _count_files(root):
    return sum(1 for _ in Path(root).rglob("*") if _.is_file())


def make_fixture(tmpdir, n_files, timeout):
    """Create deterministic git repo fixture. Returns (src_dir, file_count).

    All git commands run with cwd inside tmpdir only.
    """
    src = Path(tmpdir) / "fixture-src"
    src.mkdir(parents=True, exist_ok=True)
    half = n_files // 2
    _write_tree_files(src, 0, half)
    rc, out = _run(["git", "init", "-q"], timeout, cwd=str(src))
    if rc != 0:
        raise RuntimeError(f"git init failed: {out[-500:]}")
    # Disable auto-gc: with 10k loose objects, commit 2 would fork a
    # background repack that mutates .git mid-run (racy clone, shifting
    # file counts). Local config in the tmpdir fixture only.
    rc, out = _run(["git", "config", "gc.auto", "0"], timeout, cwd=str(src))
    if rc != 0:
        raise RuntimeError(f"git config failed: {out[-500:]}")
    rc, out = _run(["git", "add", "-A"], timeout, cwd=str(src))
    if rc != 0:
        raise RuntimeError(f"git add failed: {out[-500:]}")
    rc, out = _run(["git", "-c", "commit.gpgsign=false", "commit", "-q",
                    "-m", "commit 1"], timeout, cwd=str(src), env=GIT_ENV)
    if rc != 0:
        raise RuntimeError(f"git commit 1 failed: {out[-500:]}")
    _write_tree_files(src, half, n_files)
    rc, out = _run(["git", "add", "-A"], timeout, cwd=str(src))
    if rc != 0:
        raise RuntimeError(f"git add failed: {out[-500:]}")
    rc, out = _run(["git", "-c", "commit.gpgsign=false", "commit", "-q",
                    "-m", "commit 2"], timeout, cwd=str(src), env=GIT_ENV_2)
    if rc != 0:
        raise RuntimeError(f"git commit 2 failed: {out[-500:]}")
    return src, _count_files(src)


def op_clone(rec, rep, reps, src, tmpdir, timeout):
    dest = os.path.join(tmpdir, f"clone-{rep}")
    t0 = time.perf_counter()
    try:
        rc, out = _run(["git", "clone", "--quiet", str(src), dest], timeout)
        ok = rc == 0
        note = ""
    except Exception as e:  # noqa: BLE001 - record failure, keep going
        wall = time.perf_counter() - t0
        rec.add("git_clone", wall, ok=False,
                note=f"rep={rep + 1}/{reps} {type(e).__name__}: {e}")
        return
    wall = time.perf_counter() - t0
    if ok and not (os.path.isdir(dest) and len(os.listdir(dest)) > 0):
        ok = False
        note = f"rep={rep + 1}/{reps} clone dest looks empty"
    else:
        note = f"rep={rep + 1}/{reps} {'cold' if rep == 0 else 'warm'}"
        if not ok:
            note += f" {out.strip()[-200:]}"
    rec.add("git_clone", wall, exit_code=rc, ok=ok, note=note)


def op_worktree(rec, rep, reps, src, tmpdir, timeout):
    wt = os.path.join(tmpdir, f"wt-{rep}")
    t0 = time.perf_counter()
    try:
        rc, out = _run(["git", "-C", str(src), "worktree", "add", "--detach",
                        wt, "HEAD"], timeout)
        rm_rc, rm_out = _run(
            ["git", "-C", str(src), "worktree", "remove", "--force", wt],
            timeout) if rc == 0 else (0, "")
        ok = rc == 0 and rm_rc == 0
        detail = "" if ok else f" add={out.strip()[-150:]} rm={rm_out.strip()[-150:]}"
    except Exception as e:  # noqa: BLE001 - record failure, keep going
        rec.add("git_worktree_add_remove", time.perf_counter() - t0, ok=False,
                note=f"rep={rep + 1}/{reps} {type(e).__name__}: {e}")
        return
    rec.add("git_worktree_add_remove", time.perf_counter() - t0,
            exit_code=rc, ok=ok,
            note=f"rep={rep + 1}/{reps} {'cold' if rep == 0 else 'warm'}{detail}")


def op_copy(rec, rep, reps, src, src_count, tmpdir, timeout):
    dest = os.path.join(tmpdir, f"copy-{rep}")
    t0 = time.perf_counter()
    try:
        rc, out = _run(["cp", "-r", str(src), dest], timeout)
        ok = rc == 0
        detail = ""
    except Exception as e:  # noqa: BLE001 - record failure, keep going
        rec.add("cp_r_copy", time.perf_counter() - t0, ok=False,
                note=f"rep={rep + 1}/{reps} {type(e).__name__}: {e}")
        return
    wall = time.perf_counter() - t0
    if ok:
        got = _count_files(dest)
        if got != src_count:
            ok = False
            detail = f" file count {got} != {src_count}"
        else:
            detail = f" files={got}"
    else:
        detail = f" {out.strip()[-200:]}"
    rec.add("cp_r_copy", wall, exit_code=rc, ok=ok,
            note=f"rep={rep + 1}/{reps} {'cold' if rep == 0 else 'warm'}{detail}")


def op_tar_pipe(rec, rep, reps, src, src_count, tmpdir, timeout):
    destdir = os.path.join(tmpdir, f"tarpipe-{rep}")
    os.makedirs(destdir, exist_ok=True)
    cmd = (f"tar -cf - -C {shlex.quote(tmpdir)} {shlex.quote(src.name)}"
           f" | tar -xf - -C {shlex.quote(destdir)}")
    t0 = time.perf_counter()
    try:
        rc, out = _run(cmd, timeout, shell=True)
        ok = rc == 0
        detail = ""
    except Exception as e:  # noqa: BLE001 - record failure, keep going
        rec.add("tar_pipe", time.perf_counter() - t0, ok=False,
                note=f"rep={rep + 1}/{reps} {type(e).__name__}: {e}")
        return
    wall = time.perf_counter() - t0
    if ok:
        got = _count_files(os.path.join(destdir, src.name))
        if got != src_count:
            ok = False
            detail = f" file count {got} != {src_count}"
        else:
            detail = f" files={got}"
    else:
        detail = f" {out.strip()[-200:]}"
    rec.add("tar_pipe", wall, exit_code=rc, ok=ok,
            note=f"rep={rep + 1}/{reps} {'cold' if rep == 0 else 'warm'}{detail}")


def op_reflink(rec, rep, reps, src, src_count, tmpdir, timeout):
    # Best-effort probe: filesystems without clone support report
    # ok=True/skipped, never a harness failure.
    dest = os.path.join(tmpdir, f"reflink-{rep}")
    if sys.platform == "darwin":
        cmd = ["cp", "-c", "-R", str(src), dest]
    else:
        cmd = ["cp", "--reflink=always", "-r", str(src), dest]
    t0 = time.perf_counter()
    try:
        rc, out = _run(cmd, timeout)
    except Exception as e:  # noqa: BLE001 - unsupported counts as skipped
        rec.add("reflink_copy", time.perf_counter() - t0, ok=True,
                note=f"rep={rep + 1}/{reps} skipped: {type(e).__name__}: {e}")
        return
    wall = time.perf_counter() - t0
    if rc != 0:
        rec.add("reflink_copy", wall, exit_code=rc, ok=True,
                note=f"rep={rep + 1}/{reps} skipped: {out.strip()[-200:]}")
    elif _count_files(dest) != src_count:
        rec.add("reflink_copy", wall, exit_code=rc, ok=True,
                note=f"rep={rep + 1}/{reps} skipped: dest file count mismatch")
    else:
        rec.add("reflink_copy", wall, exit_code=rc, ok=True,
                note=f"rep={rep + 1}/{reps} {'cold' if rep == 0 else 'warm'}"
                     f" reflink ok files={src_count}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true",
                        help="small fast verification pass, not a headline result")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--out-dir", default="benchmark-results")
    args = parser.parse_args()

    n_files = QUICK_TREE_FILES if args.quick else TREE_FILES
    reps = QUICK_REPS if args.quick else REPS
    rec = Recorder()
    cpu_self_0 = _cpu_seconds(resource.RUSAGE_SELF)
    cpu_child_0 = _cpu_seconds(resource.RUSAGE_CHILDREN)
    t_start = time.perf_counter()

    with tempfile.TemporaryDirectory(prefix="workload-provision-") as tmpdir:
        src, src_count = make_fixture(tmpdir, n_files, args.timeout)
        for rep in range(reps):
            op_clone(rec, rep, reps, src, tmpdir, args.timeout)
            op_worktree(rec, rep, reps, src, tmpdir, args.timeout)
            op_copy(rec, rep, reps, src, src_count, tmpdir, args.timeout)
            op_tar_pipe(rec, rep, reps, src, src_count, tmpdir, args.timeout)
            op_reflink(rec, rep, reps, src, src_count, tmpdir, args.timeout)

    wall_total = time.perf_counter() - t_start
    cpu_self = _cpu_seconds(resource.RUSAGE_SELF) - cpu_self_0
    cpu_child = _cpu_seconds(resource.RUSAGE_CHILDREN) - cpu_child_0
    failures = rec.failures()
    summary = {
        "type": "summary",
        "variant": "direct",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "quick": args.quick,
        "tree_files": n_files,
        "reps": reps,
        "wall_total_s": wall_total,
        "executor_cpu_s": cpu_self,
        "child_cpu_s": cpu_child,
        "peak_rss_bytes": _maxrss_bytes(),
        "failures": len(failures),
        "by_op": rec.summarize(),
    }

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = os.path.join(args.out_dir,
                            f"workload-provision-direct-{ts}.jsonl")
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
        cold = (stats["cold_rep1_s"] * 1000 if stats["cold_rep1_s"] is not None
                else float("nan"))
        warm = (stats["warm_mean_s"] * 1000 if stats["warm_mean_s"] is not None
                else float("nan"))
        print(f"  {op:24s} n={stats['n']:5d} mean={stats['mean_s'] * 1000:8.2f}ms "
              f"p95={stats['p95_s'] * 1000:8.2f}ms "
              f"cold_rep1={cold:8.2f}ms warm_mean={warm:8.2f}ms")
    for fail in failures[:10]:
        print(f"  FAIL {fail}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
