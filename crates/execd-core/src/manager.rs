//! Named-session registry (skeleton; wired to the PTY engine next).

use crate::{BashSession, ExecdError, DEFAULT_OUTPUT_CAP};
use std::collections::HashMap;
use std::sync::Arc;

/// Registry of named persistent sessions.
#[derive(Debug, Default)]
pub struct SessionManager {
    sessions: tokio::sync::RwLock<HashMap<String, Arc<BashSession>>>,
    output_cap: usize,
}

impl SessionManager {
    /// New manager with the default 1 MiB output cap.
    pub fn new() -> Self {
        Self::with_output_cap(DEFAULT_OUTPUT_CAP)
    }

    /// New manager with an explicit output cap.
    pub fn with_output_cap(output_cap: usize) -> Self {
        Self {
            sessions: tokio::sync::RwLock::new(HashMap::new()),
            output_cap,
        }
    }

    /// Session names currently registered.
    pub async fn session_names(&self) -> Vec<String> {
        self.sessions.read().await.keys().cloned().collect()
    }

    /// Create and start a session (§3.2). Duplicate → `SessionExists`;
    /// non-bash type → `ValueError`.
    pub async fn create_session(
        &self,
        req: &execd_protocol::CreateBashSessionRequest,
    ) -> Result<execd_protocol::CreateBashSessionResponse, ExecdError> {
        if req.session_type != "bash" {
            return Err(ExecdError::ValueError(format!(
                "unknown session type: {:?}",
                req.session_type
            )));
        }
        if self.sessions.read().await.contains_key(&req.session) {
            return Err(ExecdError::SessionExists(format!(
                "session {} already exists",
                req.session
            )));
        }
        let startup_timeout = std::time::Duration::from_secs_f64(req.startup_timeout.max(0.0));
        let (session, output) = BashSession::start(
            req.session.clone(),
            &req.startup_source,
            startup_timeout,
            self.output_cap,
        )
        .await?;
        let mut guard = self.sessions.write().await;
        if guard.contains_key(&req.session) {
            drop(guard);
            session.close().await;
            return Err(ExecdError::SessionExists(format!(
                "session {} already exists",
                req.session
            )));
        }
        guard.insert(req.session.clone(), Arc::new(session));
        Ok(execd_protocol::CreateBashSessionResponse {
            output,
            session_type: "bash".to_string(),
        })
    }

    /// Run a `BashAction` in its session (§§3.3–3.4).
    pub async fn run_bash(
        &self,
        action: &execd_protocol::BashAction,
    ) -> Result<execd_protocol::BashObservation, ExecdError> {
        self.get(&action.session).await?.run(action).await
    }

    /// Interrupt a session (§3.5).
    pub async fn interrupt(
        &self,
        action: &execd_protocol::BashInterruptAction,
    ) -> Result<execd_protocol::BashObservation, ExecdError> {
        self.get(&action.session).await?.interrupt(action).await
    }

    /// Look up a session. Unknown → `SessionDoesNotExist`.
    pub async fn get(&self, session: &str) -> Result<Arc<BashSession>, ExecdError> {
        self.sessions
            .read()
            .await
            .get(session)
            .cloned()
            .ok_or_else(|| {
                ExecdError::SessionDoesNotExist(format!("session {session:?} does not exist"))
            })
    }

    /// Close and remove a session. Unknown → `SessionDoesNotExist`.
    pub async fn close_session(
        &self,
        req: &execd_protocol::CloseBashSessionRequest,
    ) -> Result<execd_protocol::CloseBashSessionResponse, ExecdError> {
        let session = self.sessions.write().await.remove(&req.session);
        match session {
            Some(s) => {
                s.close().await;
                Ok(execd_protocol::CloseBashSessionResponse {
                    session_type: "bash".to_string(),
                })
            }
            None => Err(ExecdError::SessionDoesNotExist(format!(
                "session {:?} does not exist",
                req.session
            ))),
        }
    }

    /// Close every session.
    pub async fn close_all(&self) {
        let sessions: Vec<_> = {
            let mut guard = self.sessions.write().await;
            guard.drain().map(|(_, s)| s).collect()
        };
        for s in sessions {
            s.close().await;
        }
    }
}
