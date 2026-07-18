"""
MCP OAuth Gateway – FastAPI entry point.

This service sits in front of a CommunityOverview MCP instance and enforces
OAuth 2.1 Authorization Code + PKCE before forwarding traffic upstream.

Endpoints
---------
GET  /.well-known/oauth-protected-resource     – Protected resource metadata (RFC 9470)
GET  /.well-known/oauth-authorization-server  – OAuth metadata (discovery)
POST /register                                – Dynamic Client Registration (RFC 7591)
GET  /authorize                               – Start the OAuth flow
GET  /callback                               – Google OIDC callback
POST /token                                  – Exchange auth code for JWT
GET  /sse                                    – Proxy: SSE stream (auth required)
GET  /mcp/sse                                – Proxy: MCP SSE stream (auth required)
POST /mcp/sse{/subpath}                      – Proxy: MCP POST (auth required)
POST /messages                               – Proxy: MCP POST (auth required)
POST /mcp/messages/                          – Proxy: MCP message POST (auth required)
"""

import hmac
import logging
import os
import time
import urllib.parse
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

import auth
import config
import proxy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="MCP OAuth Gateway", version="1.0.0")

# CORS – required for browser-based MCP clients (MCPJam, ChatGPT plugin preview, etc.)
# Credentials cannot be allowed when wildcard origins are used per the CORS spec;
# a wildcard+credentials combination is a security risk on token endpoints.
_cors_origins = config.CORS_ALLOWED_ORIGINS
_allow_credentials = "*" not in _cors_origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_allow_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# In-memory Dynamic Client Registration store (RFC 7591)
# Keyed by client_id → registration dict
dcr_clients: dict[str, dict] = {}

# Bounds on the DCR store so the unauthenticated /register endpoint cannot grow
# it without limit (memory-growth DoS). Registrations expire after the TTL, and
# the oldest are evicted once the cap is reached.
MAX_DCR_CLIENTS = 1000
DCR_CLIENT_TTL_SECONDS = 30 * 24 * 3600  # 30 days

# Per-client-IP token-bucket rate limit on the unauthenticated /register
# endpoint so it cannot be flooded (the store cap above bounds memory; this
# bounds request rate). Legitimate MCP clients register once per connection, so
# a generous budget never affects them.
REGISTER_RATE_CAPACITY = 20.0  # burst
REGISTER_RATE_REFILL_PER_SEC = 0.2  # ~12 registrations/min sustained
_REGISTER_BUCKET_IDLE_TTL = 3600.0  # evict an IP's bucket after 1 h of silence
_REGISTER_BUCKET_SWEEP_THRESHOLD = 10_000  # sweep idle keys past this many

_register_buckets: dict[str, tuple[float, float]] = {}  # ip -> (tokens, last_ts)


def _client_ip(request: Request) -> str:
    """Best-effort client IP for rate limiting.

    Behind Cloud Run (one trusted proxy hop) the real client IP is the
    right-most ``X-Forwarded-For`` entry — the address the proxy actually saw.
    Client-supplied entries sit further left and are ignored, so the key cannot
    be spoofed to mint a fresh budget. Falls back to the socket peer for direct
    or local execution where no ``X-Forwarded-For`` header is present.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    parts = [p.strip() for p in forwarded.split(",") if p.strip()]
    if parts:
        return parts[-1]
    return request.client.host if request.client else "unknown"


def _check_register_rate(request: Request) -> None:
    """Consume one token for the caller's IP, raising 429 when the budget is spent."""
    now = time.monotonic()
    if len(_register_buckets) > _REGISTER_BUCKET_SWEEP_THRESHOLD:
        cutoff = now - _REGISTER_BUCKET_IDLE_TTL
        for stale in [ip for ip, (_, ts) in _register_buckets.items() if ts < cutoff]:
            del _register_buckets[stale]

    ip = _client_ip(request)
    tokens, last = _register_buckets.get(ip, (REGISTER_RATE_CAPACITY, now))
    tokens = min(
        REGISTER_RATE_CAPACITY,
        tokens + (now - last) * REGISTER_RATE_REFILL_PER_SEC,
    )
    if tokens < 1.0:
        _register_buckets[ip] = (tokens, now)
        raise HTTPException(status_code=429, detail="registration rate limit exceeded")
    _register_buckets[ip] = (tokens - 1.0, now)


def _prune_dcr_clients() -> None:
    """Drop expired registrations and enforce the size cap (oldest-first)."""
    now = int(time.time())
    expired = [
        cid
        for cid, reg in dcr_clients.items()
        if now >= reg.get("client_id_expires_at", 0)
    ]
    for cid in expired:
        del dcr_clients[cid]

    if len(dcr_clients) >= MAX_DCR_CLIENTS:
        # Evict the oldest registrations until we are back under the cap.
        ordered = sorted(
            dcr_clients.items(), key=lambda kv: kv[1].get("client_id_issued_at", 0)
        )
        for cid, _reg in ordered[: len(dcr_clients) - MAX_DCR_CLIENTS + 1]:
            del dcr_clients[cid]


def _is_loopback_redirect(redirect_uri: str) -> bool:
    """Return True for RFC 8252 loopback redirect URIs (localhost / 127.0.0.1)."""
    host = urllib.parse.urlparse(redirect_uri).hostname or ""
    return host in ("127.0.0.1", "::1", "localhost")


def _redirect_uri_allowed(redirect_uri: str) -> bool:
    """Return True when the gateway may send an auth code to *redirect_uri*.

    Loopback URIs are always allowed (local dev). When
    ``ALLOWED_REDIRECT_ORIGINS`` is configured, the URI must start with one of
    the allowed scheme://host[:port] prefixes. When the list is empty the
    gateway stays permissive (legacy behaviour) but logs a warning, so an
    operator can lock this down without a code change.
    """
    if not redirect_uri:
        return False
    if _is_loopback_redirect(redirect_uri):
        return True
    if not config.ALLOWED_REDIRECT_ORIGINS:
        logger.warning(
            "ALLOWED_REDIRECT_ORIGINS is not set — accepting redirect_uri without "
            "an allow-list. Set it in production to prevent auth-code interception."
        )
        return True
    normalized = redirect_uri.rstrip("/")
    return any(
        normalized == origin or redirect_uri.startswith(origin + "/")
        for origin in config.ALLOWED_REDIRECT_ORIGINS
    )


# ---------------------------------------------------------------------------
# Dynamic Client Registration (RFC 7591)
# ---------------------------------------------------------------------------

class ClientRegistrationRequest(BaseModel):
    client_name: str | None = None
    redirect_uris: list[str]
    grant_types: list[str] = ["authorization_code"]
    token_endpoint_auth_method: str = "none"


@app.post("/register")
async def register_client(
    request: Request, body: ClientRegistrationRequest
) -> JSONResponse:
    """Register a new OAuth client dynamically (RFC 7591).

    No client_secret is issued – PKCE (S256) is the security mechanism.
    """
    _check_register_rate(request)

    if not body.redirect_uris:
        raise HTTPException(status_code=400, detail="redirect_uris is required and must not be empty")

    _prune_dcr_clients()

    client_id = str(uuid.uuid4())
    issued_at = int(time.time())

    registration = {
        "client_id": client_id,
        "client_id_issued_at": issued_at,
        "client_id_expires_at": issued_at + DCR_CLIENT_TTL_SECONDS,
        "redirect_uris": body.redirect_uris,
        "grant_types": body.grant_types,
        "token_endpoint_auth_method": body.token_endpoint_auth_method,
    }
    if body.client_name is not None:
        registration["client_name"] = body.client_name

    dcr_clients[client_id] = registration
    logger.info("Registered new DCR client %s (name=%s)", client_id, body.client_name)

    return JSONResponse(registration, status_code=201)


# ---------------------------------------------------------------------------
# OAuth metadata discovery
# ---------------------------------------------------------------------------

@app.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource():
    """Return OAuth Protected Resource Metadata (RFC 9470).

    Claude uses this endpoint to discover the authorization server.
    """
    return {
        "resource": f"{config.PUBLIC_BASE_URL}/mcp/sse",
        "authorization_servers": [config.PUBLIC_BASE_URL],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["openid", "email", "profile"],
    }


@app.get("/.well-known/oauth-authorization-server")
async def oauth_metadata() -> JSONResponse:
    """Return OAuth 2.0 Authorization Server Metadata (RFC 8414).

    ChatGPT fetches this URL to learn how to authenticate with the gateway.
    """
    return JSONResponse(
        {
            "issuer": config.PUBLIC_BASE_URL,
            "authorization_endpoint": config.PUBLIC_BASE_URL + "/authorize",
            "token_endpoint": config.PUBLIC_BASE_URL + "/token",
            "registration_endpoint": config.PUBLIC_BASE_URL + "/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code"],
            "scopes_supported": ["openid", "email", "profile"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
        }
    )


# ---------------------------------------------------------------------------
# OAuth authorize endpoint
# ---------------------------------------------------------------------------

@app.get("/authorize")
async def authorize(
    request: Request,
    client_id: str = "",
    redirect_uri: str = "",
    state: str = "",
    code_challenge: str = "",
    code_challenge_method: str = "",
) -> RedirectResponse:
    """Begin the OAuth 2.1 Authorization Code + PKCE flow.

    Validates PKCE parameters, then redirects the user to Google for login.
    """
    # PKCE is mandatory
    if not code_challenge:
        raise HTTPException(status_code=400, detail="code_challenge is required")
    if code_challenge_method.upper() != "S256":
        raise HTTPException(
            status_code=400,
            detail="Only code_challenge_method=S256 is supported",
        )

    # Reject redirect targets the gateway is not allowed to send codes to.
    if not _redirect_uri_allowed(redirect_uri):
        raise HTTPException(status_code=400, detail="redirect_uri not permitted")

    # A fresh nonce binds this authorization request to the returned ID token
    # (OIDC replay defence); it is carried in the gateway state so the callback
    # can assert the token echoes it back.
    nonce = str(uuid.uuid4())

    # Encode gateway state so we can recover it in the callback.
    # Format: <original_state>|<code_challenge>|<redirect_uri>|<nonce>
    # All parts are URL-encoded individually to avoid delimiter collisions.
    gateway_state = "|".join(
        [
            urllib.parse.quote(state, safe=""),
            urllib.parse.quote(code_challenge, safe=""),
            urllib.parse.quote(redirect_uri, safe=""),
            urllib.parse.quote(nonce, safe=""),
        ]
    )

    google_url = auth.build_google_auth_url(state=gateway_state, nonce=nonce)

    logger.info("Redirecting to Google for authorization (state prefix: %s...)", state[:8])
    return RedirectResponse(url=google_url, status_code=302)


# ---------------------------------------------------------------------------
# Google OIDC callback
# ---------------------------------------------------------------------------

@app.get("/callback")
async def callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
) -> RedirectResponse:
    """Handle the Google OIDC callback.

    1. Exchanges the Google code for an ID token.
    2. Checks the email against the allowlist.
    3. Issues a gateway authorization code.
    4. Redirects to the original redirect_uri with the code and state.
    """
    if error:
        logger.warning("Google returned an error: %s", error)
        raise HTTPException(status_code=400, detail=f"Google OAuth error: {error}")

    if not code or not state:
        raise HTTPException(status_code=400, detail="Missing code or state from Google")

    # Decode gateway state: <original_state>|<code_challenge>|<redirect_uri>|<nonce>
    parts = state.split("|")
    if len(parts) != 4:
        raise HTTPException(status_code=400, detail="Invalid state parameter")

    original_state = urllib.parse.unquote(parts[0])
    code_challenge = urllib.parse.unquote(parts[1])
    redirect_uri = urllib.parse.unquote(parts[2])
    nonce = urllib.parse.unquote(parts[3])

    # Re-validate the redirect target: the state is attacker-influenced, so the
    # allow-list check at /authorize must not be trusted to carry through.
    if not _redirect_uri_allowed(redirect_uri):
        raise HTTPException(status_code=400, detail="redirect_uri not permitted")

    # Exchange Google code for user's email, verifying the ID token nonce.
    email = await auth.exchange_google_code(code, expected_nonce=nonce)
    if email is None:
        raise HTTPException(status_code=400, detail="Failed to retrieve user info from Google")

    # Allowlist check
    if not auth.is_user_allowed(email):
        logger.warning("Access denied for %s (not in allowlist)", email)
        raise HTTPException(
            status_code=403,
            detail=f"User {email} is not authorized to use this service",
        )

    # Issue a one-time authorization code
    auth_code = auth.issue_auth_code(
        email=email,
        code_challenge=code_challenge,
        redirect_uri=redirect_uri,
    )

    # Redirect back to the client (ChatGPT)
    params = urllib.parse.urlencode({"code": auth_code, "state": original_state})
    destination = f"{redirect_uri}?{params}"
    logger.info("Callback complete for %s – redirecting to client", email)
    return RedirectResponse(url=destination, status_code=302)


# ---------------------------------------------------------------------------
# Token endpoint
# ---------------------------------------------------------------------------

@app.post("/token")
async def token(request: Request) -> JSONResponse:
    """Exchange an authorization code + PKCE verifier for a gateway JWT.

    Accepts application/x-www-form-urlencoded or application/json bodies.
    """
    content_type = request.headers.get("content-type", "")

    if "application/json" in content_type:
        body = await request.json()
    else:
        # Default: form-encoded
        form = await request.form()
        body = dict(form)

    grant_type = body.get("grant_type", "")
    code = body.get("code", "")
    code_verifier = body.get("code_verifier", "")
    redirect_uri = body.get("redirect_uri", "")

    if grant_type != "authorization_code":
        raise HTTPException(status_code=400, detail="grant_type must be authorization_code")
    if not code:
        raise HTTPException(status_code=400, detail="code is required")
    if not code_verifier:
        raise HTTPException(status_code=400, detail="code_verifier is required")
    if not redirect_uri:
        raise HTTPException(status_code=400, detail="redirect_uri is required")

    access_token = auth.exchange_code_for_token(
        code=code, code_verifier=code_verifier, redirect_uri=redirect_uri,
    )
    if access_token is None:
        raise HTTPException(
            status_code=400,
            detail="Invalid, expired, or already-used authorization code, PKCE mismatch, or redirect_uri mismatch",
        )

    return JSONResponse(
        {
            "access_token": access_token,
            "token_type": "Bearer",
            "expires_in": config.ACCESS_TOKEN_TTL_SECONDS,
        }
    )


# ---------------------------------------------------------------------------
# MCP proxy endpoints (require a valid Bearer token)
# ---------------------------------------------------------------------------

def _extract_bearer_token(request: Request) -> str | None:
    """Return the Bearer token from the Authorization header, or None."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[len("Bearer "):]
    return None


def _matches_static_api_key(token: str | None) -> bool:
    """Return True when the bearer token matches the configured static API key."""
    if config.GATEWAY_API_KEY is None or token is None:
        return False

    expected = config.GATEWAY_API_KEY.encode("utf-8")
    provided = token.encode("utf-8")
    return len(expected) == len(provided) and hmac.compare_digest(provided, expected)


def _require_valid_token(request: Request) -> dict:
    """Validate the Bearer token and return claims/identity, or raise 401."""
    token = _extract_bearer_token(request)
    if token is None:
        raise HTTPException(status_code=401, detail="Missing Bearer token")

    if _matches_static_api_key(token):
        return {"sub": "static-api-key", "auth_type": "api_key"}

    claims = auth.validate_token(token)
    if claims is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    # Re-check the allow-list on every request so removing a user from
    # TEST_USERS revokes their access immediately, even though the gateway's
    # access tokens are long-lived and otherwise non-revocable.
    subject = claims.get("sub", "")
    if not auth.is_user_allowed(subject):
        logger.warning("Token subject %s is no longer allow-listed", subject)
        raise HTTPException(status_code=401, detail="User is no longer authorized")

    return claims


# GET /sse – legacy SSE proxy
@app.get("/sse")
async def sse_proxy(request: Request):
    """Proxy SSE stream to the upstream MCP service (auth required)."""
    claims = _require_valid_token(request)
    logger.info("SSE proxy request from sub=%s", claims.get("sub"))
    return await proxy.proxy_sse(request)


# GET /mcp/sse – SSE proxy (MCP path)
@app.get("/mcp/sse")
async def mcp_sse_get(request: Request):
    """Proxy GET /mcp/sse to upstream SSE stream (auth required)."""
    claims = _require_valid_token(request)
    logger.info("MCP SSE GET from sub=%s", claims.get("sub"))
    return await proxy.proxy_sse(request)


# POST /mcp/sse – forward POST (Streamable HTTP or sub-path messages)
@app.post("/mcp/sse")
async def mcp_sse_post(request: Request):
    """Proxy POST /mcp/sse to upstream (auth required)."""
    claims = _require_valid_token(request)
    logger.info("MCP SSE POST from sub=%s path=%s", claims.get("sub"), request.url.path)
    return await proxy.proxy_post_mcp(request)


# GET|POST /mcp/sse/{subpath} – catch sub-paths like /mcp/sse/messages
@app.get("/mcp/sse/{subpath:path}")
async def mcp_sse_subpath_get(request: Request, subpath: str):
    """Proxy GET /mcp/sse/{subpath} to upstream (auth required)."""
    claims = _require_valid_token(request)
    logger.info("MCP SSE subpath GET from sub=%s path=%s", claims.get("sub"), request.url.path)
    return await proxy.proxy_sse(request)


@app.post("/mcp/sse/{subpath:path}")
async def mcp_sse_subpath_post(request: Request, subpath: str):
    """Proxy POST /mcp/sse/{subpath} to upstream (auth required)."""
    claims = _require_valid_token(request)
    logger.info("MCP SSE subpath POST from sub=%s path=%s", claims.get("sub"), request.url.path)
    return await proxy.proxy_post_mcp(request)


# POST /messages and /messages/ – the upstream SSE app sends endpoint URLs
# like /messages/?session_id=xxx which urljoin resolves against the root.
# Both with and without trailing slash to avoid 307 redirect issues.
@app.post("/messages")
@app.post("/messages/")
async def messages_proxy(request: Request):
    """Proxy MCP POST messages to the upstream service (auth required)."""
    claims = _require_valid_token(request)
    logger.info("POST /messages from sub=%s", claims.get("sub"))
    return await proxy.proxy_post(request)


@app.post("/mcp/messages/")
async def mcp_messages_proxy(request: Request):
    """Proxy MCP message POSTs (with session_id query param) to the upstream (auth required).

    The SSE endpoint event directs clients to POST here. This route ensures
    those requests are authenticated and forwarded rather than returning 404.
    """
    claims = _require_valid_token(request)
    logger.info("MCP messages POST proxy request from sub=%s session=%s",
                claims.get("sub"), request.query_params.get("session_id"))
    return await proxy.proxy_post_mcp(request)


# ---------------------------------------------------------------------------
# Local / direct execution
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
