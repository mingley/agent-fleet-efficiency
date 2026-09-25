# P2/P3 on real repos (2026-09-25)

Follow-up to the fixture-based P2/P3 harnesses: the same provisioning and
cache questions, measured on real checkouts and real builds.

- P3 provisioning: [results-p3-real/NOTES.md](../benchmarks/results-p3-real/NOTES.md)
  (`run_workload_provision.py --repo`)
- P2 caches: [results-p2-real/NOTES.md](../benchmarks/results-p2-real/NOTES.md)
  (`run_workload_cache_real.py`)

## Headlines

- Fresh workspace for an agent: `git worktree` + reset ≈ 0.2–0.3 s
  regardless of tree size (9 MB → 2.6 GB); full-tree copy scales with
  bytes (25 s at 2.6 GB), APFS reflink halves it (14.5 s) but stays 50x
  above worktree creation.
- Shared cargo target dir: warm no-op rebuild 0.14 s vs 10.8 s cold
  (~77x wall, ~480x CPU); single-file touch rebuild 0.67 s (16x).
- Shared uv cache: venv provisioning 9.3 → 5.1 s (~1.8x; remainder is
  venv creation + resolution + linking).
- Git `--reference` clone: 1.9 → 0.65 s wall (~3x), CPU ~7.5x, ~0 new
  object bytes.

## Scope and limits

- Single host (macOS arm64, APFS), N=3 per op, local network fetches;
  absolute times will differ on Linux/fleet hosts but the ratios (cache
  vs cold, worktree vs copy) should transfer.
- overlayfs lowerdir/upperdir (roadmap item 3) is Linux-only and was not
  measured here; needs a Linux host.
- Cold cargo builds include crates.io registry download over the live
  network (honest bootstrap cost, but network-sensitive).
