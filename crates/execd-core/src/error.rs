//! Error type with a 1:1 mapping to compat §3.9 `class_path` values.

use std::fmt;

/// Session-engine errors. Each variant maps to exactly one §3.9 `class_path`.
#[derive(Debug)]
pub enum ExecdError {
    /// `builtins.ValueError` (unknown session type, conflicting flags, ...).
    ValueError(String),
    /// `swerex.exceptions.SessionExistsError`.
    SessionExists(String),
    /// `swerex.exceptions.SessionDoesNotExistError`.
    SessionDoesNotExist(String),
    /// `swerex.exceptions.NonZeroExitCodeError`.
    NonZeroExit {
        message: String,
        exit_code: Option<i32>,
    },
    /// `swerex.exceptions.CommandTimeoutError` (run/interrupt/startup timeouts,
    /// failed interrupt fallback).
    CommandTimeout(String),
    /// `swerex.exceptions.NoExitCodeError`.
    NoExitCode(String),
    /// `swerex.exceptions.SessionNotInitializedError` (closed/dead shell).
    SessionNotInitialized(String),
}

impl ExecdError {
    /// The §3.9 `class_path` for this error.
    pub fn class_path(&self) -> &'static str {
        match self {
            ExecdError::ValueError(_) => "builtins.ValueError",
            ExecdError::SessionExists(_) => "swerex.exceptions.SessionExistsError",
            ExecdError::SessionDoesNotExist(_) => "swerex.exceptions.SessionDoesNotExistError",
            ExecdError::NonZeroExit { .. } => "swerex.exceptions.NonZeroExitCodeError",
            ExecdError::CommandTimeout(_) => "swerex.exceptions.CommandTimeoutError",
            ExecdError::NoExitCode(_) => "swerex.exceptions.NoExitCodeError",
            ExecdError::SessionNotInitialized(_) => "swerex.exceptions.SessionNotInitializedError",
        }
    }

    /// The human-readable message (becomes `ExceptionTransfer.message`).
    pub fn message(&self) -> &str {
        match self {
            ExecdError::ValueError(m)
            | ExecdError::SessionExists(m)
            | ExecdError::SessionDoesNotExist(m)
            | ExecdError::NonZeroExit { message: m, .. }
            | ExecdError::CommandTimeout(m)
            | ExecdError::NoExitCode(m)
            | ExecdError::SessionNotInitialized(m) => m,
        }
    }

    /// Convert to the §3.9 wire transfer model.
    pub fn to_transfer(&self) -> execd_protocol::ExceptionTransfer {
        execd_protocol::ExceptionTransfer {
            message: self.message().to_string(),
            class_path: self.class_path().to_string(),
            traceback: String::new(),
            extra_info: serde_json::Map::new(),
        }
    }
}

impl fmt::Display for ExecdError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(f, "{}: {}", self.class_path(), self.message())
    }
}

impl std::error::Error for ExecdError {}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn class_paths_match_compat_3_9() {
        let cases = [
            (ExecdError::ValueError(String::new()), "builtins.ValueError"),
            (
                ExecdError::SessionExists(String::new()),
                "swerex.exceptions.SessionExistsError",
            ),
            (
                ExecdError::SessionDoesNotExist(String::new()),
                "swerex.exceptions.SessionDoesNotExistError",
            ),
            (
                ExecdError::NonZeroExit {
                    message: String::new(),
                    exit_code: None,
                },
                "swerex.exceptions.NonZeroExitCodeError",
            ),
            (
                ExecdError::CommandTimeout(String::new()),
                "swerex.exceptions.CommandTimeoutError",
            ),
            (
                ExecdError::NoExitCode(String::new()),
                "swerex.exceptions.NoExitCodeError",
            ),
            (
                ExecdError::SessionNotInitialized(String::new()),
                "swerex.exceptions.SessionNotInitializedError",
            ),
        ];
        for (err, want) in cases {
            assert_eq!(err.class_path(), want);
            let t = err.to_transfer();
            assert_eq!(t.class_path, want);
            assert!(t.extra_info.is_empty());
        }
    }
}
