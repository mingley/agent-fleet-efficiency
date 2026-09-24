//! Wire models for agent-execd, mirroring the SWE-ReX JSON surface exactly.
//! See docs/p1-compat.md §§3.1, 3.2, 3.6, 3.9.

use serde::{Deserialize, Serialize};

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
