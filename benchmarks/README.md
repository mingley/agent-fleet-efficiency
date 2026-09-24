# Benchmark Harness

Reproducible execution-plane benchmarks for the P0 roadmap item: measure
before rewriting. Full methodology lives in
[docs/benchmark-plan.md](../docs/benchmark-plan.md).

## Layout

- `scripts/` — runnable harness scripts (stdlib-only Python unless noted)
- `pins.md` — pinned third-party revisions every result must cite
- `../benchmark-results/` (gitignored) — raw per-run artifacts, never committed

## Running

Record the host once per machine (required alongside every result):

```sh
python3 benchmarks/scripts/record_host.py --out benchmark-results/host.json
```

## Conventions

- Every run records host facts, workload, variant, and exact dependency SHAs.
- Report absolute CPU-seconds and RSS, executor CPU separate from child CPU.
- Cold and warm results are separate runs; never blend them.
- Do not extrapolate fleet savings from microbenchmarks without trace replay.
