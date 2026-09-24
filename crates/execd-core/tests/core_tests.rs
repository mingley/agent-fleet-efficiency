//! P1 slice 2 core tests: session engine over a real PTY bash.

use execd_core::{ExecdError, SessionManager};
use execd_protocol::{BashAction, BashInterruptAction, CreateBashSessionRequest};

fn create_req(session: &str) -> CreateBashSessionRequest {
    CreateBashSessionRequest {
        startup_source: vec![],
        session: session.to_string(),
        session_type: "bash".to_string(),
        startup_timeout: 10.0,
    }
}

fn action(session: &str, command: &str) -> BashAction {
    BashAction {
        command: command.to_string(),
        session: session.to_string(),
        timeout: Some(15.0),
        is_interactive_command: false,
        is_interactive_quit: false,
        check: "raise".to_string(),
        error_msg: String::new(),
        expect: vec![],
        action_type: "bash".to_string(),
    }
}

fn class_path(err: &ExecdError) -> &'static str {
    err.class_path()
}

/// Content lines: trimmed, blank-line framing dropped.
///
/// By design the single-submission protocol runs fewer prompt cycles than
/// upstream's extra exit-status round trip, so the COUNT of blank framing
/// lines differs (both sides emit the same `\r\n` atoms -- lone `\r` left
/// by `\r\r\n` ONLCR triples after `\r\n`->`\n` normalization, exactly as
/// upstream). Content lines and exit codes are the compatibility contract;
/// blank-line counts are framing noise. See docs/p1-compat.md §3.3.
fn content_lines(output: &str) -> Vec<String> {
    output
        .lines()
        .map(str::trim)
        .filter(|l| !l.is_empty())
        .map(str::to_string)
        .collect()
}

/// Scrubbing pin: ANSI escapes and `\r\r` triples must never leak through.
fn assert_scrubbed(output: &str) {
    assert!(!output.contains('\x1b'), "ANSI leaked: {output:?}");
    assert!(
        !output.contains("\r\r"),
        "unstripped CRLF triple: {output:?}"
    );
}

#[tokio::test]
async fn create_and_run_true() {
    let mgr = SessionManager::new();
    let resp = mgr.create_session(&create_req("default")).await.unwrap();
    assert_eq!(resp.session_type, "bash");
    let obs = mgr.run_bash(&action("default", "true")).await.unwrap();
    assert_eq!(obs.exit_code, Some(0));
    assert_scrubbed(&obs.output);
    assert!(content_lines(&obs.output).is_empty());
    assert_eq!(obs.session_type, "bash");
}

#[tokio::test]
async fn echo_exit_code() {
    let mgr = SessionManager::new();
    mgr.create_session(&create_req("default")).await.unwrap();
    let obs = mgr
        .run_bash(&action("default", "echo hello"))
        .await
        .unwrap();
    assert_eq!(obs.exit_code, Some(0));
    assert_scrubbed(&obs.output);
    assert_eq!(content_lines(&obs.output), ["hello"]);
}

#[tokio::test]
async fn nonzero_check_modes() {
    let mgr = SessionManager::new();
    mgr.create_session(&create_req("default")).await.unwrap();

    // raise (default)
    let err = mgr.run_bash(&action("default", "false")).await.unwrap_err();
    assert_eq!(
        class_path(&err),
        "swerex.exceptions.NonZeroExitCodeError",
        "unexpected: {err}"
    );
    assert!(
        err.message().contains("exit code 1"),
        "msg: {}",
        err.message()
    );

    // raise with error_msg prefix
    let mut a = action("default", "false");
    a.error_msg = "boom".to_string();
    let err = mgr.run_bash(&a).await.unwrap_err();
    assert!(
        err.message().starts_with("boom: "),
        "msg: {}",
        err.message()
    );

    // silent
    let mut a = action("default", "false");
    a.check = "silent".to_string();
    let obs = mgr.run_bash(&a).await.unwrap();
    assert_eq!(obs.exit_code, Some(1));

    // ignore
    let mut a = action("default", "false");
    a.check = "ignore".to_string();
    let obs = mgr.run_bash(&a).await.unwrap();
    assert_eq!(obs.exit_code, None);

    // unknown check mode
    let mut a = action("default", "true");
    a.check = "bogus".to_string();
    let err = mgr.run_bash(&a).await.unwrap_err();
    assert_eq!(class_path(&err), "builtins.ValueError");
}

#[tokio::test]
async fn timeout_fires_and_session_recovers() {
    let mgr = SessionManager::new();
    mgr.create_session(&create_req("default")).await.unwrap();
    let mut a = action("default", "sleep 30");
    a.timeout = Some(0.3);
    let err = mgr.run_bash(&a).await.unwrap_err();
    assert_eq!(
        class_path(&err),
        "swerex.exceptions.CommandTimeoutError",
        "unexpected: {err}"
    );
    assert!(
        err.message().contains("timeout after"),
        "msg: {}",
        err.message()
    );
    // Session still usable: the timed-out tree was reaped.
    let obs = mgr
        .run_bash(&action("default", "echo recovered"))
        .await
        .unwrap();
    assert_eq!(obs.exit_code, Some(0));
    assert_eq!(content_lines(&obs.output), ["recovered"]);
}

#[tokio::test]
async fn interrupt_kills_sleep() {
    let mgr = SessionManager::new();
    mgr.create_session(&create_req("default")).await.unwrap();
    let session = mgr.get("default").await.unwrap();

    let mut a = action("default", "sleep 30");
    a.timeout = Some(30.0);
    a.check = "silent".to_string();
    let run_handle = tokio::spawn({
        let session = session.clone();
        async move { session.run(&a).await }
    });
    tokio::time::sleep(std::time::Duration::from_secs(1)).await;

    let intr = BashInterruptAction {
        session: "default".to_string(),
        timeout: 5.0,
        n_retry: 10,
        expect: vec![],
        action_type: "bash_interrupt".to_string(),
    };
    let obs = session.interrupt(&intr).await.unwrap();
    assert_eq!(obs.exit_code, Some(0));

    let run_outcome = tokio::time::timeout(std::time::Duration::from_secs(15), run_handle)
        .await
        .expect("run did not finish after interrupt")
        .unwrap();
    let obs = run_outcome.unwrap();
    assert!(
        obs.exit_code.is_some_and(|c| c != 0),
        "expected nonzero exit, got {:?}",
        obs.exit_code
    );

    // Session still usable afterwards.
    let obs = mgr
        .run_bash(&action("default", "echo alive"))
        .await
        .unwrap();
    assert_eq!(content_lines(&obs.output), ["alive"]);
}

#[tokio::test]
async fn env_and_cwd_persist() {
    let mgr = SessionManager::new();
    mgr.create_session(&create_req("default")).await.unwrap();
    mgr.run_bash(&action("default", "export FLEET_QA_MARKER=42"))
        .await
        .unwrap();
    let obs = mgr
        .run_bash(&action("default", "echo $FLEET_QA_MARKER"))
        .await
        .unwrap();
    assert_eq!(content_lines(&obs.output), ["42"]);

    mgr.run_bash(&action("default", "cd /tmp")).await.unwrap();
    let obs = mgr.run_bash(&action("default", "pwd")).await.unwrap();
    assert!(obs.output.trim() == "/tmp" || obs.output.trim() == "/private/tmp");
}

#[tokio::test]
async fn close_unknown_and_duplicate_errors() {
    let mgr = SessionManager::new();
    // Unknown session on get/run/close.
    assert_eq!(
        class_path(&mgr.get("nope").await.unwrap_err()),
        "swerex.exceptions.SessionDoesNotExistError"
    );
    assert_eq!(
        class_path(&mgr.run_bash(&action("nope", "true")).await.unwrap_err()),
        "swerex.exceptions.SessionDoesNotExistError"
    );
    let err = mgr
        .close_session(&execd_protocol::CloseBashSessionRequest {
            session: "nope".to_string(),
            session_type: "bash".to_string(),
        })
        .await
        .unwrap_err();
    assert_eq!(
        class_path(&err),
        "swerex.exceptions.SessionDoesNotExistError"
    );

    // Duplicate create.
    mgr.create_session(&create_req("dup")).await.unwrap();
    let err = mgr.create_session(&create_req("dup")).await.unwrap_err();
    assert_eq!(class_path(&err), "swerex.exceptions.SessionExistsError");

    // Unknown session type.
    let mut req = create_req("other");
    req.session_type = "zsh".to_string();
    let err = mgr.create_session(&req).await.unwrap_err();
    assert_eq!(class_path(&err), "builtins.ValueError");
}

#[tokio::test]
async fn interactive_command_returns_zero() {
    let mgr = SessionManager::new();
    mgr.create_session(&create_req("default")).await.unwrap();
    let mut a = action("default", "echo inter");
    a.is_interactive_command = true;
    let obs = mgr.run_bash(&a).await.unwrap();
    assert_eq!(obs.exit_code, Some(0));
    assert!(obs.output.contains("inter"), "output: {:?}", obs.output);

    // Conflicting interactive flags.
    let mut a = action("default", "true");
    a.is_interactive_command = true;
    a.is_interactive_quit = true;
    let err = mgr.run_bash(&a).await.unwrap_err();
    assert_eq!(class_path(&err), "builtins.ValueError");
}

#[tokio::test]
async fn protocol_json_round_trip_through_core() {
    let mgr = SessionManager::new();
    mgr.create_session(&create_req("default")).await.unwrap();
    let action: BashAction =
        serde_json::from_value(serde_json::json!({"command": "echo json", "timeout": 10.0}))
            .unwrap();
    let obs = mgr.run_bash(&action).await.unwrap();
    let v = serde_json::to_value(&obs).unwrap();
    assert_eq!(content_lines(v["output"].as_str().unwrap()), ["json"]);
    assert_eq!(v["exit_code"], serde_json::json!(0));
    assert_eq!(v["session_type"], serde_json::json!("bash"));
}
