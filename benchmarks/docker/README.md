# Linux Docker comparison environment

Runs the benchmark harness (`benchmarks/scripts/`) on Linux in a container so
results stay comparable across developer hosts. Raw artifacts land in the
host's gitignored `benchmark-results/` via a bind mount.

## Build

From the repo root (build context must be the repo root):

```sh
docker buildx build --platform linux/amd64 \
  -f benchmarks/docker/Dockerfile -t fleet-bench --load .
```

## Run

One command builds the image and runs `record_host.py` plus workload A
(`--variant swerex-local` and `--variant direct`, small-N smoke flags):

```sh
benchmarks/docker/run-in-docker.sh
```

Environment overrides: `IMAGE` (default `fleet-bench`),
`PLATFORM` (default `linux/amd64`), `N_TINY` (default `20`),
`CONCURRENT` (default `10`).

To run a single step manually after building:

```sh
docker run --rm --platform linux/amd64 \
  -v "$PWD/benchmark-results:/results" fleet-bench \
  python3 /opt/harness/scripts/record_host.py --out /results/host.docker.json
```

The image carries two environments: the system python has the pinned
upstream SWE-ReX, and `/opt/venv-opt` has the same pin plus the P0.5
patch series from `benchmarks/patches/swerex-p0.5/`. It also builds
`agent-execd` for the target platform at `/opt/execd/agent-execd`
(multi-stage rust builder, deps pinned by `Cargo.lock`).

Linux gate rerun (N=200, linux/arm64): upstream tiny 117.73ms vs
rust tiny 0.48ms, session create 364.65ms vs 2.20ms, all 0 failures
(`benchmark-results/linux-gate/`, gitignored).

## Full matrix

`run-matrix.sh` builds the image and runs the whole comparison: workload
A at full N with per-flag attribution arms, workload B full, and the
server workload across interpreters. One directory per run:

```sh
benchmarks/docker/run-matrix.sh
```

Results land in `benchmark-results/matrix-<ts>/` with `manifest.jsonl`
(mapping each arm to interpreter + flags) and `matrix.json` (platform,
image, pin, container info). Summarize any run with:

```sh
python3 benchmarks/scripts/summarize_matrix.py benchmark-results/matrix-<ts>/
``` Overrides: `IMAGE`, `PLATFORM` (default
`linux/arm64` for speed; use `linux/amd64` for fleet-arch headlines),
`N_TINY` (default `1000`), `CONCURRENT` (default `100`),
`N_TINY_SERVER` (default `50`), `UPLOAD_MIB` (default `10`). Keep the
host otherwise idle while the matrix runs. Console output is tee'd to
`matrix.log` in the run directory; resume an interrupted run with
`MATRIX_TS=<ts> benchmarks/docker/run-matrix.sh` (completed arms are
skipped).

## Fleet-arch note

Headline runs must match the target fleet architecture (`linux/amd64`). On
ARM hosts (e.g. Apple Silicon) the default build targets `linux/amd64` via
buildx emulation, which works but is slower; `PLATFORM=linux/arm64` selects a
fast native build for iteration only — do not report those numbers as
headlines.
