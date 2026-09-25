# Treatment replay — run record

agent-execd (Rust SWE-ReX-compatible server, linux/aarch64) + SWE-agent v1.1.0
replay of the same marshmallow-1867 trajectory as the control arm.
Completed 2026-09-24.

## Setup (mirrors control)

- Input: `trajectories/control-replay.input.traj` (same repaired file).
- Testbed: `_vendor/marshmallow-testbed @ bfd2593d`, bind-mounted at
  `/testbed`, `PYTHONPATH=/testbed/src`.
- `Dockerfile.agent-execd-replay` → image `agent-execd-replay:latest`
  (multi-stage: `rust:1-bookworm` builder, `cargo build --release --locked`;
  runtime `python:3.11-slim` + git/bash, same userspace as control).

## Exact commands

```sh
# image (small tar context, avoids target/ + _vendor/)
tar cf - Dockerfile.agent-execd-replay Cargo.toml Cargo.lock crates \
  | docker build -f Dockerfile.agent-execd-replay -t agent-execd-replay:latest -

# server (fresh container per replay, port 8472)
docker run -d --rm --name agent-execd-replay -p 127.0.0.1:8472:8472 \
  -v "$PWD/_vendor/marshmallow-testbed:/testbed" -e PYTHONPATH=/testbed/src \
  agent-execd-replay:latest agent-execd --host 0.0.0.0 --port 8472 \
  --auth-token <tok>

# the replay
_vendor/.venv-replay/bin/sweagent run-replay \
  --traj_path trajectories/control-replay.input.traj \
  --deployment.type remote --deployment.host http://127.0.0.1 \
  --deployment.port 8472 --deployment.auth_token <tok> \
  --output_dir trajectories/treatment-replay

docker stop agent-execd-replay
```

## Result

- Output: `trajectories/treatment-replay/marshmallow-code__marshmallow-1867/marshmallow-code__marshmallow-1867.traj`
- exit_status `submitted`; history 24/24 items byte-identical to control
  (11 actions; observation turns are unrendered templates in replay
  output — real observations compared at trajectory level: 11/11 steps
  identical, incl. repro `344` → fix → `345`); `submission` and
  `edited_files*` identical. See `compare_trajs.py`.
- Wall time: 7.64 s vs control 12.65 s (first-hand `/usr/bin/time -p`
  re-runs, fresh containers, idle host; N=1 each — direction only).
- One compat fix was required before parity: PTY width 200 → 80 columns
  (`crates/execd-core/src/session.rs`, `PTY_COLS`), matching upstream
  pexpect default `dimensions=(24, 80)` so width-sensitive tools (`ls`,
  `ps`, …) format identically. `cargo test --workspace` 30 green,
  `cargo clippy` clean after the fix.
