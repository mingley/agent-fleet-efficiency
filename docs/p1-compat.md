# P1 agent-execd Compatibility Specification

Pinned upstream: SWE-ReX at `5c995c365dfb1fd5bc56fda688be5d8538f9931f`
(ref: `/tmp/swerex-ref`, verified via `.git/refs/heads/main`).
Sources: `src/swerex/server.py`, `src/swerex/runtime/abstract.py`,
`src/swerex/runtime/remote.py`, `src/swerex/deployment/remote.py`,
plus `src/swerex/runtime/config.py`, `src/swerex/deployment/config.py`,
`src/swerex/exceptions.py`, `src/swerex/runtime/local.py` for timeout/exit-code semantics.

Local context: `docs/architecture.md` M1 (health/version, create session, run,
timeout, interrupt, teardown) and M2 (file primitives + parity suite);
`ROADMAP.md` P1 compatibility surface; `docs/oss-landscape.md` Tier A SWE-ReX entry.

## 1. Full remote HTTP surface (endpoint table)

FastAPI app in `server.py`. All routes share two middlewares (see §4 and §3.8).
Error mapping for all routes: unhandled non-HTTP `Exception` → `511` with
`{"swerexception": _ExceptionTransfer}`; `HTTPException`/`StarletteHTTPException`
pass through to the default handler (`server.py:105-119`).

| # | Method | Path | Request model / body | Response model | Notes |
|---|--------|------|----------------------|----------------|-------|
| 1 | GET | `/` | none | `{"message": "hello world"}` (untyped dict) | Liveness greeting only; no runtime method backs it (`server.py:122-124`). |
| 2 | GET | `/is_alive` | none | `IsAliveResponse` | Calls `runtime.is_alive()` with no args (`server.py:127-129`). Client treats non-200/511 as `is_alive=False` (§3.1). |
| 3 | POST | `/create_session` | `CreateSessionRequest` = `CreateBashSessionRequest` (discriminator `session_type="bash"`) | `CreateSessionResponse` = `CreateBashSessionResponse` | `server.py:132-134`. |
| 4 | POST | `/run_in_session` | `Action` = `BashAction \| BashInterruptAction` (discriminator `action_type`: `"bash"` / `"bash_interrupt"`) | `Observation` = `BashObservation` (discriminator `session_type="bash"`) | Single endpoint for run AND interrupt; variant selected by body (`server.py:137-139`). |
| 5 | POST | `/close_session` | `CloseSessionRequest` = `CloseBashSessionRequest` | `CloseSessionResponse` = `CloseBashSessionResponse` | `server.py:142-144`. |
| 6 | POST | `/execute` | `Command` | `CommandResponse` | One-shot subprocess, session-independent (`server.py:147-149`). |
| 7 | POST | `/read_file` | `ReadFileRequest` | `ReadFileResponse` | `server.py:152-154`. |
| 8 | POST | `/write_file` | `WriteFileRequest` | `WriteFileResponse` (empty model) | `server.py:157-159`. |
| 9 | POST | `/upload` | `multipart/form-data`: `file` (bytes, required), `target_path` (str form field, required), `unzip` (bool form field, default `False`) | `UploadResponse` (empty model) | NOT JSON. Server streams upload to temp file, then `shutil.move` or zip-extract to `target_path` (`server.py:162-184`). Client sends `unzip="true"` for dirs, `"false"` for files (`remote.py:223-261`). |
| 10 | POST | `/close` | none (`payload=None`) | `CloseResponse` (empty model) | Calls `runtime.close()` then returns `CloseResponse()` (`server.py:187-190`). |

**Endpoint count: 10 HTTP routes (9 runtime-backed + `GET /`).**

Notes:

- No `/version` HTTP endpoint exists. "Version" in the P1 surface is the
  server CLI flag `--version` (`server.py:196-206`, prints `swerex.__version__`).
  agent-execd must decide whether to add a version route or expose version via
  `is_alive`/packaging (see §5, Q1).
- Request/response bodies are Pydantic JSON (`payload.model_dump()`,
  `output_class(**await resp.json())`) except `/upload` (multipart) and the
  bodyless GETs/`/close` (`remote.py:165-197`).
- `serialize_model` on the server uses `model_dump()` (Pydantic v2) with a
  `.dict()` fallback (`server.py:38-39`).

## 2. Minimal subset SWE-agent actually needs (with evidence)

Evidence base (static only — no SWE-agent run performed):

- `AbstractRuntime` (`abstract.py:234-288`) defines 9 abstract methods:
  `is_alive`, `create_session`, `run_in_session`, `close_session`, `execute`,
  `read_file`, `write_file`, `upload`, `close`.
- `RemoteRuntime` (`remote.py`) implements all 9 with a 1:1 endpoint mapping:
  `is_alive`→`GET /is_alive`; `create_session`→`POST /create_session`;
  `run_in_session`→`POST /run_in_session`; `close_session`→`POST /close_session`;
  `execute`→`POST /execute`; `read_file`→`POST /read_file`;
  `write_file`→`POST /write_file`; `upload`→`POST /upload` (multipart);
  `close`→`POST /close`. Plus `wait_until_alive` (client-side poll loop over
  `is_alive`, not an endpoint) and generic `_request` POST helper with
  `X-Request-ID` idempotency key (`remote.py:162-198`).
- `RemoteDeployment` (`deployment/remote.py:59-73`): `start()` only constructs
  `RemoteRuntime(host, port, auth_token, timeout)` (no HTTP call);
  `stop()` calls `runtime.close()` (`POST /close`); `is_alive()` delegates to
  `runtime.is_alive()`. So connection/startup/teardown over HTTP = `is_alive`
  polling + `close` at the end.
- `ROADMAP.md` P1 compatibility surface: health/version, create/close session,
  run in session, interrupt/cancel, timeout + exit-status semantics, file
  primitives needed by callers, auth token handling.
- `docs/architecture.md` M1: health/version, create session, run command,
  timeout, interrupt, teardown. M2: file primitives actually used + parity suite.

Minimal-subset proposal (to be confirmed by running SWE-agent, §5):

| Priority | Endpoint(s) | Why |
|----------|-------------|-----|
| Must (M1) | `GET /is_alive`, `POST /create_session`, `POST /run_in_session` (both `bash` and `bash_interrupt` variants), `POST /close_session`, `POST /close`, auth + `X-Request-ID` + 511 error-transfer behavior | Persistent-shell loop + startup/teardown; interrupt shares `run_in_session`. |
| Must (M2) | `POST /read_file`, `POST /write_file`, `POST /upload` | P1/M2 file surface; exact read-vs-upload mix needs a live run (Q4). |
| Likely | `POST /execute` | Exists on the client and is the only session-independent path; whether SWE-agent calls it needs a live run (Q3). |
| Explicitly out of minimal set | `GET /` | Greeting only; implement trivially or omit (Q1). |

agent-execd M1 scope = the "Must (M1)" row; M2 adds the file row.

## 3. Schema details

All models are Pydantic `BaseModel`s in `abstract.py`. Discriminated unions use
`Annotated[..., Field(discriminator=...)]`; the wire body always carries the
discriminator field. Field lists below are exhaustive per model.

### 3.1 Session aliveness

- `IsAliveResponse`: `is_alive: bool` (required), `message: str = ""`.
  `bool(resp)` returns `is_alive` (`abstract.py:8-21`).
- Server: `GET /is_alive` → `await runtime.is_alive()`; local runtime always
  returns `IsAliveResponse(is_alive=True)` (`local.py:386-388`).
- Client (`remote.py:128-160`): `GET {host[:port]}/is_alive` with
  `aiohttp.ClientTimeout(total=timeout)` where `timeout` = per-call override or
  `RemoteRuntimeConfig.timeout` (default **0.15 s**). 200 → parse body; 511 →
  reraise transferred exception; any other status → `IsAliveResponse(is_alive=False,
  message="Status code {status} ... {detail}")`; any `aiohttp.ClientError` or
  other exception → `is_alive=False` with traceback in `message`.
- `wait_until_alive(timeout=60.0)` polls `is_alive` via `_wait_until_alive`
  (`remote.py:162-163`).

### 3.2 Create session

- `CreateBashSessionRequest`: `startup_source: list[str] = []`,
  `session: str = "default"`, `session_type: Literal["bash"] = "bash"`,
  `startup_timeout: float = 1.0` (`abstract.py:23-32`).
- `CreateBashSessionResponse`: `output: str = ""`, `session_type: Literal["bash"] = "bash"`
  (`abstract.py:39-43`).
- Server behavior (`local.py:390-401`, `129-175`): duplicate `session` →
  `SessionExistsError` (→ 511); unknown session type → `ValueError` (→ 511).
  `BashSession.start()`: spawns `/usr/bin/env bash` via pexpect
  (`encoding="utf-8"`, `codec_errors="backslashreplace"`, `echo=False`,
  `PS1=SHELLPS1PREFIX, PS2="", PS0=""`), sleeps 0.3 s, sends
  `source <each startup_source> ; sleep 0.3 ; export PS1=... ; export PS2='' ;
  export PS0=''` (reset commands always sent, because sourced files may clobber
  PS1), expects PS1 with `timeout=startup_timeout`, returns stripped output.
  Startup pexpect timeout propagates as `pexpect.TIMEOUT` → 511 (not wrapped).
- Timeout semantics: `startup_timeout` bounds only the post-spawn PS1 expect,
  not the spawn itself; the fixed 0.3 s sleeps are unconditional.

### 3.3 Run in session (normal command)

- `BashAction`: `command: str` (required), `session: str = "default"`,
  `timeout: float | None = None` (None = no timeout),
  `is_interactive_command: bool = False`, `is_interactive_quit: bool = False`,
  `check: Literal["silent","raise","ignore"] = "raise"`,
  `error_msg: str = ""`, `expect: list[str] = []`,
  `action_type: Literal["bash"] = "bash"` (`abstract.py:51-88`).
- `BashObservation`: `output: str = ""`, `exit_code: int | None = None`,
  `failure_reason: str = ""` (never set by local runtime — always `""`),
  `expect_string: str = ""` (which expect string terminated the command; `""`
  on timeout paths that return rather than raise — in practice timeouts raise,
  so `""` is rarely observed), `session_type: Literal["bash"] = "bash"`
  (`abstract.py:109-120`).
- Pipeline (`local.py:274-351`): (1) `_check_bash_command` runs
  `/usr/bin/env bash -n << 'SOUNIQUEEOF' ...` in a subprocess; failure →
  `BashIncorrectSyntaxError` with `extra_info={bash_stdout, bash_stderr}`
  (transferred over 511). (2) Split `command` with bashlex and rejoin with
  `" ; "`; on bashlex failure append
  `\n TMPEXITCODE=$? ; sleep 0.1; echo -n 'UNIQUESTRING29234' ; (exit $TMPEXITCODE)`
  and expect the unique string instead of PS1. (3) `sendline`, expect
  `expect + [PS1]` (or `[UNIQUESTRING]` fallback) with `timeout=action.timeout`;
  pexpect TIMEOUT → `CommandTimeoutError("timeout after {timeout} seconds ...")`.
  Output is ANSI-stripped (`_strip_control_chars`) and `\r\n`→`\n`.
- Exit-code extraction (extra PTY round trip, skipped only if
  `check == "ignore"` → returns `exit_code=None`): sends
  `\necho EXITCODESTART$?EXITCODEEND`, expects `EXITCODEEND` with 1 s timeout
  (TIMEOUT → `NoExitCodeError("timeout while getting exit code")`), regex-parses
  `EXITCODESTART([0-9]+)` (parse failure → `NoExitCodeError`), appends text
  before the marker to output, re-expects PS1 with 0.1 s timeout
  (TIMEOUT → `CommandTimeoutError`), strips `UNIQUESTRING`/PS1 from output.
  If `check == "raise"`, extraction exceptions propagate; if `"silent"`,
  any extraction exception → `exit_code=None`.
- `check` semantics (`local.py:238-243`): `"raise"` →
  `NonZeroExitCodeError("{error_msg + ': ' if set}Command {command!r} failed
  with exit code {exit_code}. Here is the output:\n{output!r}")` when
  `exit_code != 0` (note: `None != 0`, so a `None` exit code under `"raise"`
  also raises); `"silent"` → return observation with `exit_code` possibly
  `None`, never raise for the code; `"ignore"` → skip extraction, always
  `exit_code=None`, never raise.
- Unknown session → `SessionDoesNotExistError` (→ 511). Uninitialized shell →
  `SessionNotInitializedError`.

### 3.4 Run in session (interactive command / quit)

- Same `BashAction` model; triggered when `is_interactive_command` or
  `is_interactive_quit` is true (`local.py:245-272`).
- Sends command, expects `expect + [PS1]` with `timeout=action.timeout`;
  TIMEOUT → `CommandTimeoutError`. No syntax check, no bashlex split, no exit-code
  extraction; always returns `exit_code=0`.
- `is_interactive_command` (e.g. launch gdb): output left-stripped of the echoed
  command. `is_interactive_quit` (terminates the interactive program):
  `setecho(False)`, `waitnoecho()`, sends `stty -echo; echo 'UNIQUESTRING29234'`,
  expects UNIQUESTRING then PS1 (1 s each).
- `is_interactive_quit=True` asserts `is_interactive_command` is False.

### 3.5 Interrupt

- `BashInterruptAction`: `session: str = "default"`, `timeout: float = 0.2`,
  `n_retry: int = 3`, `expect: list[str] = []`,
  `action_type: Literal["bash_interrupt"] = "bash_interrupt"`
  (`abstract.py:90-103`). Same `/run_in_session` endpoint, discriminated by body.
- Behavior (`local.py:186-216`): up to `n_retry` times send SIGINT (`sendintr`),
  expect `expect + [PS1]` with `timeout`; on success return
  `BashObservation(output=<stripped before + 0.5 s trailing drain, stripped>,
  exit_code=0, expect_string=<matched>)`. Between retries sleeps 0.2 s.
  Fallback: send Ctrl-Z, expect PS1, `sendline("kill -9 %1")`, expect PS1,
  drain; failure → `pexpect.TIMEOUT("Failed to interrupt session")` (→ 511).
- Interrupt observations always carry `exit_code=0`; no `check` field exists.

### 3.6 Close session / close runtime

- `CloseBashSessionRequest`: `session: str = "default"`,
  `session_type: Literal["bash"] = "bash"` (`abstract.py:126-129`).
- `CloseBashSessionResponse`: `session_type: Literal["bash"] = "bash"` only
  (`abstract.py:135-137`).
- `CloseResponse`: empty model (`abstract.py:220-221`).
- `close_session`: unknown session → `SessionDoesNotExistError` (→ 511);
  else closes pexpect shell (`shell.close()`, idempotent if already `None`),
  deletes session from dict (`local.py:410-417`, `353-358`).
- `close`: closes every open session, returns `CloseResponse()`
  (`local.py:474-478`). Called by `RemoteDeployment.stop()`, which then drops
  the client handle (`deployment/remote.py:70-73`).

### 3.7 Execute (session-independent)

- `Command`: `command: str | list[str]` (required),
  `timeout: float | None = None`, `shell: bool = False`, `check: bool = False`,
  `error_msg: str = ""`, `env: dict[str,str] | None = None`,
  `cwd: str | None = None`, `merge_output_streams: bool = False`
  (`abstract.py:143-177`).
- `CommandResponse`: `stdout: str = ""`, `stderr: str = ""`,
  `exit_code: int | None = None` (`abstract.py:179-182`; in practice always an
  int on success — `None` only on the `CommandTimeoutError` raise path, i.e.
  never returned).
- Behavior (`local.py:419-453`): `subprocess.run(command, shell=shell,
  timeout=timeout, env=env, stdin=DEVNULL, stdout=PIPE,
  stderr=STDOUT if merge else PIPE, cwd=cwd)`; decode with
  `errors="backslashreplace"` (`stderr=""` when merged).
  `subprocess.TimeoutExpired` → `CommandTimeoutError("Timeout ({timeout}s)
  exceeded while running command")`. If `check and returncode != 0` →
  `NonZeroExitCodeError` (message includes stdout/stderr; prefixed with
  `error_msg` when set). Note `check` is `bool` here, not the tri-state
  `BashAction.check`.

### 3.8 File primitives and upload

- `ReadFileRequest`: `path: str` (required), `encoding: str | None = None`,
  `errors: str | None = None` (both passed straight to `Path.read_text`)
  (`abstract.py:185-196`). `ReadFileResponse`: `content: str = ""`.
  Server: `Path(path).read_text(...)`; `FileNotFoundError`/`UnicodeError` etc.
  propagate as 511 transfers (`local.py:455-458`).
- `WriteFileRequest`: `content: str`, `path: str` (both required)
  (`abstract.py:202-204`). `WriteFileResponse`: empty. Server creates parents
  (`mkdir(parents=True, exist_ok=True)`), `write_text(content)` with default
  encoding (`local.py:460-464`).
- `UploadRequest` (client-side model only, never on the wire):
  `source_path: str`, `target_path: str` (`abstract.py:211-213`).
  `UploadResponse`: empty. Wire format is multipart (see endpoint 9).
  Client (`remote.py:223-261`): dir → zip via `shutil.make_archive` into a temp
  dir, post with `unzip="true"`; file → post with `unzip="false"`;
  neither → client-side `ValueError` (no HTTP call). Server: `mkdir` parents of
  `target_path`, buffer full upload to temp file (`await file.read()` —
  unbounded), then `zip_ref.extractall(target_path)` for unzip or
  `shutil.move(tmp, target_path)` otherwise; always returns `UploadResponse()`.
- Path semantics: server joins nothing — `target_path`/`path` are used as given
  (absolute or relative to server cwd). No path sandboxing upstream.

### 3.9 Error transfer

- `_ExceptionTransfer`: `message: str = ""`, `class_path: str = ""`
  (`module + "." + qualname`), `traceback: str = ""`,
  `extra_info: dict[str, Any] = {}` (from `getattr(exc, "extra_info", {})`)
  (`abstract.py:224-231`).
- Server (`server.py:105-119`): any non-HTTP exception → `511` +
  `{"swerexception": dump}`. Known exception types (`exceptions.py`):
  `SwerexException`, `SessionNotInitializedError`, `NonZeroExitCodeError`,
  `BashIncorrectSyntaxError` (carries `extra_info={bash_stdout, bash_stderr}`),
  `CommandTimeoutError`, `NoExitCodeError`, `SessionExistsError`,
  `SessionDoesNotExistError`, plus builtin/`ValueError`/`pexpect.TIMEOUT`/
  `OSError` passthroughs (e.g. unknown session type, read-file errors, startup
  timeouts, interrupt fallback failure).
- Client (`remote.py:84-126`): on 511, re-import `class_path` module (fallback
  to `SwerexException` on import/attribute failure), instantiate with
  `(message)`, attach `extra_info`, raise. Logs remote traceback at CRITICAL.
  Non-511 `>= 400` → log + `raise_for_status()` (aiohttp HTTP error, NOT a
  transferred exception). `_request` helper retries `num_retries` times
  (default 0 = no retry) with exponential backoff + jitter on ANY exception,
  reusing the same `X-Request-ID`.
- Idempotency: `X-Request-ID` header → server caches the LAST response only
  (single-slot `ResponseManager`, not per-ID map) and replays it when the same
  ID repeats; concurrent clients can evict each other (`server.py:42-102`).
  Middleware also unconditionally buffers every response body. Only `_request`
  POSTs send the header; `is_alive` and `upload` do not.

## 4. Auth token handling

- Server CLI: `--auth-token` is `required=True` (default `""`), stored in global
  `AUTH_TOKEN` (`server.py:208-217`).
- Middleware (`server.py:67-74`): if `AUTH_TOKEN` is truthy, every request must
  present header `X-API-Key` equal to the token; mismatch → `401 "Invalid API Key"`.
  If `AUTH_TOKEN` is falsy (only reachable by importing `app` without `main()`),
  all requests pass. `api_key_header = APIKeyHeader(name="X-API-Key")` uses
  FastAPI defaults (`auto_error=True`), so a missing header with auth enabled is
  expected to fail closed — exact status (401 vs 403) needs a live probe (Q5).
- Client (`remote.py:71-76`, `config.py:22-30`): `RemoteRuntimeConfig.auth_token:
  str` (required, no default), `host: str = "http://127.0.0.1"` (prefixed with
  `http://` + warning if no `http` prefix), `port: int | None = None`
  (`_api_url` = `host` or `host:port`), `timeout: float = 0.15` (is_alive only).
  `_headers` = `{"X-API-Key": auth_token}` if set, else `{}`.
  `RemoteDeploymentConfig` (`deployment/config.py:160-176`) mirrors
  `auth_token`/`host`/`port`/`timeout` and `RemoteDeployment.start()` forwards
  them into `RemoteRuntime` (`deployment/remote.py:59-68`). `DockerDeployment`
  generates its own token and injects it into the container (out of scope here).
- agent-execd must: accept the token at startup, enforce `X-API-Key` on all 10
  routes when set, return 401 on mismatch, and match the missing-header status
  after probing (Q5).

## 5. Open compatibility questions (require running SWE-agent — do NOT guess)

1. `GET /` and version: does any SWE-agent/deployment path call `GET /`, and
   where does it read the server version from (no `/version` endpoint exists)?
2. `is_alive` polling: which deployments poll `is_alive`/`wait_until_alive`
   during startup, with what timeouts, and does the 0.15 s default client timeout
   vs. POSTs-having-no-timeout matter in practice?
3. `POST /execute`: does SWE-agent ever call `execute`, or only session-based
   `run_in_session`? With what `shell`/`check`/`merge_output_streams`/`cwd`/`env`
   combinations?
4. File mix: does SWE-agent use `read_file`/`write_file`/`upload` (and with what
   `encoding`/`errors` values), or does it move files via shell commands? Dir or
   file uploads, typical sizes, `target_path` absolute vs relative?
5. Auth edge: exact status/body for a missing (vs wrong) `X-API-Key` when auth
   is enabled; does any caller rely on the no-auth (`AUTH_TOKEN=""`) path?
6. `X-Request-ID` reliance: does SWE-agent depend on retry idempotency (client
   default is 0 retries — who passes `num_retries > 0`, and does the single-slot
   replay cache cause observable behavior)?
7. `BashAction` field usage: which `check` modes (`silent`/`raise`/`ignore`),
   `timeout` values (incl. `None`), `expect` strings, and interactive flags
   (`is_interactive_command`/`is_interactive_quit`) does SWE-agent actually send?
8. Session usage: session names beyond `"default"`, `startup_source` contents,
   multiple concurrent sessions, `close_session` vs `close`-only teardown?
9. Output fidelity: does SWE-agent depend on ANSI stripping, `\r\n`→`\n`
   normalization, `backslashreplace` decoding, PS1/UNIQUE-string scrubbing, or
   the exact `BashObservation` `output`/`expect_string`/`failure_reason` shapes?
10. Exit-code paths: reliance on `NoExitCodeError` vs `exit_code=None` under
    `check="silent"`, on the `NonZeroExitCodeError` message format, or on
    `BashIncorrectSyntaxError.extra_info={bash_stdout, bash_stderr}` keys?
11. Interrupt flow: `BashInterruptAction` `timeout`/`n_retry` values used, and is
    the Ctrl-Z/`kill -9 %1` fallback or the 0.5 s trailing-output drain load-bearing?
12. Concurrency: parallel `run_in_session` calls against one session, and any
    dependence on the full-body buffering + single-slot replay middleware under
    load?
