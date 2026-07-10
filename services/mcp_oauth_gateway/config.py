"""
Configuration module for the MCP OAuth Gateway.

All configuration is read from environment variables at startup.
"""

import os
from typing import List, Optional


def _required(name: str) -> str:
    """Read a required environment variable or raise a clear error."""
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Required environment variable '{name}' is not set")
    return value


def _optional(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


# Google OAuth 2.0 credentials
GOOGLE_OAUTH_CLIENT_ID: str = _required("GOOGLE_OAUTH_CLIENT_ID")
GOOGLE_OAUTH_CLIENT_SECRET: str = _required("GOOGLE_OAUTH_CLIENT_SECRET")

# Key used to sign gateway-issued JWTs (HMAC-SHA256)
GW_JWT_SIGNING_KEY: str = _required("GW_JWT_SIGNING_KEY")

# Optional static bearer token for non-OAuth clients. When absent, gateway remains OAuth-only.
GATEWAY_API_KEY: Optional[str] = _optional("GATEWAY_API_KEY") or None

# Comma-separated list of allowed user email addresses
_raw_test_users: str = _required("TEST_USERS")
TEST_USERS: List[str] = [e.strip().lower() for e in _raw_test_users.split(",") if e.strip()]

# URL of the upstream CommunityOverview service (no trailing slash)
UPSTREAM_MCP_BASE_URL: str = _required("UPSTREAM_MCP_BASE_URL").rstrip("/")

# Public base URL of this gateway (no trailing slash)
PUBLIC_BASE_URL: str = _required("PUBLIC_BASE_URL").rstrip("/")

# TCP port the server listens on
PORT: int = int(_optional("PORT", "8080"))

# Comma-separated list of allowed CORS origins (default: wildcard).
# When set to specific origins, credentials are allowed; when wildcard, credentials are disabled.
_raw_cors_origins: str = _optional("CORS_ALLOWED_ORIGINS", "*")
CORS_ALLOWED_ORIGINS: List[str] = [o.strip() for o in _raw_cors_origins.split(",") if o.strip()]

# Google OIDC endpoints (public, no secrets)
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://openidconnect.googleapis.com/v1/userinfo"

# Authorization code TTL in seconds
AUTH_CODE_TTL_SECONDS: int = 300  # 5 minutes

# Access token TTL in seconds (60 days – lets SSE clients reconnect with the
# same token after a Cloud Run scale-to-zero event without re-running OAuth)
ACCESS_TOKEN_TTL_SECONDS: int = 60 * 24 * 3600  # 60 days

# JWT algorithm
JWT_ALGORITHM = "HS256"
