# P2 real-repo cache/bootstrap — run record

2026-09-25. New harness `benchmarks/scripts/run_workload_cache_real.py`,
x3 reps per op, host otherwise idle. Never touches the real tree,
`target/`, `~/.cargo`, or the shared uv cache (tmpdir source copies,
`CARGO_HOME`/`CARGO_TARGET_DIR`/`UV_CACHE_DIR` overrides).

## Commands

```sh
python3 benchmarks/scripts/run_workload_cache_real.py
```

Raw JSONL (gitignored): `benchmark-results/workload-cache-real-*.jsonl`.

## Results (wall seconds per rep, child CPU mean)

| op | wall [r1, r2, r3] | cpu mean | cache/target size |
|---|---|---|---|
| cargo_build_cold (dev profile, fresh registry+target) | 12.58, 10.07, 9.69 | 38.65 s | 494 MB target |
| cargo_build_warm_noop | 0.15, 0.13, 0.13 | 0.08 s | — |
| cargo_build_warm_touch (one execd-core file) | 0.64, 0.68, 0.67 | 2.02 s | — |
| uv_install_cold (`uv pip install -e` SWE-agent) | 10.51, 8.80, 8.63 | 7.67 s | 425 MB cache |
| uv_install_warm (same cache, fresh venv) | 6.04, 4.55, 4.58 | 3.08 s | 425 MB cache |
| git_clone_full (marshmallow @ bfd2593d) | 2.41, 1.64, 1.60 | 0.98 s | 8.1 MB .git |
| git_clone_reference (vs vendored testbed) | 0.74, 0.55, 0.65 | 0.13 s | ~0 (borrows store) |

Zero failures.

## Interpretation

Compiler caching dominates: warm cargo no-op is ~77x faster in wall
(0.14 vs 10.78 s mean) and ~480x in CPU; even a single-file touch
rebuild is 16x faster than cold. A shared uv cache roughly halves venv
provisioning (9.3 → 5.1 s; the remainder is venv creation, resolution,
and linking, not downloads). Git `--reference` cuts clone wall ~3x and
CPU ~7.5x while borrowing the object store. For disposable agents on one
host, shared target/package/object dirs remove nearly all repeated
setup CPU.
