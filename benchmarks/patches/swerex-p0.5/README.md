# SWE-ReX P0.5 patch series (experimental)

Unified diffs against the pinned upstream revision in
[benchmarks/pins.md](../../pins.md). Applied in filename order by
[benchmarks/scripts/build-opt-venv.sh](../../scripts/build-opt-venv.sh),
which clones the pin into gitignored `.opt-src/swerex` and installs it
into gitignored `.venv-opt`.

Every patch is behavior-preserving unless its env var is set to `1`.

| Patch | Flag | Change |
| --- | --- | --- |
| `0001-opt-skip-syntax-check` | `SWEREX_OPT_SKIP_SYNTAX_CHECK` | Skip the per-command `bash -n` subprocess; syntax errors surface from the persistent shell instead of raising `BashIncorrectSyntaxError` up front |
| `0002-opt-single-submit-exit-status` | `SWEREX_OPT_SINGLE_SUBMIT` | Frame the exit code inline (`EXITCODESTART<seq>:<code>EXITCODEEND`) in the single submission; skip the extra Part 3 round trip. Sequence numbers ignore stale framings from timed-out commands |
| `0003-opt-no-fixed-startup-sleep` | `SWEREX_OPT_NO_FIXED_SLEEP` | Skip the 0.3s startup sleep and post-source `sleep 0.3`; rely on PS1 prompt synchronization |

## Known semantic deltas (flags on)

- 0001: malformed commands no longer raise `BashIncorrectSyntaxError`
  quickly. Some (e.g. unclosed quotes) hang the shell in continuation
  until the command timeout instead, and may leave the session needing
  a reset. Only enable for well-formed benchmark workloads.
- 0002: applies only when `action.check != "ignore"`; the `ignore` path
  keeps upstream behavior (no exit code). Timeout error messages come
  from the single-submit parser rather than Part 3, so wording differs.

## Reproduce

```sh
benchmarks/scripts/build-opt-venv.sh
.venv-opt/bin/python benchmarks/scripts/run_workload_a.py --variant swerex-local --n-tiny 20 --concurrent 10
SWEREX_OPT_SKIP_SYNTAX_CHECK=1 SWEREX_OPT_SINGLE_SUBMIT=1 SWEREX_OPT_NO_FIXED_SLEEP=1 \
  .venv-opt/bin/python benchmarks/scripts/run_workload_a.py --variant swerex-local --n-tiny 20 --concurrent 10
```
