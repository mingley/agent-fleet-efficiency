#!/usr/bin/env bash
# Full benchmark matrix for the P0/P0.5 gates.
#
# Builds the dual-env image once (upstream system python + /opt/venv-opt with
# the P0.5 patch series), then runs workload A (full-N plus per-flag
# attribution arms), workload B (full), and the server workload across
# interpreters. Results land in benchmark-results/matrix-<ts>/ with a
# manifest. Nothing is committed; benchmark-results/ is gitignored.
#
# Usage (from anywhere; paths resolve to the repo root):
#   benchmarks/docker/run-matrix.sh
#   PLATFORM=linux/amd64 benchmarks/docker/run-matrix.sh   # fleet arch, emulated on ARM hosts
#   N_TINY=200 CONCURRENT=20 benchmarks/docker/run-matrix.sh  # quick matrix
#
# The host must be otherwise idle: competing CPU load skews the numbers.
set -euo pipefail

IMAGE="${IMAGE:-fleet-bench}"
PLATFORM="${PLATFORM:-linux/arm64}"
N_TINY="${N_TINY:-1000}"
CONCURRENT="${CONCURRENT:-100}"
N_TINY_SERVER="${N_TINY_SERVER:-50}"
UPLOAD_MIB="${UPLOAD_MIB:-10}"

# Never inherit stray flags from the calling shell; arms set their own.
unset SWEREX_OPT_SKIP_SYNTAX_CHECK SWEREX_OPT_SINGLE_SUBMIT SWEREX_OPT_NO_FIXED_SLEEP
unset SWEREX_OPT_REUSE_SESSION SWEREX_OPT_BOUNDED_IDEMPOTENCY SWEREX_OPT_STREAM_UPLOAD

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
# MATRIX_TS reuses an existing run directory (resume); completed arms are
# skipped by run_arm. Full console output is tee'd to matrix.log so an
# interrupted run keeps its diagnostics.
TS="${MATRIX_TS:-$(date -u +%Y%m%dT%H%M%SZ)}"
RESULTS_DIR="benchmark-results/matrix-$TS"
mkdir -p "$RESULTS_DIR"
LOG="$RESULTS_DIR/matrix.log"
exec > >(tee -a "$LOG") 2>&1
echo "==> run $TS (log: $LOG)"

SYS_PY=/usr/local/bin/python
OPT_PY=/opt/venv-opt/bin/python
OUT=/results/matrix-$TS

echo "==> building $IMAGE for $PLATFORM"
docker buildx build --platform "$PLATFORM" \
    -f benchmarks/docker/Dockerfile \
    -t "$IMAGE" \
    --load . 2>&1 | tail -3

rundock() {
    docker run --rm --platform "$PLATFORM" \
        -v "$ROOT/benchmark-results:/results" \
        -e SWEREX_OPT_SKIP_SYNTAX_CHECK -e SWEREX_OPT_SINGLE_SUBMIT -e SWEREX_OPT_NO_FIXED_SLEEP \
        -e SWEREX_OPT_REUSE_SESSION -e SWEREX_OPT_BOUNDED_IDEMPOTENCY -e SWEREX_OPT_STREAM_UPLOAD \
        "$IMAGE" "$@"
}

manifest() { # arm interpreter flags
    python3 -c 'import json,sys; print(json.dumps({"arm":sys.argv[1],"interpreter":sys.argv[2],"flags":sys.argv[3],"file":sys.argv[1]+".jsonl"}))' \
        "$1" "$2" "$3" >> "$RESULTS_DIR/manifest.jsonl"
}

run_arm() { # arm interpreter flags... -- command...
    local arm="$1" interp="$2" flags="$3"
    shift 3
    [ "${1:-}" = "--" ] && shift
    if [ -f "$RESULTS_DIR/$arm.jsonl" ]; then
        echo "==> arm $arm already done, skipping"
        return 0
    fi
    echo "==> arm $arm ($interp flags: ${flags:-none})"
    local before after newfile
    before=$(ls "$RESULTS_DIR")
    "$@"
    after=$(ls "$RESULTS_DIR")
    newfile=$(comm -13 <(echo "$before" | sort) <(echo "$after" | sort) | head -1)
    [ -n "$newfile" ] || { echo "error: no new result file for $arm" >&2; exit 1; }
    mv "$RESULTS_DIR/$newfile" "$RESULTS_DIR/$arm.jsonl"
    manifest "$arm" "$interp" "$flags"
    echo "==> $arm -> $arm.jsonl"
}

python3 - "$TS" "$PLATFORM" "$IMAGE" > "$RESULTS_DIR/matrix.json" <<'EOF'
import json, sys
print(json.dumps({
    "ts": sys.argv[1], "platform": sys.argv[2], "image": sys.argv[3],
    "swerex_pin": "5c995c365dfb1fd5bc56fda688be5d8538f9931f",
    "container": "CONTAINER_INFO",
}, indent=2))
EOF
INFO="$(rundock sh -c 'echo "nproc=$(nproc) $(uname -srm) $(python3 --version 2>&1)"')"
python3 - "$RESULTS_DIR/matrix.json" "$INFO" <<'EOF'
import json, sys
p = sys.argv[1]
d = json.load(open(p))
d["container"] = sys.argv[2]
json.dump(d, open(p, "w"), indent=2)
EOF

echo "==> host record"
rundock python3 /opt/harness/scripts/record_host.py --out "$OUT/host.docker.json"

A=(/opt/harness/scripts/run_workload_a.py --variant swerex-local
   --n-tiny "$N_TINY" --concurrent "$CONCURRENT" --out-dir "$OUT")
run_arm a-upstream "$SYS_PY" "" -- rundock "$SYS_PY" "${A[@]}"
run_arm a-opt-plain "$OPT_PY" "" -- rundock "$OPT_PY" "${A[@]}"
( export SWEREX_OPT_SKIP_SYNTAX_CHECK=1 SWEREX_OPT_SINGLE_SUBMIT=1 SWEREX_OPT_NO_FIXED_SLEEP=1
  run_arm a-opt-all "$OPT_PY" "SKIP_SYNTAX_CHECK SINGLE_SUBMIT NO_FIXED_SLEEP" -- rundock "$OPT_PY" "${A[@]}" )
( export SWEREX_OPT_SKIP_SYNTAX_CHECK=1
  run_arm a-opt-0001 "$OPT_PY" "SKIP_SYNTAX_CHECK" -- rundock "$OPT_PY" "${A[@]}" )
( export SWEREX_OPT_SINGLE_SUBMIT=1
  run_arm a-opt-0002 "$OPT_PY" "SINGLE_SUBMIT" -- rundock "$OPT_PY" "${A[@]}" )
( export SWEREX_OPT_NO_FIXED_SLEEP=1
  run_arm a-opt-0003 "$OPT_PY" "NO_FIXED_SLEEP" -- rundock "$OPT_PY" "${A[@]}" )
run_arm a-direct "$SYS_PY" "" -- \
    rundock "$SYS_PY" /opt/harness/scripts/run_workload_a.py --variant direct \
    --n-tiny "$N_TINY" --concurrent "$CONCURRENT" --out-dir "$OUT"

B=(/opt/harness/scripts/run_workload_b.py --variant swerex-local --out-dir "$OUT")
run_arm b-upstream "$SYS_PY" "" -- rundock "$SYS_PY" "${B[@]}"
run_arm b-opt-plain "$OPT_PY" "" -- rundock "$OPT_PY" "${B[@]}"
( export SWEREX_OPT_SKIP_SYNTAX_CHECK=1 SWEREX_OPT_SINGLE_SUBMIT=1 SWEREX_OPT_NO_FIXED_SLEEP=1
  run_arm b-opt-all "$OPT_PY" "SKIP_SYNTAX_CHECK SINGLE_SUBMIT NO_FIXED_SLEEP" -- rundock "$OPT_PY" "${B[@]}" )

S=(/opt/harness/scripts/run_workload_server.py
   --n-tiny "$N_TINY_SERVER" --upload-mib "$UPLOAD_MIB" --out-dir "$OUT")
run_arm s-upstream "$SYS_PY" "" -- rundock "$SYS_PY" "${S[@]}" --server-python "$SYS_PY" --label s-upstream
run_arm s-opt-plain "$OPT_PY" "" -- rundock "$OPT_PY" "${S[@]}" --server-python "$OPT_PY" --label s-opt-plain
( export SWEREX_OPT_REUSE_SESSION=1 SWEREX_OPT_BOUNDED_IDEMPOTENCY=1 SWEREX_OPT_STREAM_UPLOAD=1
  run_arm s-opt-all "$OPT_PY" "REUSE_SESSION BOUNDED_IDEMPOTENCY STREAM_UPLOAD" -- \
    rundock "$OPT_PY" "${S[@]}" --server-python "$OPT_PY" --label s-opt-all )

echo "==> matrix complete in $RESULTS_DIR"
ls "$RESULTS_DIR"
