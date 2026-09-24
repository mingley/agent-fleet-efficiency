# agent-execd crates

Rust implementation of the SWE-ReX remote-execution server surface
(`docs/p1-compat.md`, pinned upstream `5c995c3`).

## Layout

- `execd-protocol` — serde wire models mirroring the SWE-ReX Pydantic
  models exactly (sessions, actions/observations, execute, file
  primitives, 511 error envelope).
- `execd-core` — session engine: persistent bash sessions on a PTY
  (`SessionManager`: create/run/interrupt/get/close/close-all) plus
  the `ExecdError` → `class_path` mapping.
- `execd-server` — `agent-execd` binary: axum HTTP server wiring the
  routes below to `execd-core`, with `X-API-Key` auth and the 511
  `{"swerexception": ...}` error mapper.

## Build / run / test

From the workspace root (`agent-fleet-efficiency/`):

```sh
cargo build --release            # builds target/release/agent-execd
cargo test --workspace           # all crate tests
cargo clippy -p execd-server     # lint (server is clippy-clean)
```

Run the server:

```sh
./target/release/agent-execd --host 127.0.0.1 --port 8000 --auth-token secret
```

Flags: `--host` (default `127.0.0.1`), `--port` (default `8000`),
`--auth-token` (default `""` = auth disabled, same as importing the
upstream app without `main()`). When the token is non-empty, every
route requires header `X-API-Key: <token>` and returns
`401 {"detail": "Invalid API Key"}` otherwise.

## Route list

| Method | Path             | Backing call |
|--------|------------------|--------------|
| GET    | `/`              | greeting `{"message": "hello world"}` |
| GET    | `/is_alive`      | always `{"is_alive": true, "message": ""}` |
| POST   | `/create_session`| `SessionManager::create_session` |
| POST   | `/run_in_session`| `run_bash` / `interrupt`, by `action_type` (`"bash"` default when absent) |
| POST   | `/close_session` | `SessionManager::close_session` |
| POST   | `/execute`       | `tokio::process::Command` with `subprocess.run` semantics |
| POST   | `/read_file`     | `tokio::fs::read` + decode |
| POST   | `/write_file`    | parents `mkdir`, `tokio::fs::write` |
| POST   | `/upload`        | multipart `file` + `target_path` + `unzip` → move or zip-extract |
| POST   | `/close`         | `SessionManager::close_all`, returns `{}` |

Errors: every `ExecdError` maps to `511` + `{"swerexception": {...}}`
via `to_transfer()`; I/O failures map to builtin `class_path`s
(`builtins.FileNotFoundError`, `builtins.OSError`,
`builtins.UnicodeDecodeError`); bad zip maps to `zipfile.BadZipFile`;
handler-level validation failures (unknown `action_type`/`session_type`
/`check` mode, bad `unzip`, missing multipart fields) map to 511
`builtins.ValueError`. Only truly unparseable JSON bodies fall through
to axum's own 4xx rejection.

## Known deviations from upstream

1. `merge_output_streams=true`: stdout and stderr are concatenated
   (stdout then stderr) instead of file-descriptor-interleaved;
   `stderr` is still `""` as upstream.
2. `read_file` supports `encoding` `None`/`utf-8` only (other codecs →
   511 `ValueError`); `errors` supports `strict`/`backslashreplace`/
   `replace`/`ignore` (default `strict`, as `Path.read_text`).
3. No `X-Request-ID` single-slot replay middleware (P1 Q6: client
   default is 0 retries, so unobservable in practice).
4. No full-body response buffering middleware.
5. Missing multipart fields (`file`/`target_path`) return 511
   `ValueError`; upstream FastAPI returns 422 for missing required
   form fields.
6. `/execute` with `shell=false` and a string command executes the
   whole string as the program name (no splitting), exactly per the
   `subprocess.run` contract ("the string must simply name the program
   to be executed without specifying any arguments"). Prefer argv lists
   with `shell=false`, strings with `shell=true`.
7. Command timeout kills the direct child only (same as
   `subprocess.run`); no process-tree kill.
8. `execute` `check` failure message uses Rust `{:?}` quoting instead
   of Python `repr` (double quotes vs single quotes).
9. Signaled processes report `exit_code` as `-<signo>` (matching
   `subprocess` `returncode`); if neither a code nor a signal is
   available, `exit_code` is `null`.
