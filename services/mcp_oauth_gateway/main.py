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
GET|POST /mcp/sse{/subpath}                  – Proxy: MCP SSE (auth required)
POST /messages                               – Proxy: MCP POST (auth required)
"""

import logging
import os
import time
import urllib.parse
import uuid

from fastapi import FastAPI, HTTPException, Request
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

# In-memory Dynamic Client Registration store (RFC 7591)
# Keyed by client_id → registration dict
dcr_clients: dict[str, dict] = {}


# ---------------------------------------------------------------------------
# Dynamic Client Registration (RFC 7591)
# ---------------------------------------------------------------------------

class ClientRegistrationRequest(BaseModel):
    client_name: str | None = None
    redirect_uris: list[str]
    grant_types: list[str] = ["authorization_code"]
    token_endpoint_auth_method: str = "none"


@app.post("/register")
async def register_client(body: ClientRegistrationRequest) -> JSONResponse:
    """Register a new OAuth client dynamically (RFC 7591).

    No client_secret is issued – PKCE (S256) is the security mechanism.
    """
    if not body.redirect_uris:
        raise HTTPException(status_code=400, detail="redirect_uris is required and must not be empty")

    client_id = str(uuid.uuid4())
    issued_at = int(time.time())

    registration = {
        "client_id": client_id,
        "client_id_issued_at": issued_at,
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


async def _mcp_sse_handler(request: Request):
    """Proxy /mcp/sse requests to the upstream MCP service (auth required)."""
    claims = _require_valid_token(request)
    logger.info("MCP SSE proxy request from sub=%s method=%s path=%s",
                claims.get("sub"), request.method, request.url.path)
    if request.method == "POST":
        return await proxy.proxy_post_mcp_sse(request)
    return await proxy.proxy_sse(request)

app.api_route("/mcp/sse", methods=["GET", "POST"])(_mcp_sse_handler)
app.api_route("/mcp/sse/{subpath:path}", methods=["GET", "POST"])(_mcp_sse_handler)


@app.post("/messages")
async def messages_proxy(request: Request):
    """Proxy MCP POST messages to the upstream service (auth required)."""
    claims = _require_valid_token(request)
    logger.info("POST proxy request from sub=%s", claims.get("sub"))
    return await proxy.proxy_post(request)


# ---------------------------------------------------------------------------
# Local / direct execution
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
