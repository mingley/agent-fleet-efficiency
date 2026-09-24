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
| `0004-opt-reuse-remote-session` | `SWEREX_OPT_REUSE_SESSION` | RemoteRuntime keeps one keep-alive `aiohttp` session instead of a fresh `force_close` session per request; closed by `close()` |
| `0005-opt-bounded-idempotency` | `SWEREX_OPT_BOUNDED_IDEMPOTENCY` | Idempotency middleware passes through responses with no `X-Request-ID` unbuffered and only caches bodies up to 1MiB |
| `0006-opt-stream-upload` | `SWEREX_OPT_STREAM_UPLOAD` | Server writes uploads in 1MiB chunks instead of `await file.read()` into memory |

## Known semantic deltas (flags on)

- 0001: malformed commands no longer raise `BashIncorrectSyntaxError`
  quickly. Some (e.g. unclosed quotes) hang the shell in continuation
  until the command timeout instead, and may leave the session needing
  a reset. Only enable for well-formed benchmark workloads.
- 0002: applies only when `action.check != "ignore"`; the `ignore` path
  keeps upstream behavior (no exit code). Timeout error messages come
  from the single-submit parser rather than Part 3, so wording differs.
- 0004: the shared session stays open until `close()`; callers that never
  call `close()` leak it (aiohttp "Unclosed client session" warning).
  Keep-alive reuses TCP connections, so per-request source ports repeat.
- 0005: responses over 1MiB delivered under an `X-Request-ID` are not
  retained, so a client retry re-executes instead of replaying them.
  Responses without a request id are no longer rebuilt (bytes unchanged).
- 0006: no semantic delta; identical bytes reach the same target path
  (chunked 1MiB reads instead of one full read).

## Reproduce

```sh
benchmarks/scripts/build-opt-venv.sh
.venv-opt/bin/python benchmarks/scripts/run_workload_a.py --variant swerex-local --n-tiny 20 --concurrent 10
SWEREX_OPT_SKIP_SYNTAX_CHECK=1 SWEREX_OPT_SINGLE_SUBMIT=1 SWEREX_OPT_NO_FIXED_SLEEP=1 \
  .venv-opt/bin/python benchmarks/scripts/run_workload_a.py --variant swerex-local --n-tiny 20 --concurrent 10
```
