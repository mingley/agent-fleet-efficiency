#!/usr/bin/env python3
"""Summarize a benchmark matrix run (stdlib only).

Reads benchmark-results/matrix-<ts>/ (default: newest), prints arm
comparison tables, and exits nonzero if any arm reported failures.

Usage:
    python3 benchmarks/scripts/summarize_matrix.py [matrix-dir]
"""

import glob
import json
import os
import sys


def load_summaries(matrix_dir):
    arms = {}
    for path in sorted(glob.glob(os.path.join(matrix_dir, "*.jsonl"))):
        name = os.path.basename(path)
        if name == "manifest.jsonl":
            continue
        with open(path) as f:
            lines = [json.loads(line) for line in f if line.strip()]
        summaries = [l for l in lines if l.get("type") == "summary"]
        if summaries:
            arms[name[: -len(".jsonl")]] = summaries[-1]
    return arms


def overview(arms):
    print(f"{'arm':14s} {'wall_s':>8s} {'exec_cpu':>9s} {'child_cpu':>10s} {'rss_mb':>8s} {'fail':>5s}")
    total_fail = 0
    for arm in sorted(arms):
        s = arms[arm]
        total_fail += s.get("failures", 0)
        print(f"{arm:14s} {s['wall_total_s']:8.1f} {s.get('executor_cpu_s', 0):9.2f} "
              f"{s.get('child_cpu_s', 0):10.2f} {s.get('peak_rss_bytes', 0) / 1e6:8.1f} "
              f"{s.get('failures', 0):5d}")
    return total_fail


def op_table(arms, prefix, ops):
    names = sorted(a for a in arms if a.startswith(prefix))
    if not names:
        return
    print(f"\n## {prefix} ops (mean ms)")
    header = f"  {'op':24s}" + "".join(f"{a:>14s}" for a in names)
    print(header)
    for op in ops:
        row = f"  {op:24s}"
        for a in names:
            b = arms[a]["by_op"].get(op)
            row += f"{(b['mean_s'] * 1000 if b else float('nan')):14.2f}"
        print(row)


def attribution(arms):
    base = arms.get("a-upstream", {}).get("by_op", {}).get("tiny_true", {}).get("mean_s")
    if not base:
        return
    print("\n## workload-A tiny_true vs upstream (mean ms, n=1000)")
    for a in sorted(k for k in arms if k.startswith("a-")):
        t = arms[a]["by_op"].get("tiny_true", {}).get("mean_s")
        if t is None:
            continue
        print(f"  {a:14s} {t * 1000:8.2f}  ({(t - base) * 1000:+8.2f} vs upstream)")


def main():
    if len(sys.argv) > 1:
        matrix_dir = sys.argv[1]
    else:
        cands = sorted(glob.glob("benchmark-results/matrix-*/"))
        if not cands:
            print("no matrix dir found", file=sys.stderr)
            return 2
        matrix_dir = cands[-1]
    manifest = os.path.join(matrix_dir, "manifest.jsonl")
    if os.path.exists(manifest):
        print(f"## manifest ({matrix_dir})")
        for line in open(manifest):
            m = json.loads(line)
            print(f"  {m['arm']:14s} {m['interpreter']} flags=[{m['flags'] or 'none'}]")
        print()
    arms = load_summaries(matrix_dir)
    fails = overview(arms)
    attribution(arms)
    op_table(arms, "a-", ["session_create_close", "tiny_true", "pwd", "output_1024",
                           "output_65536", "output_1048576", "concurrent_true_total"])
    op_table(arms, "b-", ["file_write_1048576", "file_read_1048576", "file_write_104857600",
                           "file_read_104857600", "unpack_tarball", "git_clone_local",
                           "git_worktree_add", "reflink_copy"])
    op_table(arms, "s-", ["server_startup", "session_create", "remote_tiny_true",
                           "remote_upload_10mib", "remote_verify_upload", "session_close"])
    print(f"\ntotal failures: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
