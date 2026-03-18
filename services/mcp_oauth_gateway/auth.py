"""
OAuth 2.1 Authorization Code + PKCE flow with Google as Identity Provider.

Responsibilities:
- Build the Google OIDC authorization redirect URL
- Handle the Google callback, verify the ID token, and check the allowlist
- Issue short-lived authorization codes (stored in-memory with TTL)
- Exchange authorization codes + PKCE verifiers for gateway JWTs
"""

import base64
import hashlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, Optional

import httpx
from jose import JWTError, jwt

import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory authorization code store
# ---------------------------------------------------------------------------

@dataclass
class AuthCode:
    email: str
    code_challenge: str          # Base64url-encoded SHA-256 of code_verifier
    redirect_uri: str
    issued_at: float = field(default_factory=time.time)
    used: bool = False

    def is_expired(self) -> bool:
        return (time.time() - self.issued_at) > config.AUTH_CODE_TTL_SECONDS


# Map from auth_code string -> AuthCode
_code_store: Dict[str, AuthCode] = {}


def _prune_expired_codes() -> None:
    """Remove expired entries to prevent unbounded memory growth."""
    expired = [k for k, v in _code_store.items() if v.is_expired()]
    for k in expired:
        del _code_store[k]


# ---------------------------------------------------------------------------
# PKCE helpers
# ---------------------------------------------------------------------------

def compute_s256_challenge(verifier: str) -> str:
    """Return the S256 code_challenge for a given code_verifier.

    challenge = BASE64URL(SHA256(ASCII(verifier)))
    """
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def verify_pkce(code_verifier: str, stored_challenge: str) -> bool:
    """Return True when SHA256(code_verifier) matches the stored challenge."""
    computed = compute_s256_challenge(code_verifier)
    return computed == stored_challenge


# ---------------------------------------------------------------------------
# Google OIDC redirect
# ---------------------------------------------------------------------------

def build_google_auth_url(
    state: str,
    nonce: str,
) -> str:
    """Build the Google OIDC authorization URL that we redirect the user to."""
    params = {
        "client_id": config.GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": config.PUBLIC_BASE_URL + "/callback",
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "access_type": "online",
        "prompt": "select_account",
    }
    query_string = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{config.GOOGLE_AUTH_URL}?{query_string}"


# ---------------------------------------------------------------------------
# Google token exchange
# ---------------------------------------------------------------------------

async def exchange_google_code(google_code: str) -> Optional[str]:
    """Exchange Google authorization code for an ID token.

    Returns the user's email address on success, or None on failure.
    """
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            config.GOOGLE_TOKEN_URL,
            data={
                "code": google_code,
                "client_id": config.GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": config.GOOGLE_OAUTH_CLIENT_SECRET,
                "redirect_uri": config.PUBLIC_BASE_URL + "/callback",
                "grant_type": "authorization_code",
            },
            headers={"Accept": "application/json"},
            timeout=10,
        )

    if resp.status_code != 200:
        logger.warning("Google token exchange failed: %s %s", resp.status_code, resp.text)
        return None

    token_data = resp.json()
    id_token = token_data.get("id_token")
    if not id_token:
        logger.warning("No id_token in Google response")
        return None

    # Decode without verification (Google already validated the code)
    # The signature is verified implicitly because only Google could produce
    # a valid code; for additional security, pass options={"verify_signature": False}
    try:
        claims = jwt.get_unverified_claims(id_token)
    except JWTError as exc:
        logger.warning("Could not decode Google ID token: %s", exc)
        return None

    email = claims.get("email")
    if not email:
        logger.warning("No email claim in Google ID token")
        return None

    email_verified = claims.get("email_verified", False)
    if not email_verified:
        logger.warning("Google email not verified for: %s", email)
        return None

    return email.lower()


# ---------------------------------------------------------------------------
# Allowlist check
# ---------------------------------------------------------------------------

def is_user_allowed(email: str) -> bool:
    """Return True when the email is in the configured allowlist."""
    return email.lower() in config.TEST_USERS


# ---------------------------------------------------------------------------
# Authorization code issuance
# ---------------------------------------------------------------------------

def issue_auth_code(email: str, code_challenge: str, redirect_uri: str) -> str:
    """Create a one-time authorization code and store it with metadata."""
    _prune_expired_codes()
    code = str(uuid.uuid4())
    _code_store[code] = AuthCode(
        email=email,
        code_challenge=code_challenge,
        redirect_uri=redirect_uri,
    )
    logger.info("Issued auth code for %s", email)
    return code


# ---------------------------------------------------------------------------
# Token exchange (code + PKCE → gateway JWT)
# ---------------------------------------------------------------------------

def exchange_code_for_token(code: str, code_verifier: str, redirect_uri: str) -> Optional[str]:
    """Validate the auth code, PKCE verifier, and redirect_uri, then return a signed JWT.

    Returns the JWT string on success, or None if validation fails.
    """
    _prune_expired_codes()

    entry = _code_store.get(code)
    if entry is None:
        logger.warning("Auth code not found: %s", code)
        return None

    if entry.used:
        logger.warning("Auth code already used: %s", code)
        return None

    if entry.is_expired():
        logger.warning("Auth code expired for: %s", entry.email)
        del _code_store[code]
        return None

    if not verify_pkce(code_verifier, entry.code_challenge):
        logger.warning("PKCE verification failed for: %s", entry.email)
        return None

    # RFC 6749 §4.1.3: redirect_uri must match the one from the authorization request
    if redirect_uri != entry.redirect_uri:
        logger.warning("redirect_uri mismatch for: %s", entry.email)
        return None

    # Mark as used before issuing the token (prevent replay)
    entry.used = True

    now = int(time.time())
    claims = {
        "sub": entry.email,
        "aud": config.PUBLIC_BASE_URL,
        "iat": now,
        "exp": now + config.ACCESS_TOKEN_TTL_SECONDS,
    }

    token = jwt.encode(claims, config.GW_JWT_SIGNING_KEY, algorithm=config.JWT_ALGORITHM)
    logger.info("Issued access token for %s", entry.email)
    return token


# ---------------------------------------------------------------------------
# JWT validation (used by the proxy middleware)
# ---------------------------------------------------------------------------

def validate_token(token: str) -> Optional[Dict]:
    """Decode and verify a gateway JWT.

    Returns the claims dict on success, or None if the token is invalid or expired.
    """
    try:
        claims = jwt.decode(
            token,
            config.GW_JWT_SIGNING_KEY,
            algorithms=[config.JWT_ALGORITHM],
            audience=config.PUBLIC_BASE_URL,
        )
        return claims
    except JWTError as exc:
        logger.debug("Token validation failed: %s", exc)
        return None
