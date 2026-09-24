//! agent-execd server: full P1 HTTP surface over execd-core.
//! See docs/p1-compat.md §§1, 3.3, 3.5–3.9, 4.

use axum::{
    extract::{Multipart, State},
    http::{Request, StatusCode},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use clap::Parser;
use execd_core::{ExecdError, SessionManager};
use execd_protocol::{
    BashAction, BashInterruptAction, CloseBashSessionRequest, CloseResponse, Command, CommandArg,
    CommandResponse, CreateBashSessionRequest, ExceptionTransfer, IsAliveResponse, ReadFileRequest,
    ReadFileResponse, SwerexceptionEnvelope, UploadResponse, WriteFileRequest, WriteFileResponse,
};
use std::process::Stdio;
use std::sync::Arc;

#[derive(Debug, Parser)]
#[command(name = "agent-execd")]
struct Args {
    #[arg(long, default_value = "127.0.0.1")]
    host: String,
    #[arg(long, default_value_t = 8000)]
    port: u16,
    /// When non-empty, clients must send a matching X-API-Key header.
    #[arg(long, default_value = "")]
    auth_token: String,
}

#[derive(Debug, Clone)]
struct AppState {
    auth_token: Arc<String>,
    manager: Arc<SessionManager>,
}

/// §3.9 mapper: unhandled error -> 511 + `{"swerexception": {...}}`.
fn swerexception_511(message: &str, class_path: &str) -> Response {
    let body = SwerexceptionEnvelope {
        swerexception: ExceptionTransfer {
            message: message.to_string(),
            class_path: class_path.to_string(),
            traceback: String::new(),
            extra_info: serde_json::Map::new(),
        },
    };
    (StatusCode::from_u16(511).unwrap(), Json(body)).into_response()
}

/// Map an [`ExecdError`] to the 511 envelope via [`ExecdError::to_transfer`].
fn execd_511(err: &ExecdError) -> Response {
    let body = SwerexceptionEnvelope {
        swerexception: err.to_transfer(),
    };
    (StatusCode::from_u16(511).unwrap(), Json(body)).into_response()
}

/// Map an I/O error to the closest builtin 511 `class_path`, mirroring the
/// passthroughs upstream lets escape (`FileNotFoundError`, `OSError`, ...).
fn io_511(err: &std::io::Error, path: &str) -> Response {
    let class_path = match err.kind() {
        std::io::ErrorKind::NotFound => "builtins.FileNotFoundError",
        _ => "builtins.OSError",
    };
    swerexception_511(&format!("{err} ({path:?})"), class_path)
}

async fn require_api_key(
    State(state): State<AppState>,
    req: Request<axum::body::Body>,
    next: Next,
) -> Response {
    if !state.auth_token.is_empty() {
        let ok = req.headers().get("X-API-Key").and_then(|v| v.to_str().ok())
            == Some(state.auth_token.as_str());
        if !ok {
            // Upstream FastAPI body for a bad key: {"detail": "Invalid API Key"}.
            return (
                StatusCode::UNAUTHORIZED,
                Json(serde_json::json!({"detail": "Invalid API Key"})),
            )
                .into_response();
        }
    }
    next.run(req).await
}

async fn root() -> Json<serde_json::Value> {
    Json(serde_json::json!({"message": "hello world"}))
}

async fn is_alive() -> Json<IsAliveResponse> {
    Json(IsAliveResponse {
        is_alive: true,
        message: String::new(),
    })
}

async fn create_session(
    State(state): State<AppState>,
    Json(req): Json<CreateBashSessionRequest>,
) -> Response {
    match state.manager.create_session(&req).await {
        Ok(resp) => (StatusCode::OK, Json(resp)).into_response(),
        // Unknown session type arrives here as builtins.ValueError (§3.2).
        Err(err) => execd_511(&err),
    }
}

/// Single endpoint for run AND interrupt, discriminated by the
/// `action_type` field (§§3.3/3.5). Absent `action_type` defaults to
/// `"bash"`, like the pydantic model. An invalid discriminator value is a
/// handler `ValueError` -> 511 (not 422).
async fn run_in_session(
    State(state): State<AppState>,
    Json(body): Json<serde_json::Value>,
) -> Response {
    let action_type = body
        .get("action_type")
        .and_then(|v| v.as_str())
        .unwrap_or("bash");
    match action_type {
        "bash" => {
            let action: BashAction = match serde_json::from_value(body) {
                Ok(a) => a,
                Err(e) => {
                    return swerexception_511(
                        &format!("invalid BashAction: {e}"),
                        "builtins.ValueError",
                    )
                }
            };
            match state.manager.run_bash(&action).await {
                Ok(obs) => (StatusCode::OK, Json(obs)).into_response(),
                Err(err) => execd_511(&err),
            }
        }
        "bash_interrupt" => {
            let action: BashInterruptAction = match serde_json::from_value(body) {
                Ok(a) => a,
                Err(e) => {
                    return swerexception_511(
                        &format!("invalid BashInterruptAction: {e}"),
                        "builtins.ValueError",
                    )
                }
            };
            match state.manager.interrupt(&action).await {
                Ok(obs) => (StatusCode::OK, Json(obs)).into_response(),
                Err(err) => execd_511(&err),
            }
        }
        other => swerexception_511(
            &format!("unknown action_type: {other:?} (expected \"bash\" or \"bash_interrupt\")"),
            "builtins.ValueError",
        ),
    }
}

async fn close_session(
    State(state): State<AppState>,
    Json(req): Json<CloseBashSessionRequest>,
) -> Response {
    match state.manager.close_session(&req).await {
        Ok(resp) => (StatusCode::OK, Json(resp)).into_response(),
        Err(err) => execd_511(&err),
    }
}

async fn close(State(state): State<AppState>) -> Json<CloseResponse> {
    state.manager.close_all().await;
    Json(CloseResponse {})
}

/// Python `bytes.decode(errors="backslashreplace")`: valid UTF-8 passes
/// through, each invalid byte becomes `\xNN`.
fn decode_backslashreplace(bytes: &[u8]) -> String {
    let mut out = String::new();
    let mut rest = bytes;
    loop {
        match std::str::from_utf8(rest) {
            Ok(s) => {
                out.push_str(s);
                break;
            }
            Err(e) => {
                let valid = e.valid_up_to();
                // SAFETY: `valid_up_to` is always a valid UTF-8 boundary.
                out.push_str(unsafe { std::str::from_utf8_unchecked(&rest[..valid]) });
                let len = e.error_len().unwrap_or(rest.len() - valid).max(1);
                for b in rest.iter().skip(valid).take(len) {
                    out.push_str(&format!("\\x{b:02x}"));
                }
                rest = &rest[valid + len..];
            }
        }
    }
    out
}

/// Python `bytes.decode(errors="ignore")`: invalid bytes are dropped.
fn decode_ignore(bytes: &[u8]) -> String {
    let mut out = String::new();
    let mut rest = bytes;
    loop {
        match std::str::from_utf8(rest) {
            Ok(s) => {
                out.push_str(s);
                break;
            }
            Err(e) => {
                let valid = e.valid_up_to();
                // SAFETY: `valid_up_to` is always a valid UTF-8 boundary.
                out.push_str(unsafe { std::str::from_utf8_unchecked(&rest[..valid]) });
                let len = e.error_len().unwrap_or(rest.len() - valid).max(1);
                rest = &rest[valid + len..];
            }
        }
    }
    out
}

/// One-shot subprocess, session-independent, with `subprocess.run` semantics
/// (§3.7): `shell` via `/bin/sh -c`, timeout kill, `env`/`cwd`,
/// `merge_output_streams`, `check` -> `NonZeroExitCodeError`, `error_msg` prefix.
async fn execute(Json(req): Json<Command>) -> Response {
    let mut cmd = tokio::process::Command::new("/bin/sh");
    match (&req.command, req.shell) {
        (CommandArg::Text(s), true) => {
            cmd.arg("-c").arg(s);
        }
        (CommandArg::Argv(argv), true) => {
            let Some(first) = argv.first() else {
                return swerexception_511(
                    "empty command list with shell=True",
                    "builtins.ValueError",
                );
            };
            // Matches subprocess: `/bin/sh -c <argv[0]> <argv[1:]>`.
            cmd.arg("-c").arg(first);
            if argv.len() > 1 {
                cmd.args(&argv[1..]);
            }
        }
        // subprocess-faithful: with shell=False a string must name the
        // program itself (no splitting, no arguments).
        (CommandArg::Text(s), false) => {
            cmd = tokio::process::Command::new(s);
        }
        (CommandArg::Argv(argv), false) => {
            let Some(first) = argv.first() else {
                return swerexception_511("empty command list", "builtins.ValueError");
            };
            cmd = tokio::process::Command::new(first);
            if argv.len() > 1 {
                cmd.args(&argv[1..]);
            }
        }
    }
    cmd.stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    // subprocess.run(env=...) replaces the whole environment.
    if let Some(env) = req.env.as_ref() {
        cmd.env_clear().envs(env);
    }
    if let Some(cwd) = req.cwd.as_deref() {
        cmd.current_dir(cwd);
    }
    cmd.kill_on_drop(true);

    let mut child = match cmd.spawn() {
        Ok(c) => c,
        Err(e) => return io_511(&e, "<spawn>"),
    };
    // Drain both pipes concurrently (avoids deadlock on large output) while
    // keeping `child` available for the timeout kill.
    let stdout_pipe = child.stdout.take();
    let stderr_pipe = child.stderr.take();
    let stdout_reader = tokio::spawn(async move {
        let mut buf = Vec::new();
        match stdout_pipe {
            Some(mut p) => {
                use tokio::io::AsyncReadExt as _;
                p.read_to_end(&mut buf).await.map(|_| buf)
            }
            None => Ok(buf),
        }
    });
    let stderr_reader = tokio::spawn(async move {
        let mut buf = Vec::new();
        match stderr_pipe {
            Some(mut p) => {
                use tokio::io::AsyncReadExt as _;
                p.read_to_end(&mut buf).await.map(|_| buf)
            }
            None => Ok(buf),
        }
    });
    let timeout = req
        .timeout
        .map(|t| std::time::Duration::from_secs_f64(t.max(0.0)));
    let status = match timeout {
        Some(d) => match tokio::time::timeout(d, child.wait()).await {
            Ok(Ok(s)) => s,
            Ok(Err(e)) => {
                stdout_reader.abort();
                stderr_reader.abort();
                return io_511(&e, "<wait>");
            }
            Err(_) => {
                let _ = child.kill().await;
                let _ = child.wait().await;
                stdout_reader.abort();
                stderr_reader.abort();
                // Python-repr float: integral timeouts print as "30.0".
                let t = req.timeout.unwrap_or(0.0);
                let t_repr = if t.fract() == 0.0 {
                    format!("{t:.1}")
                } else {
                    format!("{t}")
                };
                let err = ExecdError::CommandTimeout(format!(
                    "Timeout ({t_repr}s) exceeded while running command"
                ));
                return execd_511(&err);
            }
        },
        None => match child.wait().await {
            Ok(s) => s,
            Err(e) => {
                stdout_reader.abort();
                stderr_reader.abort();
                return io_511(&e, "<wait>");
            }
        },
    };
    let (stdout_bytes, stderr_bytes) = match tokio::join!(stdout_reader, stderr_reader) {
        (Ok(Ok(o)), Ok(Ok(e))) => (o, e),
        _ => {
            return io_511(
                &std::io::Error::new(std::io::ErrorKind::BrokenPipe, "failed reading output"),
                "<pipe>",
            )
        }
    };

    #[cfg(unix)]
    let exit_code: Option<i32> = status.code().or_else(|| {
        use std::os::unix::process::ExitStatusExt as _;
        status.signal().map(|s| -s)
    });
    #[cfg(not(unix))]
    let exit_code: Option<i32> = status.code();

    let mut stdout = decode_backslashreplace(&stdout_bytes);
    let mut stderr = decode_backslashreplace(&stderr_bytes);
    if req.merge_output_streams {
        // Deviation: concatenated (stdout then stderr) rather than
        // file-descriptor-interleaved; `stderr` is still `""` as upstream.
        stdout.push_str(&stderr);
        stderr.clear();
    }

    if req.check && exit_code != Some(0) {
        let code = exit_code.map_or_else(|| "None".to_string(), |c| c.to_string());
        let command_repr = match &req.command {
            CommandArg::Text(s) => format!("{s:?}"),
            CommandArg::Argv(v) => format!("{v:?}"),
        };
        let mut msg = format!(
            "Command {command_repr} failed with exit code {code}. Stdout:\n{stdout:?}\nStderr:\n{stderr:?}"
        );
        if !req.error_msg.is_empty() {
            msg = format!("{}: {msg}", req.error_msg);
        }
        let err = ExecdError::NonZeroExit {
            message: msg,
            exit_code,
        };
        return execd_511(&err);
    }

    let resp = CommandResponse {
        stdout,
        stderr,
        exit_code,
    };
    (StatusCode::OK, Json(resp)).into_response()
}

/// Decode `read_file` bytes honoring the `errors` argument (§3.8).
/// Only UTF-8 is supported; other encodings are rejected (see README).
fn decode_read(bytes: Vec<u8>, errors: Option<&str>) -> Result<String, (String, &'static str)> {
    match errors.unwrap_or("strict") {
        "strict" => String::from_utf8(bytes).map_err(|e| {
            (
                format!("file is not valid UTF-8: {e}"),
                "builtins.UnicodeDecodeError",
            )
        }),
        "backslashreplace" => Ok(decode_backslashreplace(&bytes)),
        "replace" => Ok(String::from_utf8_lossy(&bytes).into_owned()),
        "ignore" => Ok(decode_ignore(&bytes)),
        other => Err((
            format!("unsupported errors mode: {other:?}"),
            "builtins.ValueError",
        )),
    }
}

async fn read_file(Json(req): Json<ReadFileRequest>) -> Response {
    if let Some(enc) = req.encoding.as_deref() {
        let norm = enc.to_lowercase().replace('_', "-");
        if norm != "utf-8" && norm != "utf8" {
            return swerexception_511(
                &format!("unsupported encoding: {enc:?} (only utf-8 is supported)"),
                "builtins.ValueError",
            );
        }
    }
    let bytes = match tokio::fs::read(&req.path).await {
        Ok(b) => b,
        Err(e) => return io_511(&e, &req.path),
    };
    match decode_read(bytes, req.errors.as_deref()) {
        Ok(content) => (StatusCode::OK, Json(ReadFileResponse { content })).into_response(),
        Err((message, class_path)) => swerexception_511(&message, class_path),
    }
}

async fn write_file(Json(req): Json<WriteFileRequest>) -> Response {
    let parent = std::path::Path::new(&req.path).parent();
    if let Some(dir) = parent {
        if !dir.as_os_str().is_empty() {
            if let Err(e) = tokio::fs::create_dir_all(dir).await {
                return io_511(&e, &req.path);
            }
        }
    }
    if let Err(e) = tokio::fs::write(&req.path, req.content.as_bytes()).await {
        return io_511(&e, &req.path);
    }
    (StatusCode::OK, Json(WriteFileResponse {})).into_response()
}

/// Parse a multipart `unzip` form field the way pydantic parses a bool form
/// field (`"true"`/`"false"`, plus common spellings).
fn parse_unzip(text: &str) -> Result<bool, String> {
    match text.trim().to_lowercase().as_str() {
        "true" | "1" | "yes" | "on" => Ok(true),
        "false" | "0" | "no" | "off" => Ok(false),
        other => Err(format!("invalid unzip value: {other:?} (expected a bool)")),
    }
}

/// Multipart upload (§3.8, endpoint 9): `file` (bytes, required),
/// `target_path` (str field, required), `unzip` (bool field, default false).
/// Streams the upload to a temp file, then zip-extracts or moves it to
/// `target_path` (parents created). Always returns `{}` on success.
async fn upload(mut multipart: Multipart) -> Response {
    let mut target_path: Option<String> = None;
    let mut unzip = false;
    let mut tmp: Option<tempfile::NamedTempFile> = None;
    let mut got_file = false;

    loop {
        let field = match multipart.next_field().await {
            Ok(f) => f,
            Err(e) => {
                return swerexception_511(
                    &format!("invalid multipart body: {e}"),
                    "builtins.ValueError",
                )
            }
        };
        let Some(mut field) = field else { break };
        match field.name() {
            Some("file") => {
                let slot = match tmp.as_mut() {
                    Some(t) => t,
                    None => {
                        let t = match tempfile::NamedTempFile::new() {
                            Ok(t) => t,
                            Err(e) => return io_511(&e, "<tempfile>"),
                        };
                        tmp.insert(t)
                    }
                };
                loop {
                    let chunk = match field.chunk().await {
                        Ok(c) => c,
                        Err(e) => {
                            return swerexception_511(
                                &format!("failed reading upload chunk: {e}"),
                                "builtins.ValueError",
                            )
                        }
                    };
                    let Some(bytes) = chunk else { break };
                    use std::io::Write as _;
                    if let Err(e) = slot.write_all(&bytes) {
                        return io_511(&e, "<tempfile>");
                    }
                }
                got_file = true;
            }
            Some("target_path") => match field.text().await {
                Ok(t) => target_path = Some(t),
                Err(e) => {
                    return swerexception_511(
                        &format!("invalid target_path field: {e}"),
                        "builtins.ValueError",
                    )
                }
            },
            Some("unzip") => match field.text().await {
                Ok(t) => match parse_unzip(&t) {
                    Ok(u) => unzip = u,
                    Err(msg) => return swerexception_511(&msg, "builtins.ValueError"),
                },
                Err(e) => {
                    return swerexception_511(
                        &format!("invalid unzip field: {e}"),
                        "builtins.ValueError",
                    )
                }
            },
            _ => {}
        }
    }

    let Some(target_path) = target_path else {
        return swerexception_511(
            "missing multipart field: target_path",
            "builtins.ValueError",
        );
    };
    if !got_file {
        return swerexception_511("missing multipart field: file", "builtins.ValueError");
    }
    let tmp = tmp.expect("got_file implies tmp exists");

    if let Some(dir) = std::path::Path::new(&target_path).parent() {
        if !dir.as_os_str().is_empty() {
            if let Err(e) = tokio::fs::create_dir_all(dir).await {
                return io_511(&e, &target_path);
            }
        }
    }

    if unzip {
        let tmp_path = tmp.into_temp_path();
        let target = target_path.clone();
        let joined = tokio::task::spawn_blocking(move || {
            let file = std::fs::File::open(&tmp_path)?;
            let mut archive = zip::ZipArchive::new(file).map_err(|e| {
                std::io::Error::new(std::io::ErrorKind::InvalidData, format!("bad zip: {e}"))
            })?;
            archive.extract(&target)?;
            Ok::<_, std::io::Error>(())
        })
        .await;
        match joined {
            Ok(Ok(())) => {}
            Ok(Err(e)) if e.kind() == std::io::ErrorKind::InvalidData => {
                return swerexception_511(&e.to_string(), "zipfile.BadZipFile")
            }
            Ok(Err(e)) => return io_511(&e, &target_path),
            Err(e) => return swerexception_511(&e.to_string(), "builtins.OSError"),
        }
    } else {
        let tmp_path = tmp.into_temp_path();
        // rename, with copy+delete fallback for cross-device moves (shutil.move).
        if tokio::fs::rename(&tmp_path, &target_path).await.is_err() {
            if let Err(e) = tokio::fs::copy(&tmp_path, &target_path).await {
                return io_511(&e, &target_path);
            }
        }
    }
    (StatusCode::OK, Json(UploadResponse {})).into_response()
}

#[tokio::main]
async fn main() {
    let args = Args::parse();
    let state = AppState {
        auth_token: Arc::new(args.auth_token),
        manager: Arc::new(SessionManager::new()),
    };
    let app = Router::new()
        .route("/", get(root))
        .route("/is_alive", get(is_alive))
        .route("/create_session", post(create_session))
        .route("/run_in_session", post(run_in_session))
        .route("/close_session", post(close_session))
        .route("/execute", post(execute))
        .route("/read_file", post(read_file))
        .route("/write_file", post(write_file))
        .route("/upload", post(upload))
        .route("/close", post(close))
        .layer(middleware::from_fn_with_state(
            state.clone(),
            require_api_key,
        ))
        .with_state(state);
    let addr = format!("{}:{}", args.host, args.port);
    let listener = tokio::net::TcpListener::bind(&addr)
        .await
        .expect("bind failed");
    println!("agent-execd listening on {addr}");
    axum::serve(listener, app).await.expect("serve failed");
}
