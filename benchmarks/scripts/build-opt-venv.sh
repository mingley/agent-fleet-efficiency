#!/bin/bash
# Build the P0.5 optimized SWE-ReX venv for the LOCAL runtime spike.
#
# Clones SWE-ReX at the pinned SHA (see benchmarks/pins.md) into
# $ROOT/.opt-src/swerex, applies benchmarks/patches/swerex-p0.5/*.patch,
# and installs the patched source into $ROOT/.venv-opt (created with uv).
# Both output directories are gitignored. Re-running is safe: outputs are
# rebuilt from scratch.
#
# The patches are behavior-preserving unless their env vars are set:
#   SWEREX_OPT_SKIP_SYNTAX_CHECK=1  skip per-command `bash -n` check (0001)
#   SWEREX_OPT_SINGLE_SUBMIT=1      inline exit-status framing, no extra
#                                   EXITCODESTART/END round trip (0002)
#   SWEREX_OPT_NO_FIXED_SLEEP=1     prompt sync instead of startup sleeps (0003)
set -euo pipefail

PIN="5c995c365dfb1fd5bc56fda688be5d8538f9931f" # must match benchmarks/pins.md
UPSTREAM_URL="https://github.com/SWE-agent/SWE-ReX.git"

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SRC="$ROOT/.opt-src/swerex"
VENV="$ROOT/.venv-opt"
PATCH_DIR="$ROOT/benchmarks/patches/swerex-p0.5"

command -v git >/dev/null || { echo "error: git not found on PATH" >&2; exit 1; }
command -v uv >/dev/null || { echo "error: uv not found on PATH" >&2; exit 1; }

# --- fresh shallow checkout at the pinned SHA ---
rm -rf "$SRC"
mkdir -p "$(dirname "$SRC")"
if ! (
    git init -q "$SRC" &&
    git -C "$SRC" remote add origin "$UPSTREAM_URL" &&
    git -C "$SRC" fetch -q --depth 1 origin "$PIN" &&
    git -C "$SRC" checkout -q "$PIN"
); then
    # Offline fallback: reuse a local reference checkout only if it is
    # verifiably at the pinned SHA.
    REF="${SWEREX_REF_DIR:-/tmp/swerex-ref}"
    echo "fetch from $UPSTREAM_URL failed; trying local reference $REF" >&2
    if [ -d "$REF" ] && [ "$(git -C "$REF" rev-parse HEAD 2>/dev/null)" = "$PIN" ]; then
        rm -rf "$SRC"
        git clone -q --no-checkout "$REF" "$SRC"
        git -C "$SRC" checkout -q "$PIN"
    else
        echo "error: no usable clone (network failed, $REF not at $PIN)" >&2
        exit 1
    fi
fi
HEAD="$(git -C "$SRC" rev-parse HEAD)"
[ "$HEAD" = "$PIN" ] || { echo "error: checkout at $HEAD, want $PIN" >&2; exit 1; }
[ -z "$(git -C "$SRC" status --porcelain)" ] || { echo "error: checkout not clean" >&2; exit 1; }
echo "checked out SWE-ReX $HEAD in $SRC"

# --- apply P0.5 patches in order (check before each apply) ---
shopt -s nullglob
PATCHES=("$PATCH_DIR"/*.patch)
[ "${#PATCHES[@]}" -gt 0 ] || { echo "error: no patches in $PATCH_DIR" >&2; exit 1; }
for p in "${PATCHES[@]}"; do
    git -C "$SRC" apply --check "$p" || { echo "error: check failed for $p" >&2; exit 1; }
    git -C "$SRC" apply "$p" && echo "applied $(basename "$p")"
done

# --- build venv and install patched source ---
rm -rf "$VENV"
if [ -n "${UV_PYTHON:-}" ]; then
    uv venv --python "$UV_PYTHON" "$VENV"
elif [ -x "$ROOT/.venv/bin/python" ]; then
    # Pin to the baseline .venv interpreter so the optimized comparison is
    # never confounded by a Python version drift.
    uv venv --python "$ROOT/.venv/bin/python" "$VENV"
else
    uv venv "$VENV"
fi
uv pip install --python "$VENV/bin/python" "$SRC"
# Upstream remote.py imports aiohttp but pyproject does not declare it;
# the SERVER/REMOTE path needs it on both ends.
uv pip install --python "$VENV/bin/python" aiohttp
"$VENV/bin/python" -c "import swerex; print('swerex:', swerex.__file__)"
echo "opt venv ready: $VENV"
