//! agent-execd stub server: 3 routes, auth middleware, 511 error mapper.

use axum::{
    extract::State,
    http::{Request, StatusCode},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use clap::Parser;
use execd_protocol::{
    CloseResponse, CreateBashSessionRequest, CreateBashSessionResponse, ExceptionTransfer,
    IsAliveResponse, SwerexceptionEnvelope,
};
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

async fn is_alive() -> Json<IsAliveResponse> {
    Json(IsAliveResponse {
        is_alive: true,
        message: String::new(),
    })
}

async fn create_session(Json(req): Json<CreateBashSessionRequest>) -> Response {
    if req.session_type != "bash" {
        return swerexception_511(
            &format!("unknown session type: {:?}", req.session_type),
            "builtins.ValueError",
        );
    }
    (
        StatusCode::OK,
        Json(CreateBashSessionResponse {
            output: String::new(),
            session_type: "bash".to_string(),
        }),
    )
        .into_response()
}

async fn close() -> Json<CloseResponse> {
    Json(CloseResponse {})
}

#[tokio::main]
async fn main() {
    let args = Args::parse();
    let state = AppState {
        auth_token: Arc::new(args.auth_token),
    };
    let app = Router::new()
        .route("/is_alive", get(is_alive))
        .route("/create_session", post(create_session))
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
