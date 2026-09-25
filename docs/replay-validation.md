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
| wall time (N=3 each, means) | 11.78 s | 7.43 s |

Every agent action and every environment observation (including the repro
`344` → fix → `345` and the submission diff hunk) is byte-identical
between runtimes. Both arms also reproduce themselves (independent re-runs
match the committed outputs item-for-item).

## Compat fix found by this experiment

- PTY width 200 → 80 columns (`crates/execd-core/src/session.rs`),
  matching upstream pexpect default `dimensions=(24, 80)`. Before the fix,
  width-sensitive tool output diverged from upstream. After: parity.
  `cargo test --workspace` 30 green, clippy clean.

## Variant sweep (2026-09-25)

All 8 marshmallow-1867 demo configs replayed on both arms
(record: `trajectories/sweep/NOTES.md`; evidence: `trajectories/sweep/`):

- 5 variants with `replay_config` repaired by
  `benchmarks/scripts/repair_traj.py` (config-only, histories
  byte-identical to vendor originals); 3 pre-replay-format variants use a
  documented config transplant (same action sequences, sibling config).
- 7/8 pairs byte-identical histories; all 8 `submitted` on both arms with
  identical submissions. The sole divergence is one `pip install -e`
  observation (spinner frames / temp names), proven nondeterministic by a
  control repeat — not a runtime gap. No code changes needed.
- Timing (main window100 variant, N=3 fresh containers per arm, idle host):
  control 11.43/11.01/12.91 s (mean 11.78); treatment 7.13/7.71/7.44 s
  (mean 7.43). Every timing run reproduced the committed histories
  item-for-item.

## Scope and limits

- One repo/instance (8 tool-config variants), N=3 timing per arm on one
  variant: the timing gap (~1.6x) is indicative, not a headline; the
  parity claim (byte-identical histories) is exact for these workloads.
- No live LLM inference was available in this environment (no model keys);
  replay of recorded trajectories is the strongest available substitute —
  it replays genuine agent behavior including errors and retries.
- Upstream quirk noted: `LocalRuntime.close()` does not clear its session
  dict, so a second run against the same upstream server fails with
  `SessionExistsError`; both arms use a fresh container per run.
