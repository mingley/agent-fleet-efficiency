# End-to-end replay validation: real SWE-agent trajectory vs agent-execd

Date: 2026-09-24. Question: does agent-execd behave identically to upstream
SWE-ReX under real agent traffic (not just synthetic probes)?

## Method

Replayed a real recorded SWE-agent trajectory
(marshmallow-code/marshmallow-1867, 11 actions: explore → reproduce → edit →
verify → submit) twice: once against the upstream Python server (control),
once against agent-execd (treatment). Both servers ran in Docker on the same
host with identical userspace (`python:3.11-slim` + git/bash), the same
repo checkout bind-mounted at `/testbed`, fresh containers per run, idle
host. SWE-agent drove both via `RemoteDeployment` (`sweagent run-replay`).

- Control record: `trajectories/control-replay/NOTES.md`
  (`Dockerfile.swerex-control`)
- Treatment record: `trajectories/treatment-replay/NOTES.md`
  (`Dockerfile.agent-execd-replay`)
- Committed evidence: input traj + both output trajs under `trajectories/`

## Result: full observation parity

| Check | Control | Treatment |
|---|---|---|
| exit_status | submitted | submitted |
| history items identical | 24 (self) | 24/24 vs control |
| `submission` identical | — | yes |
| `edited_files*` identical | — | yes |
| wall time (N=1 each) | 12.65 s | 7.64 s |

Every agent action and every environment observation (including the repro
`344` → fix → `345` and the submission diff hunk) is byte-identical
between runtimes. Both arms also reproduce themselves (independent re-runs
match the committed outputs item-for-item).

## Compat fix found by this experiment

- PTY width 200 → 80 columns (`crates/execd-core/src/session.rs`),
  matching upstream pexpect default `dimensions=(24, 80)`. Before the fix,
  width-sensitive tool output diverged from upstream. After: parity.
  `cargo test --workspace` 30 green, clippy clean.

## Scope and limits

- One trajectory, one repo, N=1 timing per arm: timing is direction-only;
  the parity claim (byte-identical histories) is exact for this workload.
- No live LLM inference was available in this environment (no model keys);
  replay of recorded trajectories is the strongest available substitute —
  it replays genuine agent behavior including errors and retries.
- Upstream quirk noted: `LocalRuntime.close()` does not clear its session
  dict, so a second run against the same upstream server fails with
  `SessionExistsError`; both arms use a fresh container per run.
