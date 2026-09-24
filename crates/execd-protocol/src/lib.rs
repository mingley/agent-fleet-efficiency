//! Wire models for agent-execd, mirroring the SWE-ReX JSON surface exactly.
//! See docs/p1-compat.md §§3.1–3.9.

use serde::{Deserialize, Serialize};
use std::collections::HashMap;

fn default_session() -> String {
    "default".to_string()
}

fn default_session_type() -> String {
    "bash".to_string()
}

fn default_startup_timeout() -> f64 {
    1.0
}

/// §3.1: `is_alive: bool` (required), `message: str = ""`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct IsAliveResponse {
    pub is_alive: bool,
    #[serde(default)]
    pub message: String,
}

/// §3.2 request: `startup_source: list[str] = []`, `session = "default"`,
/// `session_type = "bash"`, `startup_timeout = 1.0`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CreateBashSessionRequest {
    #[serde(default)]
    pub startup_source: Vec<String>,
    #[serde(default = "default_session")]
    pub session: String,
    #[serde(default = "default_session_type")]
    pub session_type: String,
    #[serde(default = "default_startup_timeout")]
    pub startup_timeout: f64,
}

/// §3.2 response: `output: str = ""`, `session_type = "bash"`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CreateBashSessionResponse {
    #[serde(default)]
    pub output: String,
    #[serde(default = "default_session_type")]
    pub session_type: String,
}

/// §3.6 request: `session = "default"`, `session_type = "bash"`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CloseBashSessionRequest {
    #[serde(default = "default_session")]
    pub session: String,
    #[serde(default = "default_session_type")]
    pub session_type: String,
}

/// §3.6 response: `session_type = "bash"` only.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CloseBashSessionResponse {
    #[serde(default = "default_session_type")]
    pub session_type: String,
}

/// §3.6: empty model, serializes to `{}`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CloseResponse {}

/// §3.9 `_ExceptionTransfer`: `message`, `class_path`, `traceback`,
/// `extra_info: dict = {}`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ExceptionTransfer {
    #[serde(default)]
    pub message: String,
    #[serde(default)]
    pub class_path: String,
    #[serde(default)]
    pub traceback: String,
    #[serde(default)]
    pub extra_info: serde_json::Map<String, serde_json::Value>,
}

/// §3.9 wire envelope: server returns 511 + `{"swerexception": dump}`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct SwerexceptionEnvelope {
    pub swerexception: ExceptionTransfer,
}

fn default_action_type_bash() -> String {
    "bash".to_string()
}

fn default_action_type_bash_interrupt() -> String {
    "bash_interrupt".to_string()
}

fn default_check() -> String {
    "raise".to_string()
}

fn default_interrupt_timeout() -> f64 {
    0.2
}

fn default_n_retry() -> i32 {
    3
}

/// §3.3 `BashAction`: `command` (required), `session = "default"`,
/// `timeout: float | None = None`, `is_interactive_command = False`,
/// `is_interactive_quit = False`, `check = "raise"`, `error_msg = ""`,
/// `expect = []`, `action_type = "bash"`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BashAction {
    pub command: String,
    #[serde(default = "default_session")]
    pub session: String,
    #[serde(default)]
    pub timeout: Option<f64>,
    #[serde(default)]
    pub is_interactive_command: bool,
    #[serde(default)]
    pub is_interactive_quit: bool,
    #[serde(default = "default_check")]
    pub check: String,
    #[serde(default)]
    pub error_msg: String,
    #[serde(default)]
    pub expect: Vec<String>,
    #[serde(default = "default_action_type_bash")]
    pub action_type: String,
}

/// §3.5 `BashInterruptAction`: `session = "default"`, `timeout = 0.2`,
/// `n_retry = 3`, `expect = []`, `action_type = "bash_interrupt"`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BashInterruptAction {
    #[serde(default = "default_session")]
    pub session: String,
    #[serde(default = "default_interrupt_timeout")]
    pub timeout: f64,
    #[serde(default = "default_n_retry")]
    pub n_retry: i32,
    #[serde(default)]
    pub expect: Vec<String>,
    #[serde(default = "default_action_type_bash_interrupt")]
    pub action_type: String,
}

/// §3.3 `BashObservation`: `output = ""`, `exit_code: int | None = None`,
/// `failure_reason = ""`, `expect_string = ""`, `session_type = "bash"`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct BashObservation {
    #[serde(default)]
    pub output: String,
    #[serde(default)]
    pub exit_code: Option<i32>,
    #[serde(default)]
    pub failure_reason: String,
    #[serde(default)]
    pub expect_string: String,
    #[serde(default = "default_session_type")]
    pub session_type: String,
}

/// §3.7 `Command.command`: `str | list[str]` (untagged union, like pydantic).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(untagged)]
pub enum CommandArg {
    Text(String),
    Argv(Vec<String>),
}

/// §3.7 `Command`: `command` (required), `timeout = None`, `shell = False`,
/// `check = False`, `error_msg = ""`, `env = None`, `cwd = None`,
/// `merge_output_streams = False`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Command {
    pub command: CommandArg,
    #[serde(default)]
    pub timeout: Option<f64>,
    #[serde(default)]
    pub shell: bool,
    #[serde(default)]
    pub check: bool,
    #[serde(default)]
    pub error_msg: String,
    #[serde(default)]
    pub env: Option<HashMap<String, String>>,
    #[serde(default)]
    pub cwd: Option<String>,
    #[serde(default)]
    pub merge_output_streams: bool,
}

/// §3.7 `CommandResponse`: `stdout = ""`, `stderr = ""`,
/// `exit_code: int | None = None`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct CommandResponse {
    #[serde(default)]
    pub stdout: String,
    #[serde(default)]
    pub stderr: String,
    #[serde(default)]
    pub exit_code: Option<i32>,
}

/// §3.8 request: `path` (required), `encoding = None`, `errors = None`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ReadFileRequest {
    pub path: String,
    #[serde(default)]
    pub encoding: Option<String>,
    #[serde(default)]
    pub errors: Option<String>,
}

/// §3.8 response: `content = ""`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ReadFileResponse {
    #[serde(default)]
    pub content: String,
}

/// §3.8 request: `content`, `path` (both required).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct WriteFileRequest {
    pub content: String,
    pub path: String,
}

/// §3.8 response: empty model, serializes to `{}`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct WriteFileResponse {}

/// §3.8 response: empty model, serializes to `{}`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct UploadResponse {}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn is_alive_round_trip() {
        let v: IsAliveResponse = serde_json::from_value(json!({"is_alive": true})).unwrap();
        assert_eq!(
            v,
            IsAliveResponse {
                is_alive: true,
                message: String::new()
            }
        );
        assert_eq!(
            serde_json::to_value(&v).unwrap(),
            json!({"is_alive": true, "message": ""})
        );
    }

    #[test]
    fn create_session_request_defaults() {
        let v: CreateBashSessionRequest = serde_json::from_value(json!({})).unwrap();
        assert_eq!(v.startup_source, Vec::<String>::new());
        assert_eq!(v.session, "default");
        assert_eq!(v.session_type, "bash");
        assert_eq!(v.startup_timeout, 1.0);
        assert_eq!(
            serde_json::to_value(&v).unwrap(),
            json!({
                "startup_source": [],
                "session": "default",
                "session_type": "bash",
                "startup_timeout": 1.0,
            })
        );
    }

    #[test]
    fn create_session_response_shape() {
        let v = CreateBashSessionResponse {
            output: String::new(),
            session_type: "bash".to_string(),
        };
        assert_eq!(
            serde_json::to_value(&v).unwrap(),
            json!({"output": "", "session_type": "bash"})
        );
        let back: CreateBashSessionResponse =
            serde_json::from_value(json!({"output": "", "session_type": "bash"})).unwrap();
        assert_eq!(back, v);
    }

    #[test]
    fn close_models_shape() {
        let req: CloseBashSessionRequest = serde_json::from_value(json!({})).unwrap();
        assert_eq!(req.session, "default");
        assert_eq!(req.session_type, "bash");
        let resp = CloseBashSessionResponse {
            session_type: "bash".to_string(),
        };
        assert_eq!(
            serde_json::to_value(&resp).unwrap(),
            json!({"session_type": "bash"})
        );
        assert_eq!(serde_json::to_value(&CloseResponse {}).unwrap(), json!({}));
        let _: CloseResponse = serde_json::from_value(json!({})).unwrap();
    }

    #[test]
    fn bash_action_defaults_and_round_trip() {
        let v: BashAction = serde_json::from_value(json!({"command": "echo hi"})).unwrap();
        assert_eq!(v.command, "echo hi");
        assert_eq!(v.session, "default");
        assert_eq!(v.timeout, None);
        assert!(!v.is_interactive_command);
        assert!(!v.is_interactive_quit);
        assert_eq!(v.check, "raise");
        assert_eq!(v.error_msg, "");
        assert!(v.expect.is_empty());
        assert_eq!(v.action_type, "bash");
        assert_eq!(
            serde_json::to_value(&v).unwrap(),
            json!({
                "command": "echo hi",
                "session": "default",
                "timeout": null,
                "is_interactive_command": false,
                "is_interactive_quit": false,
                "check": "raise",
                "error_msg": "",
                "expect": [],
                "action_type": "bash",
            })
        );
        // Explicit timeout survives the round trip.
        let v: BashAction =
            serde_json::from_value(json!({"command": "x", "timeout": 2.5})).unwrap();
        assert_eq!(v.timeout, Some(2.5));
        // command is required.
        assert!(serde_json::from_value::<BashAction>(json!({})).is_err());
    }

    #[test]
    fn bash_interrupt_action_defaults_and_round_trip() {
        let v: BashInterruptAction = serde_json::from_value(json!({})).unwrap();
        assert_eq!(v.session, "default");
        assert_eq!(v.timeout, 0.2);
        assert_eq!(v.n_retry, 3);
        assert!(v.expect.is_empty());
        assert_eq!(v.action_type, "bash_interrupt");
        let back: BashInterruptAction =
            serde_json::from_value(serde_json::to_value(&v).unwrap()).unwrap();
        assert_eq!(back, v);
    }

    #[test]
    fn bash_observation_defaults_and_round_trip() {
        let v: BashObservation = serde_json::from_value(json!({})).unwrap();
        assert_eq!(v.output, "");
        assert_eq!(v.exit_code, None);
        assert_eq!(v.failure_reason, "");
        assert_eq!(v.expect_string, "");
        assert_eq!(v.session_type, "bash");
        let full = BashObservation {
            output: "hi\n".to_string(),
            exit_code: Some(3),
            failure_reason: String::new(),
            expect_string: "SHELLPS1PREFIX".to_string(),
            session_type: "bash".to_string(),
        };
        assert_eq!(
            serde_json::to_value(&full).unwrap(),
            json!({
                "output": "hi\n",
                "exit_code": 3,
                "failure_reason": "",
                "expect_string": "SHELLPS1PREFIX",
                "session_type": "bash",
            })
        );
        let back: BashObservation =
            serde_json::from_value(json!({"output": "x", "exit_code": null})).unwrap();
        assert_eq!(back.exit_code, None);
    }

    #[test]
    fn command_str_and_list_round_trip() {
        // String form (shell-style).
        let v: Command =
            serde_json::from_value(json!({"command": "echo hi", "shell": true})).unwrap();
        assert_eq!(v.command, CommandArg::Text("echo hi".to_string()));
        assert!(v.shell);
        assert_eq!(v.timeout, None);
        assert!(!v.check);
        assert_eq!(v.error_msg, "");
        assert_eq!(v.env, None);
        assert_eq!(v.cwd, None);
        assert!(!v.merge_output_streams);
        // List form (argv-style).
        let v: Command = serde_json::from_value(json!({"command": ["echo", "hi"]})).unwrap();
        assert_eq!(
            v.command,
            CommandArg::Argv(vec!["echo".to_string(), "hi".to_string()])
        );
        assert!(!v.shell);
        // Full round trip with all fields.
        let full = Command {
            command: CommandArg::Argv(vec!["ls".to_string()]),
            timeout: Some(5.0),
            shell: false,
            check: true,
            error_msg: "ls failed".to_string(),
            env: Some([("A".to_string(), "b".to_string())].into_iter().collect()),
            cwd: Some("/tmp".to_string()),
            merge_output_streams: true,
        };
        let back: Command = serde_json::from_value(serde_json::to_value(&full).unwrap()).unwrap();
        assert_eq!(back, full);
        // command is required.
        assert!(serde_json::from_value::<Command>(json!({})).is_err());
        // Response shape.
        let r = CommandResponse {
            stdout: "o".to_string(),
            stderr: "e".to_string(),
            exit_code: Some(0),
        };
        assert_eq!(
            serde_json::to_value(&r).unwrap(),
            json!({"stdout": "o", "stderr": "e", "exit_code": 0})
        );
        let d: CommandResponse = serde_json::from_value(json!({})).unwrap();
        assert_eq!(d.exit_code, None);
    }

    #[test]
    fn file_models_round_trip() {
        let r: ReadFileRequest = serde_json::from_value(json!({"path": "/tmp/x"})).unwrap();
        assert_eq!(r.path, "/tmp/x");
        assert_eq!(r.encoding, None);
        assert_eq!(r.errors, None);
        assert!(serde_json::from_value::<ReadFileRequest>(json!({})).is_err());
        let back: ReadFileRequest =
            serde_json::from_value(serde_json::to_value(&r).unwrap()).unwrap();
        assert_eq!(back, r);
        let resp = ReadFileResponse {
            content: "hi".to_string(),
        };
        assert_eq!(
            serde_json::to_value(&resp).unwrap(),
            json!({"content": "hi"})
        );
        let w: WriteFileRequest =
            serde_json::from_value(json!({"content": "c", "path": "p"})).unwrap();
        assert_eq!(w.content, "c");
        assert_eq!(w.path, "p");
        assert!(serde_json::from_value::<WriteFileRequest>(json!({"path": "p"})).is_err());
        assert_eq!(
            serde_json::to_value(&WriteFileResponse {}).unwrap(),
            json!({})
        );
        let _: WriteFileResponse = serde_json::from_value(json!({})).unwrap();
        assert_eq!(serde_json::to_value(&UploadResponse {}).unwrap(), json!({}));
        let _: UploadResponse = serde_json::from_value(json!({})).unwrap();
    }

    #[test]
    fn exception_transfer_shape() {
        let v: ExceptionTransfer = serde_json::from_value(json!({})).unwrap();
        assert_eq!(v.message, "");
        assert_eq!(v.class_path, "");
        assert_eq!(v.traceback, "");
        assert!(v.extra_info.is_empty());
        let full = ExceptionTransfer {
            message: "boom".to_string(),
            class_path: "swerex.exceptions.SwerexException".to_string(),
            traceback: "tb".to_string(),
            extra_info: serde_json::Map::new(),
        };
        assert_eq!(
            serde_json::to_value(&SwerexceptionEnvelope {
                swerexception: full
            })
            .unwrap(),
            json!({"swerexception": {
                "message": "boom",
                "class_path": "swerex.exceptions.SwerexException",
                "traceback": "tb",
                "extra_info": {},
            }})
        );
    }
}
