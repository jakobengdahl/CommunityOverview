"""Auth and CORS middleware wiring for the api_host application."""

import base64
import logging
import secrets

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.requests import Request

from .config import AppConfig
from .diagnostics import PUBLIC_READINESS_PATH, PUBLIC_STARTUP_DIAGNOSTICS_PATH

logger = logging.getLogger(__name__)


def compute_auth_active(config: AppConfig) -> bool:
    """Canonical condition for whether the auth middleware guards any endpoint.

    True when a password (Basic) or bearer token is configured together with an
    activation flag (auth_enabled or mcp_basic_auth). Used both to install the
    middleware and to gate unauthenticated tool execution.
    """
    return bool(
        (config.auth_password and (config.auth_enabled or config.mcp_basic_auth))
        or (config.auth_bearer_token and (config.auth_enabled or config.mcp_basic_auth))
    )


def add_auth_middleware(app: FastAPI, config: AppConfig) -> None:
    """Install the HTTP auth middleware when auth is active.

    Supports two schemes on all guarded endpoints:
      - Basic <base64(user:pass)>  — for browsers and MCP clients that support Basic
      - Bearer <token>             — for MCP clients / API consumers (AUTH_BEARER_TOKEN)

    Two activation modes:
      1. auth_enabled=True: auth on ALL endpoints (except /health, /info, etc.)
      2. mcp_basic_auth=True: auth ONLY on /mcp and /execute_tool endpoints
    """
    if not compute_auth_active(config):
        return

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        if request.method == "OPTIONS":
            return await call_next(request)

        # Public routes — always bypass auth
        if request.url.path in [
            "/health",
            PUBLIC_READINESS_PATH,
            "/info",
            PUBLIC_STARTUP_DIAGNOSTICS_PATH,
            "/auth/logout",
            "/logged-out",
        ]:
            return await call_next(request)

        # Visualization session endpoints are secured by the session ID itself
        # (CSPRNG, 100M-combination address space).  The browser's EventSource
        # cannot send Authorization headers, so these routes must bypass auth.
        if request.url.path.startswith("/sessions/"):
            return await call_next(request)

        # The shared-session SSE stream (/api/sessions/{id}/stream) is likewise
        # opened by an EventSource that cannot send Authorization headers. It is
        # protected by the unguessable session id — the same rationale as the
        # legacy bypass above (design §3.9, alternative A). Only the stream is
        # bypassed; the CRUD/ops endpoints are reached by fetch and stay guarded.
        if (
            request.url.path.endswith("/stream")
            and "/api/sessions/" in request.url.path
        ):
            return await call_next(request)

        # MCP_AUTH_ENABLED=false: MCP endpoints bypass auth regardless of auth_enabled
        # or mcp_basic_auth — this takes precedence over both.
        # Unset (None) → MCP follows auth_enabled (backwards compatible).
        if config.mcp_auth_enabled is False:
            path = request.url.path
            if path.startswith("/mcp") or path.startswith("/execute_tool"):
                return await call_next(request)

        # In MCP-only mode, only require auth for MCP and execute_tool paths
        if config.mcp_basic_auth and not config.auth_enabled:
            path = request.url.path
            if not (path.startswith("/mcp") or path.startswith("/execute_tool")):
                return await call_next(request)

        auth_header = request.headers.get("Authorization")
        if not auth_header:
            return JSONResponse(
                status_code=401,
                content={"detail": "Authentication required"},
                headers={"WWW-Authenticate": 'Basic realm="Community Knowledge Graph"'},
            )

        try:
            scheme, credentials = auth_header.split(" ", 1)

            if scheme.lower() == "bearer":
                token = config.auth_bearer_token or ""
                if not token or not secrets.compare_digest(credentials.strip(), token):
                    raise ValueError("invalid bearer token")

            elif scheme.lower() == "basic":
                if not config.auth_password:
                    raise ValueError("basic auth not configured")
                decoded = base64.b64decode(credentials).decode("utf-8")
                username, _, password = decoded.partition(":")
                ok_user = secrets.compare_digest(username, config.auth_username)
                ok_pass = secrets.compare_digest(password, config.auth_password)
                if not (ok_user and ok_pass):
                    raise ValueError("invalid basic credentials")

            else:
                raise ValueError(f"unsupported scheme: {scheme}")

        except Exception:
            return JSONResponse(
                status_code=401,
                content={"detail": "Invalid credentials"},
                headers={"WWW-Authenticate": 'Basic realm="Community Knowledge Graph"'},
            )

        return await call_next(request)


def add_cors_middleware(app: FastAPI, config: AppConfig) -> None:
    """Add CORS middleware to allow external clients (like the ChatGPT MCP connector).

    Credentials cannot be allowed when wildcard origins are used, for security.
    """
    cors_origins = config.cors_allowed_origins
    allow_credentials = "*" not in cors_origins

    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=allow_credentials,
        allow_methods=["*"],  # Allow all methods (GET, POST, OPTIONS, etc.)
        allow_headers=["*"],  # Allow all headers
    )
