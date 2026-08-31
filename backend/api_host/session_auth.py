"""Signed session-cookie helpers for the api_host form-login flow.

Some browsers never surface the native HTTP Basic dialog on a ``WWW-Authenticate``
challenge (Microsoft Edge does this on the sspcloud deployment), so those users
cannot authenticate through the challenge at all. The form-login flow validates
the same credentials once and hands the browser a signed, HttpOnly session
cookie that the auth middleware accepts on subsequent requests — no native
dialog required, so the login flow is identical across browsers.

The cookie is signed with a key derived from an existing server secret (the
bearer token, or the Basic password as fallback), so no new configuration or
secret is introduced.
"""

import hashlib
import hmac
import time
from typing import Optional
from urllib.parse import urlsplit

from starlette.requests import Request

from .config import AppConfig

SESSION_COOKIE_NAME = "co_session"
SESSION_TTL_SECONDS = 12 * 60 * 60  # 12h

_KDF_SALT = b"co-session-cookie-v1"
_KDF_ITERATIONS = 200_000
_signing_key_cache: dict[str, bytes] = {}


def _signing_key(config: AppConfig) -> Optional[bytes]:
    """Derive the HMAC signing key from an existing server secret (no new config).

    Uses PBKDF2-HMAC-SHA256 (a deliberately expensive KDF) rather than a plain
    fast hash, and caches the result per secret so the cost is paid once per
    process. The key is deterministic from the secret, so it stays stable across
    worker processes and restarts. Returns None when neither a bearer token nor a
    Basic password is set; the caller then treats session cookies as unavailable.
    """
    secret = config.auth_bearer_token or config.auth_password
    if not secret:
        return None
    key = _signing_key_cache.get(secret)
    if key is None:
        key = hashlib.pbkdf2_hmac(
            "sha256", secret.encode("utf-8"), _KDF_SALT, _KDF_ITERATIONS
        )
        _signing_key_cache[secret] = key
    return key


def mint_session_cookie(
    config: AppConfig, ttl_seconds: int = SESSION_TTL_SECONDS
) -> Optional[str]:
    """Return a signed ``version.expiry.hmac`` cookie value, or None if unsigned."""
    key = _signing_key(config)
    if key is None:
        return None
    expires = int(time.time()) + ttl_seconds
    payload = f"v1.{expires}"
    sig = hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _value_is_valid(config: AppConfig, value: str) -> bool:
    key = _signing_key(config)
    if key is None or not value:
        return False
    try:
        version, expires_str, sig = value.split(".", 2)
    except ValueError:
        return False
    payload = f"{version}.{expires_str}"
    expected = hmac.new(key, payload.encode("utf-8"), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        expires = int(expires_str)
    except ValueError:
        return False
    return expires >= int(time.time())


def request_has_valid_session(request: Request, config: AppConfig) -> bool:
    """True when the request carries a valid, unexpired session cookie."""
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    return bool(cookie) and _value_is_valid(config, cookie)


def credentials_valid(config: AppConfig, username: str, password: str) -> bool:
    """Constant-time check of a submitted username/password against config."""
    if not config.auth_password:
        return False
    ok_user = hmac.compare_digest(username or "", config.auth_username)
    ok_pass = hmac.compare_digest(password or "", config.auth_password)
    return ok_user and ok_pass


def is_safe_next_path(path: str) -> bool:
    """Allow only same-origin absolute paths as post-login redirect targets.

    A valid target starts with a single ``/`` and, once parsed, carries neither a
    URL scheme nor a network location — so the ``next`` parameter can never become
    an open redirect to another host. Protocol-relative (``//host``) and
    backslash/CR/LF-bearing values are rejected outright.
    """
    if not path or not path.startswith("/") or path.startswith("//"):
        return False
    if "\\" in path or "\n" in path or "\r" in path:
        return False
    parts = urlsplit(path)
    return not parts.scheme and not parts.netloc
