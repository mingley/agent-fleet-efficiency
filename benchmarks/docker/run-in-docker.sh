#!/usr/bin/env bash
# Build the Linux comparison image and run the benchmark harness in it.
#
# Runs record_host.py plus workload A (both variants, small-N smoke flags),
# writing artifacts to the host's gitignored benchmark-results/ via a mount.
#
# Usage (from anywhere; paths resolve to the repo root):
#   benchmarks/docker/run-in-docker.sh
#   PLATFORM=linux/arm64 benchmarks/docker/run-in-docker.sh   # native on ARM hosts
#   IMAGE=my-bench N_TINY=50 CONCURRENT=20 benchmarks/docker/run-in-docker.sh
#
# Headline runs must match the target fleet arch (linux/amd64); on ARM hosts
# that build goes through buildx emulation, so expect it to be slower.
set -euo pipefail

IMAGE="${IMAGE:-fleet-bench}"
PLATFORM="${PLATFORM:-linux/amd64}"
N_TINY="${N_TINY:-20}"
CONCURRENT="${CONCURRENT:-10}"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"

mkdir -p benchmark-results

echo "==> building $IMAGE for $PLATFORM"
docker buildx build --platform "$PLATFORM" \
    -f benchmarks/docker/Dockerfile \
    -t "$IMAGE" \
    --load .

run() {
    docker run --rm --platform "$PLATFORM" \
        -v "$ROOT/benchmark-results:/results" \
        "$IMAGE" "$@"
}

echo "==> record_host.py"
run python3 /opt/harness/scripts/record_host.py --out /results/host.docker.json

echo "==> workload A --variant swerex-local (n-tiny=$N_TINY concurrent=$CONCURRENT)"
run python3 /opt/harness/scripts/run_workload_a.py \
    --variant swerex-local --n-tiny "$N_TINY" --concurrent "$CONCURRENT" \
    --out-dir /results

echo "==> workload A --variant direct (n-tiny=$N_TINY concurrent=$CONCURRENT)"
run python3 /opt/harness/scripts/run_workload_a.py \
    --variant direct --n-tiny "$N_TINY" --concurrent "$CONCURRENT" \
    --out-dir /results

echo "==> done; artifacts in benchmark-results/"
ls -la benchmark-results/
