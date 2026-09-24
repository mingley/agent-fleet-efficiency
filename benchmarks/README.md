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

## Setup

From a fresh shell in the project directory:

```sh
uv venv
uv pip install 'git+https://github.com/SWE-agent/SWE-ReX@5c995c365dfb1fd5bc56fda688be5d8538f9931f'
```

The SWE-ReX revision must match [pins.md](pins.md).

## Workload A

Executor microbenchmarks against one variant:

```sh
uv run python benchmarks/scripts/run_workload_a.py --variant swerex-local
uv run python benchmarks/scripts/run_workload_a.py --variant direct --n-tiny 1000
```

Quick verification (small N, not a headline result):

```sh
uv run python benchmarks/scripts/run_workload_a.py --variant swerex-local --n-tiny 20 --concurrent 10
```

Variants: `swerex-local` (pinned upstream Python baseline), `direct`
(one subprocess per command, no persistent session).
