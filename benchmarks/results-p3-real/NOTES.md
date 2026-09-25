# P3 real-repo provisioning — run record

2026-09-25. `run_workload_provision.py --repo` mode (added this round)
against three real checkouts, x3 reps each, host otherwise idle. Never
mutates the source repos (all dests under tmpdir; worktrees removed).

## Commands

```sh
python3 benchmarks/scripts/run_workload_provision.py --repo _vendor/marshmallow-testbed --out-dir benchmark-results
python3 benchmarks/scripts/run_workload_provision.py --repo _vendor/SWE-agent --out-dir benchmark-results
python3 benchmarks/scripts/run_workload_provision.py --repo . --clone-via-scratch --out-dir benchmark-results
```

Raw JSONL (gitignored): `benchmark-results/workload-provision-real-*.jsonl`.

## Results (wall seconds per rep [r1, r2, r3], child CPU mean, bytes)

marshmallow-testbed (152 files, 9.4 MB):

| op | wall | cpu | bytes |
|---|---|---|---|
| git_clone | 0.15, 0.05, 0.05 | 0.04 | 8.9 MB |
| git_worktree_add_remove | 0.11, 0.06, 0.06 | 0.04 | 0.8 MB |
| git_worktree_reset_cycle | 0.09, 0.10, 0.08 | 0.06 | 0.8 MB |
| cp -R copy | 0.14, 0.09, 0.10 | 0.06 | 9.4 MB |
| tar_pipe | 0.11, 0.09, 0.09 | 0.14 | 9.4 MB |
| reflink_copy (`cp -c`) | 0.08, 0.08, 0.08 | 0.03 | 9.4 MB |

SWE-agent vendor (482 files, 68 MB):

| op | wall | cpu | bytes |
|---|---|---|---|
| git_clone | 0.29, 0.30, 0.29 | 0.35 | 67 MB |
| git_worktree_add_remove | 0.13, 0.13, 0.16 | 0.12 | 36 MB |
| git_worktree_reset_cycle | 0.28, 0.28, 0.27 | 0.25 | 36 MB |
| cp -R copy | 0.41, 0.33, 0.32 | 0.18 | 68 MB |
| tar_pipe | 0.37, 0.33, 0.36 | 0.53 | 68 MB |
| reflink_copy | 0.25, 0.26, 0.25 | 0.11 | 68 MB |

agent-fleet-efficiency self (2.6 GB working tree incl. target/ + _vendor):

| op | wall | cpu | bytes |
|---|---|---|---|
| git_clone (via scratch) | 0.28, 0.36, 0.42 | 0.28 | 13 MB (.git only) |
| git_worktree_add_remove | 0.11, 0.13, 0.13 | 0.10 | 11 MB |
| git_worktree_reset_cycle | 0.20, 0.20, 0.19 | 0.16 | 11 MB |
| cp -R copy | 25.1, 25.9, 25.5 | 12.6 | 2618 MB |
| tar_pipe | 26.0, 26.7, 26.3 | 35.2 | 2618 MB |
| reflink_copy | 14.5, 14.3, 14.6 | 7.4 | 2618 MB |

Zero failures after the fix below; worktrees all removed.

## Interpretation

Git-native provisioning (clone/worktree) is effectively size-independent
here (0.1–0.4 s across 9 MB → 2.6 GB trees) because it never touches
untracked build output; full-tree copies scale with bytes (25 s for
2.6 GB). APFS `cp -c` reflink halves that (14.5 s) but stays two orders
of magnitude above worktree creation. A worktree add + reset cycle
(≈0.2–0.3 s) is the cheapest correct "fresh workspace" primitive
measured; it also skips the 2.6 GB of build artifacts a copy would drag
along (or require excluding).

## Harness fix this round

`cp -r` → `cp -R`: BSD `cp -r` dereferences symlinked dirs (e.g.
SWE-agent's `docs/assets/readme_assets`), materializing target files in
dest and tripping file-count verification. `-R` preserves links — the
realistic workspace-copy semantic, identical on symlink-free trees.
