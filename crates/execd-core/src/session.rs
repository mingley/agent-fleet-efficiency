//! Persistent bash session on a PTY.
//!
//! Design: `/usr/bin/env bash` is spawned under portable-pty as a session
//! leader (so `killpg` covers the whole tree). A dedicated reader thread
//! pumps PTY bytes into a shared buffer; `expect` calls block on a condvar
//! and are always driven via `spawn_blocking` so the async runtime never
//! stalls. Normal runs and interactive runs are serialized by an async
//! mutex (matching upstream's effective serialization); `interrupt`
//! bypasses it and only peeks at the buffer so it never steals an
//! in-flight run's exit marker.

use crate::error::ExecdError;
use crate::output;
use crate::proctree;
use crate::{EXIT_CODE_PREFIX, EXIT_CODE_SUFFIX, PS1_MARKER, UNIQUE_STRING};
use execd_protocol::{BashAction, BashInterruptAction, BashObservation};
use nix::sys::signal::Signal;
use nix::unistd::Pid;
use portable_pty::{native_pty_system, CommandBuilder, PtySize};
use std::io::{Read, Write};
use std::os::fd::RawFd;
use std::sync::{
    atomic::{AtomicBool, AtomicU64, Ordering},
    Arc, Condvar, Mutex,
};
use std::time::{Duration, Instant};

const PTY_ROWS: u16 = 24;
/// Wide default so long lines do not wrap (wrapped lines would inject
/// extra newlines into captured output).
const PTY_COLS: u16 = 200;
/// Bound on the `EXITCODEEND` suffix wait (upstream: 1 s).
const EXIT_SUFFIX_TIMEOUT: Duration = Duration::from_secs(1);
/// Bound on re-syncing to the prompt after the exit marker (upstream: 0.1 s;
/// relaxed for loaded machines).
const PROMPT_SETTLE_TIMEOUT: Duration = Duration::from_secs(2);
/// Sleep between interrupt retries (upstream: 0.2 s).
const INTERRUPT_RETRY_SLEEP: Duration = Duration::from_millis(200);
/// Trailing-output drain after a successful interrupt (upstream: 0.5 s).
const INTERRUPT_DRAIN: Duration = Duration::from_millis(500);
/// How long `close` waits for the shell to be reaped after SIGKILL.
const CLOSE_REAP_GRACE: Duration = Duration::from_secs(5);
/// Timeout recovery: grace after SIGKILL for the shell to print its job
/// message + late marker, and drain after resync before discarding.
const RECOVERY_GRACE: Duration = Duration::from_millis(150);
const RECOVERY_DRAIN: Duration = Duration::from_millis(150);
/// Bound on the interactive-quit handshake expects (upstream: 1 s each).
const QUIT_HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(1);

/// `BashAction.check` modes (§3.3).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum CheckMode {
    Silent,
    Raise,
    Ignore,
}

fn parse_check(check: &str) -> Result<CheckMode, ExecdError> {
    match check {
        "silent" => Ok(CheckMode::Silent),
        "raise" => Ok(CheckMode::Raise),
        "ignore" => Ok(CheckMode::Ignore),
        other => Err(ExecdError::ValueError(format!(
            "unknown check mode: {other:?} (expected \"silent\", \"raise\", or \"ignore\")"
        ))),
    }
}

/// `None` (or missing) timeout means no timeout (§3.3).
fn timeout_opt(t: Option<f64>) -> Option<Duration> {
    t.map(timeout_f64)
}

fn timeout_f64(t: f64) -> Duration {
    if t.is_finite() && t > 0.0 {
        Duration::from_secs_f64(t)
    } else if t.is_sign_negative() || t == 0.0 || t.is_nan() {
        Duration::from_secs(0)
    } else {
        Duration::from_secs(u64::MAX / 2)
    }
}

fn timeout_secs_for_msg(t: Option<Duration>) -> String {
    match t {
        Some(d) => format!("{}", d.as_secs_f64()),
        None => "None".to_string(),
    }
}

fn nonzero_message(command: &str, error_msg: &str, exit_code: Option<i32>, output: &str) -> String {
    let code = match exit_code {
        Some(c) => c.to_string(),
        None => "None".to_string(),
    };
    let msg = format!(
        "Command {} failed with exit code {}. Here is the output:\n{}",
        output::py_repr(command),
        code,
        output::py_repr(output)
    );
    if error_msg.is_empty() {
        msg
    } else {
        format!("{error_msg}: {msg}")
    }
}

// ---------------------------------------------------------------------------
// PTY reader: background pump + shared buffer + expect engine
// ---------------------------------------------------------------------------

#[derive(Debug)]
struct ReaderState {
    buf: String,
    consumed: usize,
    /// Trailing bytes of an incomplete UTF-8 sequence held back across reads.
    pending: Vec<u8>,
    eof: bool,
    closed: bool,
    /// Unconsumed bytes dropped under memory pressure (truncation accounting).
    dropped_total: u64,
}

#[derive(Debug)]
struct ReaderShared {
    state: Mutex<ReaderState>,
    cvar: Condvar,
    /// Buffer hard cap; past this the front is dropped (tail kept).
    hard_cap: usize,
}

impl ReaderShared {
    fn new(output_cap: usize) -> Self {
        Self {
            state: Mutex::new(ReaderState {
                buf: String::new(),
                consumed: 0,
                pending: Vec::new(),
                eof: false,
                closed: false,
                dropped_total: 0,
            }),
            cvar: Condvar::new(),
            hard_cap: (output_cap.saturating_mul(2)).max(65536),
        }
    }

    fn append(&self, chunk: &[u8]) {
        let mut guard = self.state.lock().unwrap();
        if guard.closed || guard.eof {
            return;
        }
        let mut bytes = std::mem::take(&mut guard.pending);
        bytes.extend_from_slice(chunk);
        let tail = output::incomplete_tail_len(&bytes);
        let split = bytes.len() - tail;
        guard.pending = bytes[split..].to_vec();
        guard
            .buf
            .push_str(&output::decode_backslashreplace(&bytes[..split]));
        Self::gc(&mut guard, self.hard_cap);
        self.cvar.notify_all();
    }

    fn mark_eof(&self) {
        let mut guard = self.state.lock().unwrap();
        if !guard.eof {
            let pending = std::mem::take(&mut guard.pending);
            guard
                .buf
                .push_str(&output::decode_backslashreplace(&pending));
            guard.eof = true;
            self.cvar.notify_all();
        }
    }

    fn notify_close(&self) {
        let mut guard = self.state.lock().unwrap();
        guard.closed = true;
        self.cvar.notify_all();
    }

    fn dropped_total(&self) -> u64 {
        self.state.lock().unwrap().dropped_total
    }

    /// Snapshot of unconsumed text without consuming (interrupt peeks).
    fn peek_from(&self, skip: usize) -> String {
        let guard = self.state.lock().unwrap();
        let from = guard.consumed.saturating_add(skip).min(guard.buf.len());
        guard.buf[from..].to_string()
    }

    /// Forget all buffered text (timeout recovery).
    fn discard_through_end(&self) {
        let mut guard = self.state.lock().unwrap();
        guard.consumed = guard.buf.len();
    }

    fn gc(state: &mut ReaderState, hard: usize) {
        if state.consumed >= 262144 {
            state.buf.drain(..state.consumed);
            state.consumed = 0;
        }
        if state.buf.len() <= hard {
            return;
        }
        if state.consumed > 0 {
            state.buf.drain(..state.consumed);
            state.consumed = 0;
        }
        if state.buf.len() <= hard {
            return;
        }
        let mut cut = state.buf.len() - hard / 2;
        while cut < state.buf.len() && !state.buf.is_char_boundary(cut) {
            cut += 1;
        }
        state.dropped_total += cut.saturating_sub(state.consumed) as u64;
        state.buf.drain(..cut);
        state.consumed = 0;
    }
}

fn reader_loop(mut reader: Box<dyn Read + Send>, shared: &Arc<ReaderShared>) {
    let mut chunk = [0u8; 8192];
    loop {
        match reader.read(&mut chunk) {
            Ok(0) => {
                shared.mark_eof();
                break;
            }
            Ok(n) => shared.append(&chunk[..n]),
            Err(_) => {
                // EIO once the slave side closes also lands here.
                shared.mark_eof();
                break;
            }
        }
    }
}

#[derive(Debug)]
enum ExpectError {
    Timeout,
    Eof,
    Closed,
}

#[derive(Debug)]
struct ExpectMatch {
    index: usize,
    matched: String,
    before: String,
}

/// Earliest match wins; ties go to the lowest pattern index (pexpect order).
fn find_earliest(haystack: &[u8], patterns: &[String]) -> Option<(usize, usize)> {
    let mut best: Option<(usize, usize)> = None;
    for (i, pat) in patterns.iter().enumerate() {
        let pos = if pat.is_empty() {
            Some(0)
        } else {
            let needle = pat.as_bytes();
            if needle.len() > haystack.len() {
                None
            } else {
                haystack.windows(needle.len()).position(|w| w == needle)
            }
        };
        if let Some(p) = pos {
            let replace = match best {
                None => true,
                Some((bp, bi)) => p < bp || (p == bp && i < bi),
            };
            if replace {
                best = Some((p, i));
            }
        }
    }
    best
}

/// Blocking expect on the shared buffer. `consume` advances past the match;
/// peeks leave the buffer untouched. Searches incrementally with overlap so
/// huge outputs do not rescans quadratically.
fn expect_blocking(
    shared: &Arc<ReaderShared>,
    patterns: &[String],
    timeout: Option<Duration>,
    consume: bool,
) -> Result<ExpectMatch, ExpectError> {
    let deadline = timeout.map(|t| Instant::now() + t);
    let max_pat = patterns.iter().map(|p| p.len()).max().unwrap_or(1).max(1);
    let mut search_from: Option<usize> = None;
    let mut guard = shared.state.lock().unwrap();
    loop {
        if guard.closed {
            return Err(ExpectError::Closed);
        }
        let from = search_from
            .unwrap_or(guard.consumed)
            .max(guard.consumed)
            .min(guard.buf.len());
        if let Some((rel, idx)) = find_earliest(&guard.buf.as_bytes()[from..], patterns) {
            let pos = from + rel;
            let before = guard.buf[guard.consumed..pos].to_string();
            let matched = patterns[idx].clone();
            if consume {
                guard.consumed = pos + patterns[idx].len();
            }
            return Ok(ExpectMatch {
                index: idx,
                matched,
                before,
            });
        }
        if guard.eof {
            return Err(ExpectError::Eof);
        }
        search_from = Some(
            guard
                .buf
                .len()
                .saturating_sub(max_pat.saturating_sub(1))
                .max(guard.consumed),
        );
        match deadline {
            None => {
                guard = shared.cvar.wait(guard).unwrap();
            }
            Some(d) => {
                let now = Instant::now();
                if now >= d {
                    return Err(ExpectError::Timeout);
                }
                let (g, _) = shared.cvar.wait_timeout(guard, d - now).unwrap();
                guard = g;
            }
        }
    }
}

// ---------------------------------------------------------------------------
// BashSession
// ---------------------------------------------------------------------------

struct SessionInner {
    name: String,
    output_cap: usize,
    reader: Arc<ReaderShared>,
    writer: Mutex<Option<Box<dyn Write + Send>>>,
    master: Mutex<Option<Box<dyn portable_pty::MasterPty>>>,
    child: Mutex<Option<Box<dyn portable_pty::Child + Send + Sync>>>,
    shell_pid: u32,
    seq: AtomicU64,
    run_lock: tokio::sync::Mutex<()>,
    initialized: AtomicBool,
    closed: AtomicBool,
}

impl Drop for SessionInner {
    fn drop(&mut self) {
        // Best-effort orphan prevention if `close` was never called. The
        // direct child may be left for init to reap; prefer `close()`.
        self.closed.store(true, Ordering::SeqCst);
        self.reader.notify_close();
        proctree::kill_process_group(Pid::from_raw(self.shell_pid as i32), Signal::SIGKILL);
    }
}

/// A persistent bash session. Clone is cheap (handle to shared state).
#[derive(Clone)]
pub struct BashSession {
    inner: Arc<SessionInner>,
}

impl std::fmt::Debug for BashSession {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("BashSession")
            .field("name", &self.inner.name)
            .finish_non_exhaustive()
    }
}

impl BashSession {
    /// Spawn `/usr/bin/env bash` under a PTY and sync to the first prompt.
    /// Returns the session plus stripped startup output (§3.2).
    pub async fn start(
        name: String,
        startup_source: &[String],
        startup_timeout: Duration,
        output_cap: usize,
    ) -> Result<(Self, String), ExecdError> {
        let session = Self::spawn(name, output_cap)?;
        match session.init(startup_source, startup_timeout).await {
            Ok(output) => Ok((session, output)),
            Err(e) => {
                session.close().await;
                Err(e)
            }
        }
    }

    /// Session name.
    pub fn name(&self) -> &str {
        &self.inner.name
    }

    /// Dispatch a `BashAction` (§§3.3–3.4).
    pub async fn run(&self, action: &BashAction) -> Result<BashObservation, ExecdError> {
        if action.is_interactive_command || action.is_interactive_quit {
            self.run_interactive(action).await
        } else {
            self.run_normal(action).await
        }
    }

    /// Normal run pipeline (§3.3): the command is sent as-is, followed by a
    /// sequenced exit marker in the same submission.
    pub async fn run_normal(&self, action: &BashAction) -> Result<BashObservation, ExecdError> {
        let check = parse_check(&action.check)?;
        self.ensure_usable()?;
        let _guard = self.inner.run_lock.lock().await;
        self.ensure_usable()?;
        if check == CheckMode::Ignore {
            self.run_ignore(action).await
        } else {
            self.run_checked(action, check).await
        }
    }

    /// Interactive run (§3.4): no exit extraction, always exit 0.
    pub async fn run_interactive(
        &self,
        action: &BashAction,
    ) -> Result<BashObservation, ExecdError> {
        if action.is_interactive_command && action.is_interactive_quit {
            return Err(ExecdError::ValueError(
                "is_interactive_command and is_interactive_quit are mutually exclusive".to_string(),
            ));
        }
        self.ensure_usable()?;
        let _guard = self.inner.run_lock.lock().await;
        self.ensure_usable()?;

        let timeout = timeout_opt(action.timeout);
        Self::write_bytes(&self.inner, format!("{}\n", action.command).into_bytes()).await?;
        let mut patterns = action.expect.clone();
        patterns.push(PS1_MARKER.to_string());
        let m = Self::expect_async(&self.inner.reader, patterns, timeout, true)
            .await
            .map_err(|e| self.map_run_err(e, &action.command, timeout))?;

        let mut output = output::strip_control_chars(&m.before);
        if action.is_interactive_quit {
            Self::write_bytes(
                &self.inner,
                format!("stty -echo; echo '{UNIQUE_STRING}'\n").into_bytes(),
            )
            .await?;
            Self::expect_async(
                &self.inner.reader,
                vec![UNIQUE_STRING.to_string()],
                Some(QUIT_HANDSHAKE_TIMEOUT),
                true,
            )
            .await
            .map_err(|_| {
                ExecdError::CommandTimeout("timeout during interactive quit handshake".to_string())
            })?;
            Self::expect_async(
                &self.inner.reader,
                vec![PS1_MARKER.to_string()],
                Some(QUIT_HANDSHAKE_TIMEOUT),
                true,
            )
            .await
            .map_err(|_| {
                ExecdError::CommandTimeout("timeout during interactive quit handshake".to_string())
            })?;
        } else {
            let trimmed = output.trim_start();
            let without_echo = trimmed.strip_prefix(&action.command).unwrap_or(trimmed);
            output = without_echo.trim().to_string();
        }
        let output = self.bound_output(output, None);
        Ok(BashObservation {
            output,
            exit_code: Some(0),
            failure_reason: String::new(),
            expect_string: m.matched,
            session_type: "bash".to_string(),
        })
    }

    /// Interrupt the foreground command (§3.5). Never consumes buffer text,
    /// so an in-flight run still sees its own exit marker.
    pub async fn interrupt(
        &self,
        action: &BashInterruptAction,
    ) -> Result<BashObservation, ExecdError> {
        self.ensure_usable()?;
        let timeout = Some(timeout_f64(action.timeout));
        let mut patterns = action.expect.clone();
        patterns.push(PS1_MARKER.to_string());
        let n_retry = action.n_retry.max(0) as usize;

        for _ in 0..n_retry {
            self.send_intr().await;
            match Self::expect_async(&self.inner.reader, patterns.clone(), timeout, false).await {
                Ok(m) => return Ok(self.finish_interrupt(&m).await),
                Err(ExpectError::Timeout) => {
                    let sleep = INTERRUPT_RETRY_SLEEP;
                    let _ = tokio::task::spawn_blocking(move || {
                        std::thread::sleep(sleep);
                    })
                    .await;
                }
                Err(ExpectError::Eof) | Err(ExpectError::Closed) => {
                    self.mark_dead();
                    return Err(ExecdError::SessionNotInitialized(
                        "shell exited while interrupting".to_string(),
                    ));
                }
            }
        }

        // Fallback: suspend the job, then kill it (§3.5).
        self.send_susp().await;
        match Self::expect_async(&self.inner.reader, patterns.clone(), timeout, false).await {
            Ok(_) => {}
            Err(ExpectError::Timeout) => {
                return Err(ExecdError::CommandTimeout(
                    "Failed to interrupt session".to_string(),
                ));
            }
            Err(ExpectError::Eof) | Err(ExpectError::Closed) => {
                self.mark_dead();
                return Err(ExecdError::SessionNotInitialized(
                    "shell exited while interrupting".to_string(),
                ));
            }
        }
        Self::write_bytes(&self.inner, b"kill -9 %1\n".to_vec())
            .await
            .map_err(|_| ExecdError::CommandTimeout("Failed to interrupt session".to_string()))?;
        match Self::expect_async(&self.inner.reader, patterns, timeout, false).await {
            Ok(m) => Ok(self.finish_interrupt(&m).await),
            Err(ExpectError::Timeout) => Err(ExecdError::CommandTimeout(
                "Failed to interrupt session".to_string(),
            )),
            Err(ExpectError::Eof) | Err(ExpectError::Closed) => {
                self.mark_dead();
                Err(ExecdError::SessionNotInitialized(
                    "shell exited while interrupting".to_string(),
                ))
            }
        }
    }

    /// Terminate the PTY, SIGKILL the process tree, and reap. Idempotent.
    pub async fn close(&self) {
        if self.inner.closed.swap(true, Ordering::SeqCst) {
            return;
        }
        self.inner.reader.notify_close();
        proctree::kill_process_group(Pid::from_raw(self.inner.shell_pid as i32), Signal::SIGKILL);
        let inner = Arc::clone(&self.inner);
        let _ = tokio::task::spawn_blocking(move || {
            if let Ok(mut guard) = inner.child.lock() {
                if let Some(mut child) = guard.take() {
                    let _ = child.kill();
                    proctree::reap_child(&mut child, CLOSE_REAP_GRACE);
                }
            }
            if let Ok(mut guard) = inner.writer.lock() {
                guard.take();
            }
            if let Ok(mut guard) = inner.master.lock() {
                guard.take();
            }
        })
        .await;
    }

    // -- internals ----------------------------------------------------------

    fn spawn(name: String, output_cap: usize) -> Result<Self, ExecdError> {
        let pty = native_pty_system();
        let pair = pty
            .openpty(PtySize {
                rows: PTY_ROWS,
                cols: PTY_COLS,
                pixel_width: 0,
                pixel_height: 0,
            })
            .map_err(|e| ExecdError::SessionNotInitialized(format!("failed to open pty: {e}")))?;
        setup_termios(&*pair.master);
        let mut cmd = CommandBuilder::new("/usr/bin/env");
        cmd.arg("bash");
        for (k, v) in std::env::vars() {
            cmd.env(k, v);
        }
        cmd.env("PS1", PS1_MARKER);
        cmd.env("PS2", "");
        cmd.env("PS0", "");
        let child = pair
            .slave
            .spawn_command(cmd)
            .map_err(|e| ExecdError::SessionNotInitialized(format!("failed to spawn bash: {e}")))?;
        let shell_pid = child.process_id().ok_or_else(|| {
            ExecdError::SessionNotInitialized("spawned shell has no pid".to_string())
        })?;
        // portable-pty already setsid's the child; best-effort belt-and-braces.
        let _ = proctree::ensure_new_process_group(shell_pid);
        let reader = pair.master.try_clone_reader().map_err(|e| {
            ExecdError::SessionNotInitialized(format!("failed to open pty reader: {e}"))
        })?;
        let writer = pair.master.take_writer().map_err(|e| {
            ExecdError::SessionNotInitialized(format!("failed to open pty writer: {e}"))
        })?;
        let shared = Arc::new(ReaderShared::new(output_cap));
        let thread_shared = Arc::clone(&shared);
        std::thread::Builder::new()
            .name(format!("execd-pty-{name}"))
            .spawn(move || reader_loop(reader, &thread_shared))
            .map_err(|e| {
                ExecdError::SessionNotInitialized(format!("failed to spawn pty reader: {e}"))
            })?;
        Ok(Self {
            inner: Arc::new(SessionInner {
                name,
                output_cap,
                reader: shared,
                writer: Mutex::new(Some(writer)),
                master: Mutex::new(Some(pair.master)),
                child: Mutex::new(Some(child)),
                shell_pid,
                seq: AtomicU64::new(0),
                run_lock: tokio::sync::Mutex::new(()),
                initialized: AtomicBool::new(false),
                closed: AtomicBool::new(false),
            }),
        })
    }

    /// Prompt sync + startup sources + prompt reset (§3.2). The reset line is
    /// sent immediately (no fixed sleeps); the pty input buffer holds it
    /// while bash runs its startup files. A trailing sentinel echo proves
    /// the line EXECUTED: matching a bare prompt is not enough, since bash
    /// may print one prompt before reading our line and a second after.
    async fn init(
        &self,
        startup_source: &[String],
        startup_timeout: Duration,
    ) -> Result<String, ExecdError> {
        let mut parts: Vec<String> = startup_source
            .iter()
            .map(|p| format!("source {p}"))
            .collect();
        parts.push(format!("export PS1='{PS1_MARKER}'"));
        parts.push("export PS2=''".to_string());
        parts.push("export PS0=''".to_string());
        parts.push(format!("echo {UNIQUE_STRING}-init-done"));
        Self::write_bytes(&self.inner, format!("{}\n", parts.join(" ; ")).into_bytes()).await?;
        let map_err = |e: ExpectError| match e {
            ExpectError::Timeout => ExecdError::CommandTimeout(format!(
                "timeout after {} seconds while waiting for shell prompt during startup",
                startup_timeout.as_secs_f64()
            )),
            ExpectError::Eof | ExpectError::Closed => {
                ExecdError::SessionNotInitialized("shell exited during startup".to_string())
            }
        };
        let m = Self::expect_async(
            &self.inner.reader,
            vec![format!("{UNIQUE_STRING}-init-done")],
            Some(startup_timeout),
            true,
        )
        .await
        .map_err(&map_err)?;
        // Consume the post-execution prompt so no stale prompt leaks into
        // the first run (matches-early reads as empty output downstream).
        Self::expect_async(
            &self.inner.reader,
            vec![PS1_MARKER.to_string()],
            Some(PROMPT_SETTLE_TIMEOUT),
            true,
        )
        .await
        .map_err(&map_err)?;
        self.inner.initialized.store(true, Ordering::SeqCst);
        Ok(output::strip_control_chars(&m.before))
    }

    async fn run_checked(
        &self,
        action: &BashAction,
        check: CheckMode,
    ) -> Result<BashObservation, ExecdError> {
        let timeout = timeout_opt(action.timeout);
        let seq = self.inner.seq.fetch_add(1, Ordering::Relaxed);
        let marker_prefix = format!("{EXIT_CODE_PREFIX}{seq}:");
        let dropped_before = self.inner.reader.dropped_total();
        let payload = format!(
            "{}\necho {marker_prefix}$?{EXIT_CODE_SUFFIX}\n",
            action.command
        );
        Self::write_bytes(&self.inner, payload.into_bytes()).await?;

        let mut patterns = action.expect.clone();
        patterns.push(marker_prefix);
        let m = match Self::expect_async(&self.inner.reader, patterns, timeout, true).await {
            Ok(m) => m,
            Err(ExpectError::Timeout) => {
                self.recover_timed_out_command().await;
                return Err(ExecdError::CommandTimeout(format!(
                    "timeout after {} seconds while running command {}",
                    timeout_secs_for_msg(timeout),
                    output::py_repr(&action.command)
                )));
            }
            Err(ExpectError::Eof) | Err(ExpectError::Closed) => {
                self.mark_dead();
                return Err(ExecdError::SessionNotInitialized(
                    "shell exited while running command".to_string(),
                ));
            }
        };

        // A custom expect string may win over the marker; either way attempt
        // exit extraction like upstream (raise propagates, silent → None).
        let custom_hit = m.index < action.expect.len();
        let expect_string = if custom_hit {
            m.matched.clone()
        } else {
            PS1_MARKER.to_string()
        };
        let exit_code = match self.extract_exit_code().await {
            Ok(code) => Some(code),
            Err(e) => {
                if check == CheckMode::Raise {
                    return Err(e);
                }
                None
            }
        };

        let mut output = output::strip_control_chars(&m.before);
        output = output::drop_stale_framings(&output, seq).to_string();
        output = output.replace(UNIQUE_STRING, "").replace(PS1_MARKER, "");
        let output = self.bound_output(output, Some(dropped_before));

        if check == CheckMode::Raise && exit_code != Some(0) {
            return Err(ExecdError::NonZeroExit {
                message: nonzero_message(&action.command, &action.error_msg, exit_code, &output),
                exit_code,
            });
        }
        Ok(BashObservation {
            output,
            exit_code,
            failure_reason: String::new(),
            expect_string,
            session_type: "bash".to_string(),
        })
    }

    async fn run_ignore(&self, action: &BashAction) -> Result<BashObservation, ExecdError> {
        let timeout = timeout_opt(action.timeout);
        Self::write_bytes(&self.inner, format!("{}\n", action.command).into_bytes()).await?;
        let mut patterns = action.expect.clone();
        patterns.push(PS1_MARKER.to_string());
        let m = match Self::expect_async(&self.inner.reader, patterns, timeout, true).await {
            Ok(m) => m,
            Err(ExpectError::Timeout) => {
                self.recover_timed_out_command().await;
                return Err(ExecdError::CommandTimeout(format!(
                    "timeout after {} seconds while running command {}",
                    timeout_secs_for_msg(timeout),
                    output::py_repr(&action.command)
                )));
            }
            Err(ExpectError::Eof) | Err(ExpectError::Closed) => {
                self.mark_dead();
                return Err(ExecdError::SessionNotInitialized(
                    "shell exited while running command".to_string(),
                ));
            }
        };
        let output = output::strip_control_chars(&m.before);
        let output = self.bound_output(output, None);
        Ok(BashObservation {
            output,
            exit_code: None,
            failure_reason: String::new(),
            expect_string: m.matched,
            session_type: "bash".to_string(),
        })
    }

    /// Read the sequenced exit marker's code and re-sync to the prompt.
    async fn extract_exit_code(&self) -> Result<i32, ExecdError> {
        let m = Self::expect_async(
            &self.inner.reader,
            vec![EXIT_CODE_SUFFIX.to_string()],
            Some(EXIT_SUFFIX_TIMEOUT),
            true,
        )
        .await
        .map_err(|e| match e {
            ExpectError::Timeout => {
                ExecdError::NoExitCode("timeout while getting exit code".to_string())
            }
            ExpectError::Eof | ExpectError::Closed => {
                self.mark_dead();
                ExecdError::SessionNotInitialized(
                    "shell exited while reading exit code".to_string(),
                )
            }
        })?;
        let code: i32 = m.before.trim().parse().map_err(|_| {
            ExecdError::NoExitCode(format!(
                "failed to parse exit code from output {:?}",
                m.before
            ))
        })?;
        Self::expect_async(
            &self.inner.reader,
            vec![PS1_MARKER.to_string()],
            Some(PROMPT_SETTLE_TIMEOUT),
            true,
        )
        .await
        .map_err(|e| match e {
            ExpectError::Timeout => ExecdError::CommandTimeout(
                "Timeout while getting PS1 after exit code extraction".to_string(),
            ),
            ExpectError::Eof | ExpectError::Closed => {
                self.mark_dead();
                ExecdError::SessionNotInitialized(
                    "shell exited while reading exit code".to_string(),
                )
            }
        })?;
        Ok(code)
    }

    /// Timeout recovery: SIGKILL the foreground tree (never the shell),
    /// let the shell print its job message + the late exit marker, re-sync
    /// to the fresh prompt, drain trailing bytes, and forget stale text so
    /// the next command starts clean.
    async fn recover_timed_out_command(&self) {
        let master_fd = self
            .inner
            .master
            .lock()
            .unwrap()
            .as_ref()
            .and_then(|m| m.as_raw_fd());
        proctree::kill_foreground(
            master_fd,
            Pid::from_raw(self.inner.shell_pid as i32),
            Signal::SIGKILL,
        );
        // Grace: the kill is async; without this the resync below can match
        // a pre-kill prompt and the job message lands in the next command.
        Self::sleep_blocking(RECOVERY_GRACE).await;
        let _ = Self::expect_async(
            &self.inner.reader,
            vec![PS1_MARKER.to_string()],
            Some(PROMPT_SETTLE_TIMEOUT),
            true,
        )
        .await;
        Self::sleep_blocking(RECOVERY_DRAIN).await;
        self.inner.reader.discard_through_end();
    }

    async fn send_intr(&self) {
        let _ = Self::write_bytes(&self.inner, vec![0x03]).await;
        let master_fd = self.master_fd_snapshot();
        proctree::kill_foreground(
            master_fd,
            Pid::from_raw(self.inner.shell_pid as i32),
            Signal::SIGINT,
        );
    }

    async fn send_susp(&self) {
        let _ = Self::write_bytes(&self.inner, vec![0x1A]).await;
        let master_fd = self.master_fd_snapshot();
        proctree::kill_foreground(
            master_fd,
            Pid::from_raw(self.inner.shell_pid as i32),
            Signal::SIGTSTP,
        );
    }

    fn master_fd_snapshot(&self) -> Option<RawFd> {
        self.inner
            .master
            .lock()
            .unwrap()
            .as_ref()
            .and_then(|m| m.as_raw_fd())
    }

    /// Build the interrupt observation: peeked text plus a 0.5 s trailing
    /// drain, scrubbed and trimmed (§3.5).
    async fn finish_interrupt(&self, m: &ExpectMatch) -> BashObservation {
        let reader = Arc::clone(&self.inner.reader);
        let _ = tokio::task::spawn_blocking(move || {
            std::thread::sleep(INTERRUPT_DRAIN);
        })
        .await;
        let trailing = reader.peek_from(m.before.len() + m.matched.len());
        let combined = format!("{}{}", m.before, trailing);
        let mut output = output::strip_control_chars(&combined);
        output = output::scrub_markers(&output);
        let output = self.bound_output(output, None);
        BashObservation {
            output: output.trim().to_string(),
            exit_code: Some(0),
            failure_reason: String::new(),
            expect_string: m.matched.clone(),
            session_type: "bash".to_string(),
        }
    }

    fn ensure_usable(&self) -> Result<(), ExecdError> {
        if self.inner.closed.load(Ordering::SeqCst) {
            return Err(ExecdError::SessionNotInitialized(
                "session is closed".to_string(),
            ));
        }
        if !self.inner.initialized.load(Ordering::SeqCst) {
            return Err(ExecdError::SessionNotInitialized(
                "shell not initialized".to_string(),
            ));
        }
        Ok(())
    }

    fn mark_dead(&self) {
        self.inner.closed.store(true, Ordering::SeqCst);
        self.inner.reader.notify_close();
    }

    fn map_run_err(&self, e: ExpectError, command: &str, timeout: Option<Duration>) -> ExecdError {
        match e {
            ExpectError::Timeout => ExecdError::CommandTimeout(format!(
                "timeout after {} seconds while running command {}",
                timeout_secs_for_msg(timeout),
                output::py_repr(command)
            )),
            ExpectError::Eof | ExpectError::Closed => {
                self.mark_dead();
                ExecdError::SessionNotInitialized("shell exited while running command".to_string())
            }
        }
    }

    /// Apply the session-buffer truncation note (if `dropped_before` shows
    /// loss during this command) and the output cap.
    fn bound_output(&self, output: String, dropped_before: Option<u64>) -> String {
        let mut output = output;
        if let Some(before) = dropped_before {
            let after = self.inner.reader.dropped_total();
            if after > before {
                output = format!(
                    "[execd: output truncated ({} session-buffer bytes dropped)]\n{output}",
                    after - before
                );
            }
        }
        let (output, _) = output::truncate_with_note(&output, self.inner.output_cap);
        output
    }

    async fn write_bytes(inner: &Arc<SessionInner>, bytes: Vec<u8>) -> Result<(), ExecdError> {
        let inner = Arc::clone(inner);
        tokio::task::spawn_blocking(move || {
            let mut guard = inner.writer.lock().unwrap();
            let writer = guard.as_mut().ok_or_else(|| {
                ExecdError::SessionNotInitialized("session is closed".to_string())
            })?;
            writer.write_all(&bytes).map_err(|e| {
                ExecdError::SessionNotInitialized(format!("failed to write to shell: {e}"))
            })?;
            writer.flush().map_err(|e| {
                ExecdError::SessionNotInitialized(format!("failed to write to shell: {e}"))
            })?;
            Ok(())
        })
        .await
        .map_err(|e| ExecdError::SessionNotInitialized(format!("pty write task failed: {e}")))?
    }

    async fn sleep_blocking(d: Duration) {
        let _ = tokio::task::spawn_blocking(move || {
            std::thread::sleep(d);
        })
        .await;
    }

    async fn expect_async(
        reader: &Arc<ReaderShared>,
        patterns: Vec<String>,
        timeout: Option<Duration>,
        consume: bool,
    ) -> Result<ExpectMatch, ExpectError> {
        let reader = Arc::clone(reader);
        tokio::task::spawn_blocking(move || expect_blocking(&reader, &patterns, timeout, consume))
            .await
            .map_err(|_| ExpectError::Closed)?
    }
}

/// PTY line discipline setup: disable input echo (equivalent of pexpect
/// `echo=False`) and set NOFLSH.
///
/// NOFLSH is load-bearing for the single-submission protocol: without it,
/// an interrupt (VINTR) flushes the not-yet-read exit-marker line from the
/// PTY input buffer, so the in-flight run hangs until its own timeout.
/// Upstream's two-round-trip design is immune (the marker line is sent only
/// after the run completes); ours sends it up front and must preserve it.
fn setup_termios(master: &(dyn portable_pty::MasterPty + Send)) {
    #[cfg(unix)]
    {
        use nix::sys::termios::{tcgetattr, tcsetattr, LocalFlags, SetArg};
        use std::os::fd::BorrowedFd;
        if let Some(fd) = master.as_raw_fd() {
            if fd >= 0 {
                // SAFETY: borrowed for the syscalls; the master outlives them.
                let borrowed: BorrowedFd<'_> = unsafe { BorrowedFd::borrow_raw(fd) };
                if let Ok(mut termios) = tcgetattr(borrowed) {
                    termios.local_flags &= !LocalFlags::ECHO;
                    termios.local_flags |= LocalFlags::NOFLSH;
                    let _ = tcsetattr(borrowed, SetArg::TCSANOW, &termios);
                }
            }
        }
    }
    #[cfg(not(unix))]
    {
        let _ = master;
    }
}
