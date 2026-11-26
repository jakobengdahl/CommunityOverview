"""Publishing side of federation: who may read this graph over the federation route.

The federation route serves the graph read-only to other instances. Each request
presents a key, which a ``FederationKeyProvider`` classifies; the route serves only
on ``served`` and answers every other outcome with the same 401. Keys and every
environment variable they come from are read once, when the provider is built at
startup; a change applies on restart.
"""

from __future__ import annotations

import hmac
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, List, Mapping, Optional, Protocol, Tuple

from .config import FederationConsumer, FederationSettings

logger = logging.getLogger(__name__)

FEDERATION_GRAPH_PATH = "/federation/v1/graph"
FETCH_EVENT = "federation.fetch"

OUTCOME_SERVED = "served"
OUTCOME_REFUSED_AUTH = "refused_auth"
OUTCOME_REFUSED_DISABLED = "refused_disabled"
OUTCOME_REFUSED_EXPIRED = "refused_expired"
OUTCOME_ERROR = "error"

CONSUMER_OPEN = "open"
CONSUMER_SHARED = "shared"
CONSUMER_UNAUTHENTICATED = "unauthenticated"


@dataclass(frozen=True)
class FederationAuthResult:
    """Who presented the key and whether the federation route may serve them."""

    consumer_id: str
    outcome: str

    @property
    def served(self) -> bool:
        return self.outcome == OUTCOME_SERVED


REFUSED = FederationAuthResult(CONSUMER_UNAUTHENTICATED, OUTCOME_REFUSED_AUTH)


class FederationKeyProvider(Protocol):
    """Decide whether a presented federation key may read the published graph.

    ``presented`` is the key from ``Authorization: Bearer`` or ``X-API-Key``, or
    None when the request carried neither. The provider never sees which header
    it came from and must not log it.
    """

    def authenticate(self, presented: Optional[str]) -> FederationAuthResult: ...


class OpenKeyProvider:
    """``open`` mode: every request is served, with or without a key."""

    def authenticate(self, presented: Optional[str]) -> FederationAuthResult:
        return FederationAuthResult(CONSUMER_OPEN, OUTCOME_SERVED)


class RefuseAllKeyProvider:
    """Admits nothing; used when the configured mode cannot be honoured."""

    def authenticate(self, presented: Optional[str]) -> FederationAuthResult:
        return REFUSED


def _matches(presented: bytes, expected: bytes) -> bool:
    return hmac.compare_digest(presented, expected)


class SharedTokenKeyProvider:
    """``shared_token`` mode: every consumer presents the same token."""

    def __init__(self, token: str):
        self._token = token.encode("utf-8")

    def authenticate(self, presented: Optional[str]) -> FederationAuthResult:
        if presented is not None and _matches(presented.encode("utf-8"), self._token):
            return FederationAuthResult(CONSUMER_SHARED, OUTCOME_SERVED)
        return REFUSED


@dataclass(frozen=True)
class _LoadedConsumer:
    consumer_id: str
    token: bytes
    enabled: bool
    expires_at: Optional[datetime]


class ConsumerKeyProvider:
    """``per_consumer`` mode: each consuming graph has its own token."""

    def __init__(self, consumers: Iterable[_LoadedConsumer], now=None):
        self._consumers = tuple(consumers)
        self._now = now or (lambda: datetime.now(timezone.utc))

    def authenticate(self, presented: Optional[str]) -> FederationAuthResult:
        if presented is None:
            return REFUSED
        candidate = presented.encode("utf-8")
        match = None
        # Compare against every loaded consumer, disabled and expired included,
        # so the time taken does not reveal whether or where a match was.
        for consumer in self._consumers:
            if _matches(candidate, consumer.token) and match is None:
                match = consumer
        if match is None:
            return REFUSED
        if not match.enabled:
            return FederationAuthResult(match.consumer_id, OUTCOME_REFUSED_DISABLED)
        if match.expires_at is not None and match.expires_at <= self._now():
            return FederationAuthResult(match.consumer_id, OUTCOME_REFUSED_EXPIRED)
        return FederationAuthResult(match.consumer_id, OUTCOME_SERVED)


def _env_value(name: Optional[str]) -> str:
    return os.getenv(name, "") if name else ""


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _load_consumers(
    consumers: List[FederationConsumer], reserved: frozenset
) -> List[_LoadedConsumer]:
    ids = [consumer.consumer_id for consumer in consumers]
    loaded: List[Tuple[FederationConsumer, str]] = []
    for consumer in consumers:
        if ids.count(consumer.consumer_id) > 1:
            logger.error(
                "Federation consumer %s is configured more than once; skipped",
                consumer.consumer_id,
            )
            continue
        token = _env_value(consumer.token_env)
        if not token:
            logger.error(
                "Federation consumer %s: variable %s is unset or empty; skipped",
                consumer.consumer_id,
                consumer.token_env,
            )
            continue
        if token in reserved:
            logger.error(
                "Federation consumer %s: token equals an instance credential; skipped",
                consumer.consumer_id,
            )
            continue
        loaded.append((consumer, token))

    tokens = [token for _, token in loaded]
    result = []
    for consumer, token in loaded:
        # A shared value would make the per-consumer fetch log name the wrong one.
        if tokens.count(token) > 1:
            logger.error(
                "Federation consumer %s shares its token with another consumer; skipped",
                consumer.consumer_id,
            )
            continue
        result.append(
            _LoadedConsumer(
                consumer_id=consumer.consumer_id,
                token=token.encode("utf-8"),
                enabled=consumer.enabled,
                expires_at=_as_utc(consumer.expires_at),
            )
        )
    return result


def is_publishing(settings: FederationSettings) -> bool:
    """The federation route exists only while both switches are on."""
    return settings.enabled and settings.publish.enabled


def build_key_provider(
    settings: FederationSettings,
    *,
    external_provider: Optional[FederationKeyProvider] = None,
    reserved_tokens: Iterable[Optional[str]] = (),
) -> Optional[FederationKeyProvider]:
    """Build the key provider for the configured mode, or None when not publishing.

    ``external_provider`` serves ``access: external``, where consumers and their
    keys are managed by an authentication layer outside this config; without one
    that mode refuses every request. ``reserved_tokens`` are instance credentials
    a federation key must never equal, so handing one out as a federation key
    cannot also open the rest of the instance.
    """
    if not is_publishing(settings):
        return None
    publish = settings.publish
    reserved = frozenset(token for token in reserved_tokens if token)

    if publish.access == "open":
        logger.warning(
            "Federation publishing is in open mode: the graph is readable "
            "at %s without a key",
            FEDERATION_GRAPH_PATH,
        )
        return OpenKeyProvider()
    if publish.access == "shared_token":
        token = _env_value(publish.shared_token_env)
        if not token or token in reserved:
            logger.error(
                "Federation shared_token mode: variable %s is unset, empty or "
                "equals an instance credential; publishing off",
                publish.shared_token_env,
            )
            return None
        return SharedTokenKeyProvider(token)
    if publish.access == "per_consumer":
        return ConsumerKeyProvider(_load_consumers(publish.consumers, reserved))
    if publish.access == "external":
        if external_provider is None:
            logger.error(
                "Federation external mode has no key provider installed; "
                "every federation request is refused"
            )
            return RefuseAllKeyProvider()
        return external_provider
    logger.error(
        "Unknown federation publish access mode %r; publishing off", publish.access
    )
    return None


def presented_key(headers: Mapping[str, str]) -> Tuple[Optional[str], bool]:
    """Return the presented key and whether the request is consistent.

    Accepts ``Authorization: Bearer <key>`` and ``X-API-Key: <key>``; both
    present with different values is inconsistent and refused.
    """
    scheme, _, credentials = (headers.get("authorization") or "").partition(" ")
    bearer = credentials.strip() if scheme.lower() == "bearer" else ""
    api_key = (headers.get("x-api-key") or "").strip()
    if bearer and api_key and bearer != api_key:
        return None, False
    return (bearer or api_key or None), True


def log_fetch(
    result: FederationAuthResult,
    *,
    graph_id: str,
    request_id: str = "",
    nodes: Optional[int] = None,
    edges: Optional[int] = None,
) -> None:
    """Write the one record every federation request leaves; never the key."""
    fields = {
        "event": FETCH_EVENT,
        "consumer_id": result.consumer_id,
        "graph_id": graph_id,
        "outcome": result.outcome,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "request_id": request_id,
    }
    if result.served:
        fields["nodes"] = nodes
        fields["edges"] = edges
    logger.info(
        "%s consumer_id=%s graph_id=%s outcome=%s nodes=%s edges=%s",
        FETCH_EVENT,
        result.consumer_id,
        graph_id,
        result.outcome,
        nodes,
        edges,
        extra={"federation_fetch": fields},
    )
