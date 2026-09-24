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

## Fleet-arch note

Headline runs must match the target fleet architecture (`linux/amd64`). On
ARM hosts (e.g. Apple Silicon) the default build targets `linux/amd64` via
buildx emulation, which works but is slower; `PLATFORM=linux/arm64` selects a
fast native build for iteration only — do not report those numbers as
headlines.
