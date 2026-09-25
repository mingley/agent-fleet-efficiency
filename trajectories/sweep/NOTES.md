# Replay variant sweep — run record

2026-09-25. All 8 marshmallow-1867 demo configs replayed against control
(`swerex-control:1.4.0`, port 8471) and treatment (`agent-execd-replay:latest`,
port 8472). Sequential runs, fresh container per run, host otherwise idle;
same run pattern as `../control-replay/NOTES.md` (testbed bind-mounted at
`/testbed`, `PYTHONPATH=/testbed/src`).

## Layout

- `<name>.input.traj` — replay input per variant (see below).
- `<name>.control/`, `<name>.treatment/` — `run-replay` outputs + `driver.log`.
- `timing-control-N/`, `timing-treatment-N/` (N=1..3) — timed repeats on the
  main window100 variant (`/usr/bin/time -p`).
- `function_calling_replace_from_source.control-repeat/` — diagnostic 2nd
  control run proving the pip-output divergence is nondeterministic.

## Inputs

5 variants carry `replay_config` and were repaired with
`benchmarks/scripts/repair_traj.py` (bundle renames incl.
`tools/edit_replace`→`tools/windowed_edit_replace`, `tools/registry` prepend,
`history_processor`→`history_processors`; history byte-identical):

- `default_sys-env_window100__...` (= `../control-replay.input.traj`, byte-identical)
- `function_calling__install-1`, `function_calling_replace__install-1`,
  `function_calling_replace_from_source`, `xml_sys-env_window100__...`

3 pre-replay-format variants have no `replay_config`, so faithful repair is
impossible (`run-replay` requires it; `traj-to-demo` too). Their inputs graft
the repaired sibling config onto the original history (documented transplant,
NOT the original config — validates these action sequences, not the configs):

- `default__...___install_from_source` ← window100 config (thought_action)
- `default_sys-env_cursors_window100__...` ← window100 config (thought_action)
- `xml_sys-env_cursors_window100__...` ← xml window100 config (xml_thought_action)

The cursors tooling (`set_cursors`, cursor-based bare `edit`) no longer exists
upstream, so the transplanted cursors runs take tool-error paths and submit an
empty diff on both arms — still a valid error-path parity signal.

## Results (control vs treatment)

7/8 byte-identical histories; all 8 `submitted` both arms, all 8 identical
submissions. Sole divergence: `function_calling_replace_from_source`
history[7] (a `pip install -e` observation): spinner frames, wheel sha256 /
`/tmp/pip-ephem-wheel-cache-*` names (nondeterministic per build — the
control repeat differs from control at the same index), plus
typing-extensions/pygments "already satisfied" (control image) vs downloaded
(treatment image layer lacks the SWE-ReX Python deps; same versions either
way). Not an agent-execd gap; no code changes made.

Timing (wall `real`, main window100 variant): control 13.14/12.50/11.52s
(mean 12.39); treatment 6.50/6.60/6.60s (mean 6.57).
