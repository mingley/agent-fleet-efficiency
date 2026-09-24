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
uv pip install aiohttp  # upstream remote.py imports it but does not declare it; needed for server-path runs
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

## Workload B

File and workspace operations (read/write at 4KiB/1MiB/100MiB, tarball
unpack, local git clone, git worktree creation, reflink probe):

```sh
uv run python benchmarks/scripts/run_workload_b.py --variant swerex-local
uv run python benchmarks/scripts/run_workload_b.py --variant direct --quick
```

`--quick` skips the 100MiB files and shrinks the unpack tree for a fast
verification pass.

## Trace format (workload D)

Agent execution traces are JSONL, one event per line: a leading
`trace_header`, then `session_start`/`session_end`, `command` (request +
observed outcome), `interrupt`, and `file_op` events.

- `schemas/trace.schema.json` — normative field types
- `fixtures/sample-trace.jsonl` — small example trace

```sh
python3 benchmarks/scripts/validate_trace.py benchmarks/fixtures/sample-trace.jsonl
```

Replay a trace against an executor and check exit-code fidelity:

```sh
uv run python benchmarks/scripts/replay_trace.py benchmarks/fixtures/replay-smoke.jsonl --variant swerex-local
```

Byte counts are recorded, not asserted; `false`-style nonzero exits
pass when the trace expects them. Capturing real SWE-agent traces is
still open (see `docs/p1-compat.md` Q7/Q8 for what to record).

## Optimized Python (P0.5)

Build a second venv from the pinned SWE-ReX plus the experimental patch
series in `patches/swerex-p0.5/` (see its README for flags and known
semantic deltas), then run any workload with that interpreter:

```sh
benchmarks/scripts/build-opt-venv.sh
SWEREX_OPT_SKIP_SYNTAX_CHECK=1 SWEREX_OPT_SINGLE_SUBMIT=1 SWEREX_OPT_NO_FIXED_SLEEP=1 \
  .venv-opt/bin/python benchmarks/scripts/run_workload_a.py --variant swerex-local
```

## Server workload (P0.5)

Remote-path microbenchmark: starts a `swerex-remote` server subprocess,
drives tiny remote commands plus one upload through it. Server and
client interpreters are selectable independently; flags apply to both
ends via the inherited environment.

```sh
.venv/bin/python benchmarks/scripts/run_workload_server.py --quick --label upstream
.venv-opt/bin/python benchmarks/scripts/run_workload_server.py --quick --label opt-plain \
  --server-python .venv-opt/bin/python
SWEREX_OPT_REUSE_SESSION=1 SWEREX_OPT_BOUNDED_IDEMPOTENCY=1 SWEREX_OPT_STREAM_UPLOAD=1 \
  .venv-opt/bin/python benchmarks/scripts/run_workload_server.py --quick --label opt-flags \
  --server-python .venv-opt/bin/python
```

Target the Rust server instead with `--server-cmd` (`{port}` and
`{token}` are substituted per run):

```sh
uv run python benchmarks/scripts/run_workload_server.py --quick --label rust \
  --server-cmd './target/release/agent-execd --host 127.0.0.1 --port {port} --auth-token {token}'
```

`check_parity.py` diffs the Rust server against upstream over 43
vectors (status, error class, exit code, content lines):

```sh
uv run python benchmarks/scripts/check_parity.py
```
