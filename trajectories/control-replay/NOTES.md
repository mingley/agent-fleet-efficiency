# Control replay — run record

Upstream SWE-ReX server (vendored v1.4.0, local runtime) + SWE-agent v1.1.0 replay
of the marshmallow-1867 demo trajectory. Completed 2026-09-24.

## Why the server runs in Docker (not bare metal on this Mac)

SWE-agent hard-codes server-side paths `/root/...` (tool bundles, state) and the
traj's env reset does `cd /testbed`. Both need root; this host has no sudo, and
macOS forbids them. The container runs the *unmodified upstream* server
(`python -m swerex`, LocalRuntime) as root, exactly like SWE-bench images.

## Inputs (all under workspace root)

- `trajectories/control-replay.input.traj` — repaired copy of
  `_vendor/SWE-agent/trajectories/demonstrations/replay__marshmallow-code__marshmallow-1867__default_sys-env_window100__t-0.20__p-0.95__c-2.00__install-1/marshmallow-code__marshmallow-1867.traj`.
  Repairs (replay_config only; 11 actions + 23 history items byte-identical):
  - `agent.tools.bundles`: `tools/defaults`→`tools/windowed`,
    `tools/edit_linting`→`tools/windowed_edit_linting` (upstream renames), and
    prepended `tools/registry` (now holds `_write_env`, required first on PATH).
  - `agent.history_processor` (dict) → `agent.history_processors` (list).
  - `env.repo` kept as original `preexisting/testbed @ bfd2593d...`.
  (`--update_config` could not be used: RunReplay validates the stale
  replay_config *before* merging updates.)
- `_vendor/marshmallow-testbed` — `git clone
  https://github.com/marshmallow-code/marshmallow.git`, checked out at
  `bfd2593d4b416122e30cdefe0c72d322ef471611`, bind-mounted at `/testbed`.
- `Dockerfile.swerex-control` → image `swerex-control:1.4.0`
  (python:3.11-slim + git/bash + `pip install /opt/SWE-ReX` from `_vendor/SWE-ReX`).

## Exact commands

```sh
# venv (CPython 3.13.12)
uv venv _vendor/.venv-replay
uv pip install --python _vendor/.venv-replay/bin/python -e _vendor/SWE-ReX
uv pip install --python _vendor/.venv-replay/bin/python -e _vendor/SWE-agent
uv pip install --python _vendor/.venv-replay/bin/python setuptools  # distutils shim (host-side only)

# server image + container (token: c96c30159d09e98cf97037f2ad8219e9, port 8471)
docker build -f Dockerfile.swerex-control -t swerex-control:1.4.0 .
docker run -d --rm --name swerex-control -p 127.0.0.1:8471:8471 \
  -v "$PWD/_vendor/marshmallow-testbed:/testbed" -e PYTHONPATH=/testbed/src \
  swerex-control:1.4.0 python -m swerex --host 0.0.0.0 --port 8471 \
  --auth-token c96c30159d09e98cf97037f2ad8219e9

# the one control replay (wall 12.9s)
_vendor/.venv-replay/bin/sweagent run-replay \
  --traj_path trajectories/control-replay.input.traj \
  --deployment.type remote --deployment.host http://127.0.0.1 \
  --deployment.port 8471 --deployment.auth_token c96c30159d09e98cf97037f2ad8219e9 \
  --output_dir trajectories/control-replay

docker stop swerex-control
```

Note: restart the container before each replay — upstream `LocalRuntime.close()`
closes sessions without clearing its session dict, so a second run against the
same server fails with `SessionExistsError: session default already exists`
(upstream never hits this: DockerDeployment uses a fresh container per run).

## Result

- Output: `trajectories/control-replay/marshmallow-code__marshmallow-1867/marshmallow-code__marshmallow-1867.traj`
- exit_status `submitted`; 11/11 actions byte-identical; repro `344`→fix→`345`
  identical; submission hunk identical (only git header formatting differs).
- Post-run, `/testbed` (bind mount) holds the replayed edit to
  `src/marshmallow/fields.py`; env reset (`git restore/checkout/clean`) reverts
  it automatically on the next run.
