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

Real-repo mode (ROADMAP P3 follow-up) runs the SAME ops (plus a
worktree reset-cycle op) against an existing git checkout, never
mutating it — all dest dirs live under a tmpdir:

    python3 benchmarks/scripts/run_workload_provision.py --repo _vendor/SWE-agent
    python3 benchmarks/scripts/run_workload_provision.py --repo . --clone-via-scratch
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

    def add(self, op, wall_s, out_bytes=0, exit_code=0, ok=True, note="",
              cpu_s=0.0, n_bytes=0):
        self.ops.append(
            {
                "op": op,
                "wall_s": wall_s,
                "cpu_s": cpu_s,
                "n_bytes": n_bytes,
                "out_bytes": out_bytes,
                "exit_code": exit_code,
                "ok": ok,
                "note": note,
            }
        )

    def summarize(self):
        by_op = {}
        for rec in self.ops:
            by_op.setdefault(rec["op"], []).append(rec)
        summary = {}
        for op, recs in by_op.items():
            walls = [r["wall_s"] for r in recs]  # rep1, rep2, rep3 in run order
            cpus = [r.get("cpu_s", 0.0) for r in recs]
            nbytes = [r.get("n_bytes", 0) for r in recs]
            rep_order = list(walls)
            walls = sorted(walls)
            warm = rep_order[1:]
            warm_cpu = cpus[1:]
            warm_bytes = nbytes[1:]
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
                "mean_cpu_s": sum(cpus) / len(cpus) if cpus else None,
                "cold_rep1_cpu_s": cpus[0] if cpus else None,
                "warm_mean_cpu_s": ((sum(warm_cpu) / len(warm_cpu))
                                    if warm_cpu else None),
                "mean_bytes": (sum(nbytes) / len(nbytes)) if nbytes else None,
                "cold_rep1_bytes": nbytes[0] if nbytes else None,
                "warm_mean_bytes": ((sum(warm_bytes) / len(warm_bytes))
                                    if warm_bytes else None),
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


def _count_and_bytes(root):
    """Single-walk (file count, byte size) for a tree. Never follows symlinks."""
    n, total = 0, 0
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for fn in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, fn)).st_size
            except OSError:
                continue
            n += 1
    return n, total


def _dir_bytes(root):
    return _count_and_bytes(root)[1]


def _child_cpu():
    return _cpu_seconds(resource.RUSAGE_CHILDREN)


def _repo_head(src):
    """Fail fast if src is not a usable git repo. Returns HEAD SHA."""
    rc, out = _run(["git", "-C", str(src), "rev-parse", "HEAD"], 60)
    if rc != 0 or not out.strip():
        raise RuntimeError(
            f"--repo target is not a git repo: {src}: {out.strip()[-300:]}")
    return out.strip().split()[0]


def _inside(inner, outer):
    try:
        Path(inner).resolve().relative_to(Path(outer).resolve())
        return True
    except ValueError:
        return False


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


def op_clone(rec, rep, reps, src, tmpdir, timeout, expect_head=None):
    dest = os.path.join(tmpdir, f"clone-{rep}")
    c0 = _child_cpu()
    t0 = time.perf_counter()
    try:
        rc, out = _run(["git", "clone", "--quiet", str(src), dest], timeout)
        ok = rc == 0
        note = ""
    except Exception as e:  # noqa: BLE001 - record failure, keep going
        wall = time.perf_counter() - t0
        rec.add("git_clone", wall, ok=False, cpu_s=_child_cpu() - c0,
                note=f"rep={rep + 1}/{reps} {type(e).__name__}: {e}")
        return
    wall = time.perf_counter() - t0
    cpu = _child_cpu() - c0
    nbytes = _dir_bytes(dest) if (ok and os.path.isdir(dest)) else 0
    if ok and not (os.path.isdir(dest) and len(os.listdir(dest)) > 0):
        ok = False
        note = f"rep={rep + 1}/{reps} clone dest looks empty"
    else:
        note = f"rep={rep + 1}/{reps} {'cold' if rep == 0 else 'warm'}"
        if not ok:
            note += f" {out.strip()[-200:]}"
        elif expect_head:
            rc2, out2 = _run(["git", "-C", dest, "rev-parse", "HEAD"], timeout)
            got = out2.strip().split()[0] if rc2 == 0 and out2.strip() else "?"
            if got != expect_head:
                ok = False
                note += f" HEAD {got[:12]} != {expect_head[:12]}"
    rec.add("git_clone", wall, exit_code=rc, ok=ok, note=note,
            cpu_s=cpu, n_bytes=nbytes)


def op_worktree(rec, rep, reps, src, tmpdir, timeout):
    wt = os.path.join(tmpdir, f"wt-{rep}")
    c0 = _child_cpu()
    t0 = time.perf_counter()
    try:
        rc, out = _run(["git", "-C", str(src), "worktree", "add", "--detach",
                        wt, "HEAD"], timeout)
        add_wall = time.perf_counter() - t0
        # Size probe runs outside the timed region (before remove).
        nbytes = _dir_bytes(wt) if (rc == 0 and os.path.isdir(wt)) else 0
        t1 = time.perf_counter()
        rm_rc, rm_out = _run(
            ["git", "-C", str(src), "worktree", "remove", "--force", wt],
            timeout) if rc == 0 else (0, "")
        wall = add_wall + (time.perf_counter() - t1)
        cpu = _child_cpu() - c0
        ok = rc == 0 and rm_rc == 0
        detail = "" if ok else f" add={out.strip()[-150:]} rm={rm_out.strip()[-150:]}"
    except Exception as e:  # noqa: BLE001 - record failure, keep going
        rec.add("git_worktree_add_remove", time.perf_counter() - t0, ok=False,
                cpu_s=_child_cpu() - c0,
                note=f"rep={rep + 1}/{reps} {type(e).__name__}: {e}")
        return
    rec.add("git_worktree_add_remove", wall,
            exit_code=rc, ok=ok, cpu_s=cpu, n_bytes=nbytes,
            note=f"rep={rep + 1}/{reps} {'cold' if rep == 0 else 'warm'}{detail}")


def op_worktree_reset_cycle(rec, rep, reps, src, tmpdir, timeout):
    """Repo mode: worktree add + reset --hard HEAD inside it + remove.

    Approximates agent workspace reset latency (fresh checkout of HEAD
    followed by a hard reset cycle). Untimed size probe before remove.
    """
    wt = os.path.join(tmpdir, f"wt-reset-{rep}")
    c0 = _child_cpu()
    t0 = time.perf_counter()
    r_rc, r_out, reset_wall, nbytes = 0, "", 0.0, 0
    try:
        rc, out = _run(["git", "-C", str(src), "worktree", "add", "--detach",
                        wt, "HEAD"], timeout)
        add_wall = time.perf_counter() - t0
        if rc == 0:
            tr0 = time.perf_counter()
            r_rc, r_out = _run(["git", "-C", wt, "reset", "--hard",
                                "--quiet", "HEAD"], timeout)
            reset_wall = time.perf_counter() - tr0
            nbytes = _dir_bytes(wt) if os.path.isdir(wt) else 0
        t1 = time.perf_counter()
        rm_rc, rm_out = _run(
            ["git", "-C", str(src), "worktree", "remove", "--force", wt],
            timeout) if rc == 0 else (0, "")
        wall = add_wall + reset_wall + (time.perf_counter() - t1)
        cpu = _child_cpu() - c0
        ok = rc == 0 and r_rc == 0 and rm_rc == 0
        detail = ("" if ok else
                  f" add={out.strip()[-100:]} reset={r_out.strip()[-100:]}"
                  f" rm={rm_out.strip()[-100:]}")
    except Exception as e:  # noqa: BLE001 - record failure, keep going
        rec.add("git_worktree_reset_cycle", time.perf_counter() - t0, ok=False,
                cpu_s=_child_cpu() - c0,
                note=f"rep={rep + 1}/{reps} {type(e).__name__}: {e}")
        return
    rec.add("git_worktree_reset_cycle", wall,
            exit_code=rc, ok=ok, cpu_s=cpu, n_bytes=nbytes,
            note=(f"rep={rep + 1}/{reps} {'cold' if rep == 0 else 'warm'}"
                  f" reset_ms={reset_wall * 1000:.1f}{detail}"))


def _verify_copy(src, dest_root, src_count):
    """Compare dest tree against src. Returns (ok, got, nbytes, detail).

    src_count=None (repo mode) recounts src fresh, since a live checkout's
    .git/worktrees metadata shifts as worktree ops run.
    """
    if src_count is None:
        src_count, _ = _count_and_bytes(src)
    got, nbytes = _count_and_bytes(dest_root)
    if got != src_count:
        return False, got, nbytes, f" file count {got} != {src_count}"
    return True, got, nbytes, f" files={got}"


def op_copy(rec, rep, reps, src, src_count, tmpdir, timeout):
    dest = os.path.join(tmpdir, f"copy-{rep}")
    c0 = _child_cpu()
    t0 = time.perf_counter()
    try:
        # -R (not -r): preserve symlinked dirs instead of materializing
        # their targets (BSD cp -r dereferences, inflating dest file counts
        # on real repos; identical to -r on symlink-free trees).
        rc, out = _run(["cp", "-R", str(src), dest], timeout)
        ok = rc == 0
        detail = ""
    except Exception as e:  # noqa: BLE001 - record failure, keep going
        rec.add("cp_r_copy", time.perf_counter() - t0, ok=False,
                cpu_s=_child_cpu() - c0,
                note=f"rep={rep + 1}/{reps} {type(e).__name__}: {e}")
        return
    wall = time.perf_counter() - t0
    cpu = _child_cpu() - c0
    nbytes = 0
    if ok:
        ok, _got, nbytes, detail = _verify_copy(src, dest, src_count)
    else:
        detail = f" {out.strip()[-200:]}"
    rec.add("cp_r_copy", wall, exit_code=rc, ok=ok,
            cpu_s=cpu, n_bytes=nbytes,
            note=f"rep={rep + 1}/{reps} {'cold' if rep == 0 else 'warm'}{detail}")


def op_tar_pipe(rec, rep, reps, src, src_count, tmpdir, timeout):
    destdir = os.path.join(tmpdir, f"tarpipe-{rep}")
    os.makedirs(destdir, exist_ok=True)
    if _inside(src, tmpdir):
        # Fixture layout: src lives in tmpdir; archive it by name.
        src = Path(src)
        cmd = (f"tar -cf - -C {shlex.quote(tmpdir)} {shlex.quote(src.name)}"
               f" | tar -xf - -C {shlex.quote(destdir)}")
        expect_root = os.path.join(destdir, src.name)
    else:
        # Repo mode: src is an outside path; stream its contents.
        cmd = (f"tar -cf - -C {shlex.quote(str(src))} ."
               f" | tar -xf - -C {shlex.quote(destdir)}")
        expect_root = destdir
    c0 = _child_cpu()
    t0 = time.perf_counter()
    try:
        rc, out = _run(cmd, timeout, shell=True)
        ok = rc == 0
        detail = ""
    except Exception as e:  # noqa: BLE001 - record failure, keep going
        rec.add("tar_pipe", time.perf_counter() - t0, ok=False,
                cpu_s=_child_cpu() - c0,
                note=f"rep={rep + 1}/{reps} {type(e).__name__}: {e}")
        return
    wall = time.perf_counter() - t0
    cpu = _child_cpu() - c0
    nbytes = 0
    if ok:
        ok, _got, nbytes, detail = _verify_copy(src, expect_root, src_count)
    else:
        detail = f" {out.strip()[-200:]}"
    rec.add("tar_pipe", wall, exit_code=rc, ok=ok,
            cpu_s=cpu, n_bytes=nbytes,
            note=f"rep={rep + 1}/{reps} {'cold' if rep == 0 else 'warm'}{detail}")


def op_reflink(rec, rep, reps, src, src_count, tmpdir, timeout):
    # Best-effort probe: filesystems without clone support report
    # ok=True/skipped, never a harness failure.
    dest = os.path.join(tmpdir, f"reflink-{rep}")
    if sys.platform == "darwin":
        cmd = ["cp", "-c", "-R", str(src), dest]
    else:
        cmd = ["cp", "--reflink=always", "-r", str(src), dest]
    c0 = _child_cpu()
    t0 = time.perf_counter()
    try:
        rc, out = _run(cmd, timeout)
    except Exception as e:  # noqa: BLE001 - unsupported counts as skipped
        rec.add("reflink_copy", time.perf_counter() - t0, ok=True,
                cpu_s=_child_cpu() - c0,
                note=f"rep={rep + 1}/{reps} skipped: {type(e).__name__}: {e}")
        return
    wall = time.perf_counter() - t0
    cpu = _child_cpu() - c0
    if rc != 0:
        rec.add("reflink_copy", wall, exit_code=rc, ok=True, cpu_s=cpu,
                note=f"rep={rep + 1}/{reps} skipped: {out.strip()[-200:]}")
        return
    if src_count is None:
        src_count, _ = _count_and_bytes(src)
    got, nbytes = _count_and_bytes(dest)
    if got != src_count:
        rec.add("reflink_copy", wall, exit_code=rc, ok=True, cpu_s=cpu,
                note=(f"rep={rep + 1}/{reps} skipped: dest file count"
                      f" {got} != {src_count}"))
    else:
        rec.add("reflink_copy", wall, exit_code=rc, ok=True,
                cpu_s=cpu, n_bytes=nbytes,
                note=f"rep={rep + 1}/{reps} {'cold' if rep == 0 else 'warm'}"
                     f" reflink ok files={got}")


def _finalize(rec, args, extra, prefix, t_start, cpu_self_0, cpu_child_0):
    wall_total = time.perf_counter() - t_start
    cpu_self = _cpu_seconds(resource.RUSAGE_SELF) - cpu_self_0
    cpu_child = _cpu_seconds(resource.RUSAGE_CHILDREN) - cpu_child_0
    failures = rec.failures()
    summary = {
        "type": "summary",
        "variant": "direct",
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        **extra,
        "wall_total_s": wall_total,
        "executor_cpu_s": cpu_self,
        "child_cpu_s": cpu_child,
        "peak_rss_bytes": _maxrss_bytes(),
        "failures": len(failures),
        "by_op": rec.summarize(),
    }

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = os.path.join(args.out_dir, f"{prefix}-{ts}.jsonl")
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
        cold_cpu = (stats["cold_rep1_cpu_s"] * 1000
                    if stats.get("cold_rep1_cpu_s") is not None
                    else float("nan"))
        warm_cpu = (stats["warm_mean_cpu_s"] * 1000
                    if stats.get("warm_mean_cpu_s") is not None
                    else float("nan"))
        mb = ((stats.get("mean_bytes") or 0) / 1e6)
        print(f"  {op:24s} n={stats['n']:5d} mean={stats['mean_s'] * 1000:8.2f}ms "
              f"p95={stats['p95_s'] * 1000:8.2f}ms "
              f"cold_rep1={cold:8.2f}ms warm_mean={warm:8.2f}ms "
              f"cold_cpu={cold_cpu:8.2f}ms warm_cpu={warm_cpu:8.2f}ms "
              f"bytes={mb:.1f}MB")
    for fail in failures[:10]:
        print(f"  FAIL {fail}")
    return 1 if failures else 0


def run_repo_mode(args, reps):
    """Same ops as fixture mode against a real repo; never mutates it.

    All dest dirs live under a tmpdir. Worktrees are added+removed per rep
    and pruned at the end; a leftover check is recorded as an op failure.
    """
    repo = Path(args.repo).resolve()
    head = _repo_head(repo)
    tree_files, tree_bytes = _count_and_bytes(repo)
    print(f"repo={repo} head={head[:12]} tree_files={tree_files} "
          f"tree_bytes={tree_bytes / 1e6:.1f}MB")
    slug = repo.name.replace(" ", "_") or "repo"
    rec = Recorder()

    with tempfile.TemporaryDirectory(prefix="workload-provision-real-") as tmpdir:
        clone_src = (Path(args.clone_source).resolve()
                     if args.clone_source else repo)
        if args.clone_via_scratch:
            # Unmeasured setup: one scratch clone becomes the clone source so
            # the timed clone reps never touch the live checkout's .git.
            scratch = Path(tmpdir) / "scratch-src"
            rc, out = _run(["git", "clone", "--quiet", str(repo), str(scratch)],
                           args.timeout)
            if rc != 0:
                raise RuntimeError(f"scratch clone failed: {out[-500:]}")
            clone_src = scratch
        cpu_self_0 = _cpu_seconds(resource.RUSAGE_SELF)
        cpu_child_0 = _cpu_seconds(resource.RUSAGE_CHILDREN)
        t_start = time.perf_counter()
        try:
            for rep in range(reps):
                op_clone(rec, rep, reps, clone_src, tmpdir, args.timeout,
                         expect_head=head)
                op_worktree(rec, rep, reps, repo, tmpdir, args.timeout)
                op_worktree_reset_cycle(rec, rep, reps, repo, tmpdir,
                                        args.timeout)
                op_copy(rec, rep, reps, repo, None, tmpdir, args.timeout)
                op_tar_pipe(rec, rep, reps, repo, None, tmpdir, args.timeout)
                op_reflink(rec, rep, reps, repo, None, tmpdir, args.timeout)
        finally:
            _run(["git", "-C", str(repo), "worktree", "prune"], args.timeout)
        rc, out = _run(["git", "-C", str(repo), "worktree", "list",
                        "--porcelain"], args.timeout)
        leftovers = [ln for ln in out.splitlines()
                     if ln.startswith("worktree ") and tmpdir in ln]
        if leftovers:
            rec.add("cleanup_check", 0.0, ok=False,
                    note=f"leftover worktrees: {leftovers}")

    extra = {
        "mode": "real-repo",
        "repo": str(repo),
        "repo_head": head,
        "clone_source": str(clone_src),
        "quick": args.quick,
        "tree_files": tree_files,
        "tree_bytes": tree_bytes,
        "reps": reps,
    }
    return _finalize(rec, args, extra, f"workload-provision-real-{slug}",
                     t_start, cpu_self_0, cpu_child_0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quick", action="store_true",
                        help="small fast verification pass, not a headline result")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--out-dir", default="benchmark-results")
    parser.add_argument("--repo", default=None, metavar="PATH",
                        help="real-repo mode: run the same ops against the git "
                             "checkout at PATH (never mutated; dests in tmpdir)")
    parser.add_argument("--clone-source", default=None, metavar="PATH",
                        help="repo mode: clone FROM this path instead of --repo")
    parser.add_argument("--clone-via-scratch", action="store_true",
                        help="repo mode: make one unmeasured scratch clone in "
                             "tmpdir first and use it as the clone source")
    args = parser.parse_args()

    reps = QUICK_REPS if args.quick else REPS
    if args.repo is not None:
        return run_repo_mode(args, reps)

    n_files = QUICK_TREE_FILES if args.quick else TREE_FILES
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

    extra = {
        "mode": "fixture",
        "quick": args.quick,
        "tree_files": n_files,
        "reps": reps,
    }
    return _finalize(rec, args, extra, "workload-provision-direct",
                     t_start, cpu_self_0, cpu_child_0)


if __name__ == "__main__":
    raise SystemExit(main())
