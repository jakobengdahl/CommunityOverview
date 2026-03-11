"""
MCP OAuth Gateway – FastAPI entry point.

This service sits in front of a CommunityOverview MCP instance and enforces
OAuth 2.1 Authorization Code + PKCE before forwarding traffic upstream.

Endpoints
---------
GET  /.well-known/oauth-authorization-server  – OAuth metadata (discovery)
GET  /authorize                               – Start the OAuth flow
GET  /callback                               – Google OIDC callback
POST /token                                  – Exchange auth code for JWT
GET  /sse                                    – Proxy: SSE stream (auth required)
POST /messages                               – Proxy: MCP POST (auth required)
"""

import logging
import urllib.parse
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse

import auth
import config
import proxy

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="MCP OAuth Gateway", version="1.0.0")


# ---------------------------------------------------------------------------
# OAuth metadata discovery
# ---------------------------------------------------------------------------

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
            "response_types_supported": ["code"],
            "code_challenge_methods_supported": ["S256"],
            "grant_types_supported": ["authorization_code"],
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

    # Enforce that redirect_uri points back to this gateway's callback
    expected_redirect = config.PUBLIC_BASE_URL + "/callback"
    if redirect_uri != expected_redirect:
        raise HTTPException(
            status_code=400,
            detail=f"redirect_uri must be {expected_redirect}",
        )

    # Encode gateway state so we can recover it in the callback.
    # Format: <original_state>|<code_challenge>|<redirect_uri>
    # All parts are URL-encoded individually to avoid delimiter collisions.
    gateway_state = "|".join(
        [
            urllib.parse.quote(state, safe=""),
            urllib.parse.quote(code_challenge, safe=""),
            urllib.parse.quote(redirect_uri, safe=""),
        ]
    )

    nonce = str(uuid.uuid4())
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

    # Decode gateway state: <original_state>|<code_challenge>|<redirect_uri>
    parts = state.split("|")
    if len(parts) != 3:
        raise HTTPException(status_code=400, detail="Invalid state parameter")

    original_state = urllib.parse.unquote(parts[0])
    code_challenge = urllib.parse.unquote(parts[1])
    redirect_uri = urllib.parse.unquote(parts[2])

    # Exchange Google code for user's email
    email = await auth.exchange_google_code(code)
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

    if grant_type != "authorization_code":
        raise HTTPException(status_code=400, detail="grant_type must be authorization_code")
    if not code:
        raise HTTPException(status_code=400, detail="code is required")
    if not code_verifier:
        raise HTTPException(status_code=400, detail="code_verifier is required")

    access_token = auth.exchange_code_for_token(code=code, code_verifier=code_verifier)
    if access_token is None:
        raise HTTPException(
            status_code=400,
            detail="Invalid, expired, or already-used authorization code, or PKCE mismatch",
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


def _require_valid_token(request: Request) -> dict:
    """Validate the Bearer token and return its claims, or raise 401."""
    token = _extract_bearer_token(request)
    if token is None:
        raise HTTPException(status_code=401, detail="Missing Bearer token")

    claims = auth.validate_token(token)
    if claims is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    return claims


@app.get("/sse")
async def sse_proxy(request: Request):
    """Proxy SSE stream to the upstream MCP service (auth required)."""
    claims = _require_valid_token(request)
    logger.info("SSE proxy request from sub=%s", claims.get("sub"))
    return await proxy.proxy_sse(request)


@app.post("/messages")
async def messages_proxy(request: Request):
    """Proxy MCP POST messages to the upstream service (auth required)."""
    claims = _require_valid_token(request)
    logger.info("POST proxy request from sub=%s", claims.get("sub"))
    return await proxy.proxy_post(request)
