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

Compared with `benchmarks/scripts/compare_trajs.py` at the trajectory
level (`trajectory[i]`: action, real environment observation, thought,
response, state, rendered query; plus exit_status, submission,
edited_files*). Note: replay-output `history[].content` for observation
turns holds only unrendered `{observation}` templates by design — the real
observations live in `trajectory[].observation`, which is what we compare.

| Check | Control | Treatment |
|---|---|---|
| exit_status | submitted | submitted |
| trajectory steps identical | 11 (self) | 11/11 vs control |
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
- 6/8 pairs fully identical (actions + real observations + state +
  info); all 8 `submitted` on both arms with identical submissions. The 2
  diverging pairs (both `install_from_source`) differ only in one
  `pip install -e` observation (spinner frames / temp names), each proven
  nondeterministic by a control repeat — not a runtime gap. No code
  changes needed.
- Timing (main window100 variant, N=3 fresh containers per arm, idle host):
  control 11.43/11.01/12.91 s (mean 11.78); treatment 7.13/7.71/7.44 s
  (mean 7.43). Every timing run reproduced the committed histories
  item-for-item.

## New instances (2026-09-25)

Two demos beyond marshmallow-1867 (record: `trajectories/new-instances/`,
builder: `benchmarks/scripts/build_new_instance_inputs.py`):

- humanevalfix-python-0: PARITY (5 steps, real `abs(...)` fix submitted
  both arms).
- function_calling_simple: PARITY (5 steps) as an error-path signal —
  the old-era `edit` tool call is a no-op under current bundles on both
  arms, so the run shows the unchanged SyntaxError and the submit is
  empty on both sides.

Overall: 9/11 pairs fully identical at trajectory level; the other 2
differ only in nondeterministic `pip install` output (proven by control
repeats); all 11 `submitted` on both arms.

## Scope and limits

- Three instances (marshmallow-1867 in 8 tool-config variants,
  humanevalfix-python-0, function_calling_simple), N=3 timing per arm on
  one variant: the timing gap (~1.6x) is indicative, not a headline; the
  parity claim (byte-identical trajectory steps) is exact for these
  workloads.
- No live LLM inference was available in this environment (no model keys);
  replay of recorded trajectories is the strongest available substitute —
  it replays genuine agent behavior including errors and retries.
- Upstream quirk noted: `LocalRuntime.close()` does not clear its session
  dict, so a second run against the same upstream server fails with
  `SessionExistsError`; both arms use a fresh container per run.
