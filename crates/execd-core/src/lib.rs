//! Session engine for agent-execd: persistent bash sessions on a PTY.
//!
//! See `docs/p1-compat.md` §§3.2–3.6 (prompt protocol, run pipeline,
//! interactive, interrupt, close) and `docs/architecture.md` constraints
//! (persistent sessions, process-tree cancellation, bounded backpressure).

pub mod error;
pub mod manager;
pub mod output;
pub mod proctree;
pub mod session;

pub use error::ExecdError;
pub use manager::SessionManager;
pub use session::BashSession;

/// PS1 prompt marker. Matches upstream `BashSession._ps1`.
pub const PS1_MARKER: &str = "SHELLPS1PREFIX";
/// Sentinel used by the interactive-quit handshake. Matches upstream.
pub const UNIQUE_STRING: &str = "UNIQUESTRING29234";
/// Exit-code framing markers for single-submission runs (§3.3):
/// `EXITCODESTART<seq>:<code>EXITCODEEND`.
pub const EXIT_CODE_PREFIX: &str = "EXITCODESTART";
pub const EXIT_CODE_SUFFIX: &str = "EXITCODEEND";
/// Default per-observation output cap (1 MiB). Over-cap output is truncated
/// (tail kept) with a note.
pub const DEFAULT_OUTPUT_CAP: usize = 1024 * 1024;

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn constants_match_upstream() {
        assert_eq!(PS1_MARKER, "SHELLPS1PREFIX");
        assert_eq!(UNIQUE_STRING, "UNIQUESTRING29234");
        assert_eq!(EXIT_CODE_PREFIX, "EXITCODESTART");
        assert_eq!(EXIT_CODE_SUFFIX, "EXITCODEEND");
        assert_eq!(DEFAULT_OUTPUT_CAP, 1024 * 1024);
    }
}
