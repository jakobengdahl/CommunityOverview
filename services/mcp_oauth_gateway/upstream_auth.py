"""Service-identity credentials for calling the upstream Cloud Run service.

The gateway reaches the upstream over its public ``*.run.app`` URL. For that to
work while the upstream is *not* open to the world, the upstream's Cloud Run
service must require ``roles/run.invoker`` and the gateway must present a Google
ID token for its own service account, with the upstream's URL as the audience.

The token is fetched from the Cloud Run metadata server rather than through
``google-auth``. That is the same request google-auth makes on Cloud Run, and it
keeps a security-sensitive component — which exact-pins and hash-verifies every
runtime dependency — from taking on a new one for four lines of HTTP.

Disabled by default. ``UPSTREAM_USE_ID_TOKEN`` has to be turned on explicitly,
so deploying this build changes nothing until the invoker binding is in place;
turning it on before that would authenticate against a service that still allows
everyone, which succeeds either way and is therefore safe to sequence in either
order.
"""

import asyncio
import logging
import time
from typing import Optional

import httpx2 as httpx

import config

logger = logging.getLogger(__name__)

_METADATA_IDENTITY_URL = (
    "http://metadata.google.internal/computeMetadata/v1/"
    "instance/service-accounts/default/identity"
)

# Refresh this long before the token actually expires. An ID token lives an
# hour; a request that starts just inside the boundary must not arrive just
# outside it.
_REFRESH_MARGIN_SECONDS = 300

_lock = asyncio.Lock()
_cached_token: Optional[str] = None
_cached_expiry: float = 0.0


def _now() -> float:
    return time.monotonic()


async def _fetch_id_token(audience: str) -> str:
    """Mint an ID token for ``audience`` from the metadata server."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            _METADATA_IDENTITY_URL,
            params={"audience": audience, "format": "full"},
            headers={"Metadata-Flavor": "Google"},
        )
    resp.raise_for_status()
    token = resp.text.strip()
    if not token:
        raise RuntimeError("metadata server returned an empty ID token")
    return token


async def id_token_header() -> dict:
    """``{"authorization": "Bearer <id-token>"}``, or ``{}`` when disabled.

    Never raises: an upstream call with no token gets a 403 from Cloud Run,
    which surfaces as a clear proxy error, whereas an exception here would take
    down every request including the ones that do not need the token.
    """
    if not config.UPSTREAM_USE_ID_TOKEN:
        return {}

    global _cached_token, _cached_expiry

    # Fast path outside the lock: a valid cached token is the common case, and
    # serialising every proxied request behind one lock would cost more than the
    # token fetch it avoids.
    if _cached_token and _now() < _cached_expiry:
        return {"authorization": f"Bearer {_cached_token}"}

    async with _lock:
        # Re-check: several requests can queue on the lock while the first one
        # refreshes, and they must not each mint another token.
        if _cached_token and _now() < _cached_expiry:
            return {"authorization": f"Bearer {_cached_token}"}
        try:
            token = await _fetch_id_token(config.UPSTREAM_ID_TOKEN_AUDIENCE)
        except Exception:
            logger.exception(
                "Could not mint an upstream ID token; forwarding without one"
            )
            return {}
        _cached_token = token
        _cached_expiry = _now() + 3600 - _REFRESH_MARGIN_SECONDS
        logger.info("Minted upstream ID token for %s", config.UPSTREAM_ID_TOKEN_AUDIENCE)
        return {"authorization": f"Bearer {token}"}


def reset_cache() -> None:
    """Drop the cached token. For tests."""
    global _cached_token, _cached_expiry
    _cached_token = None
    _cached_expiry = 0.0
