# New-instance replays — run record

2026-09-25. Two demo trajectories beyond marshmallow-1867, each replayed
against control (`swerex-control:1.4.0`, port 8471) and treatment
(`agent-execd-replay:latest`, port 8472). Fresh container per run, host
otherwise idle; same run pattern as `../control-replay/NOTES.md`.

## Inputs (built by `benchmarks/scripts/build_new_instance_inputs.py`)

Neither demo ships `replay_config`, so configs are transplanted and fixture
repos reconstructed from the `open` observations (both verified buggy
pre-fix: `main.py` fails its asserts, `missing_colon.py` is a SyntaxError):

- `humanevalfix-python-0.input.traj` — 11 history items, 5 actions
  (ls → open → str_replace → run → submit); thought_action config from the
  sweep window100 sibling; fixture `_vendor/humanevalfix-testbed/main.py`
  (23 lines, git, mounted at `/testbed`).
- `function_calling_simple.input.traj` — 12 history items, 5 actions
  (find_file → open → edit → bash → submit); function_calling config from
  the sweep function_calling sibling; fixture
  `_vendor/fc-simple-testbed/tests/missing_colon.py` (10 lines, mode 755,
  git, mounted at `/testbed`).

## Results

Compared with `benchmarks/scripts/compare_trajs.py` (trajectory-level:
actions + real observations + state + info):

- humanevalfix-python-0: PARITY (5 steps); submission is the real
  `abs(...)` fix on both arms; both `submitted`.
- function_calling_simple: PARITY (5 steps); both `submitted`.
  Caveat: the old-era `edit` tool call maps to no current-bundle tool, so
  step 2 is a no-op (empty action/observation) on both arms, the run step
  shows the unchanged SyntaxError, and the submit is empty
  (`info.submission` None both arms). Error-path parity signal, not a
  success-path replay — same class as the sweep's cursors transplants.

No code changes needed.
