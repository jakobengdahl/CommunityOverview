"""Auth and CORS middleware wiring for the api_host application."""

import base64
import html
import logging
import secrets

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.middleware.cors import CORSMiddleware
from starlette.requests import Request

from .config import AppConfig
from .diagnostics import PUBLIC_READINESS_PATH, PUBLIC_STARTUP_DIAGNOSTICS_PATH
from .session_auth import request_has_valid_session

logger = logging.getLogger(__name__)

_REALM = 'Basic realm="Community Knowledge Graph"'

# Sign-in page shown to browsers on an unauthenticated navigation. It carries a
# real credential form that POSTs to /auth/login and sets a session cookie, so
# the login flow works identically in every browser — including those that never
# surface the native Basic dialog (Microsoft Edge on some deployments). The 401
# still carries WWW-Authenticate: Basic so browsers that do honour it can also
# use the native dialog. Either way the user sees a usable page rather than the
# raw JSON error body. __NEXT__ / __ERROR__ are substituted per request.
_SIGNIN_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Sign in required</title>
  <link rel="icon" href="/favicon.svg" />
  <style>
    :root { color-scheme: dark; }
    html, body {
      margin: 0;
      padding: 0;
      height: 100%;
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto,
        Helvetica, Arial, sans-serif;
      background: #121212;
      color: #eaeaea;
    }
    body {
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .auth-card {
      background: rgba(26, 26, 26, 0.95);
      border: 1px solid #333;
      border-radius: 12px;
      box-shadow: 0 8px 32px rgba(0, 0, 0, 0.5);
      padding: 32px 40px;
      max-width: 420px;
      width: 100%;
      box-sizing: border-box;
      text-align: center;
    }
    .auth-card h1 {
      margin: 0 0 12px 0;
      font-size: 1.4rem;
      font-weight: 600;
      color: #fff;
    }
    .auth-card p {
      margin: 0 0 20px 0;
      color: #bbb;
      font-size: 0.95rem;
      line-height: 1.5;
    }
    .auth-form {
      display: flex;
      flex-direction: column;
      gap: 12px;
      text-align: left;
    }
    .auth-form label {
      display: flex;
      flex-direction: column;
      gap: 4px;
      font-size: 0.8rem;
      color: #bbb;
    }
    .auth-form input {
      padding: 10px 12px;
      border: 1px solid #333;
      border-radius: 8px;
      background: #151515;
      color: #eaeaea;
      font-size: 0.95rem;
    }
    .auth-form input:focus {
      outline: none;
      border-color: #646cff;
    }
    .auth-form button {
      margin-top: 4px;
      padding: 10px 18px;
      background: #646cff;
      color: #fff;
      border: none;
      border-radius: 8px;
      font-size: 0.9rem;
      font-weight: 500;
      cursor: pointer;
      transition: background 0.15s;
    }
    .auth-form button:hover {
      background: #535bf2;
    }
    .auth-error {
      margin: 0 0 16px 0;
      color: #ff8a8a;
      font-size: 0.9rem;
    }
  </style>
</head>
<body>
  <div class="auth-card">
    <h1>Sign in required</h1>
    <p>This instance is protected. Enter your credentials to sign in.</p>
    __ERROR__
    <form class="auth-form" method="post" action="/auth/login">
      <input type="hidden" name="next" value="__NEXT__" />
      <label>Username
        <input type="text" name="username" autocomplete="username" autofocus />
      </label>
      <label>Password
        <input type="password" name="password" autocomplete="current-password" />
      </label>
      <button type="submit">Sign in</button>
    </form>
  </div>
</body>
</html>
"""


def render_signin_page(next_path: str = "/web/", error: str = "") -> str:
    """Render the sign-in page with a safe redirect target and optional error."""
    error_block = f'<p class="auth-error">{html.escape(error)}</p>' if error else ""
    return _SIGNIN_TEMPLATE.replace(
        "__NEXT__", html.escape(next_path, quote=True)
    ).replace("__ERROR__", error_block)


def _client_accepts_html(request: Request) -> bool:
    """True for a top-level browser navigation, which sends ``Accept: text/html``.

    API / fetch / MCP clients send ``Accept: */*`` or ``application/json`` and
    must keep receiving the JSON error body they parse; only browsers get the
    HTML sign-in page.
    """
    return "text/html" in request.headers.get("Accept", "")


def _requested_target(request: Request) -> str:
    """The path (with query) the user was trying to reach, for post-login return."""
    target = request.url.path
    if request.url.query:
        target = f"{target}?{request.url.query}"
    return target


def _unauthorized(request: Request, detail: str) -> Response:
    """Build a 401 challenge, negotiated by Accept so browsers never see raw JSON.

    Browsers get the HTML sign-in form (with the requested path as the return
    target); API / fetch / MCP clients keep the JSON body. Both carry
    WWW-Authenticate so the native Basic dialog still fires where supported.
    """
    headers = {"WWW-Authenticate": _REALM}
    if _client_accepts_html(request):
        return HTMLResponse(
            content=render_signin_page(next_path=_requested_target(request)),
            status_code=401,
            headers=headers,
        )
    return JSONResponse(status_code=401, content={"detail": detail}, headers=headers)


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
            "/readyz",
            "/healthz/deep",
            "/healthz/storage",
            "/healthz/secrets",
            "/info",
            PUBLIC_STARTUP_DIAGNOSTICS_PATH,
            "/auth/login",
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

        # A valid signed session cookie (issued by POST /auth/login after a
        # successful form login) authenticates the request without the native
        # Basic dialog — the browser-independent path, used by Edge.
        if request_has_valid_session(request, config):
            return await call_next(request)

        auth_header = request.headers.get("Authorization")
        if not auth_header:
            return _unauthorized(request, "Authentication required")

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
            return _unauthorized(request, "Invalid credentials")

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
