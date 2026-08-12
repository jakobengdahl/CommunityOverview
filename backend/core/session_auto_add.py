"""Session-scoped auto-add agents.

A session-scoped auto-add agent watches for newly created nodes that match a
defined pattern (node types and/or keywords) and adds each match to *one*
visualization session's live view — additively, never clearing existing content
(it builds on the additive ``add_to_visualization`` push path).

The agent is bound to a single session: its rule lives in memory keyed by
``session_id``, dies with the session, and can only ever push to its own
session. It therefore never leaks nodes into another session — the defining
guarantee of the feature.

This is a deterministic, in-process reactor: it runs on the synchronous
``node.create`` graph event, needs no LLM, and never mutates the graph. Because
it does not write to the graph it cannot generate further events, so — unlike
graph-mutating agents — it needs no loop-prevention wiring.

Matching mirrors the create-event semantics of
:class:`backend.core.events.dispatcher.EventDispatcher`: a node matches when its
type is in ``node_types`` (when any are given) *and* any keyword appears
(case-insensitively) in its name/description/summary/tags (when any are given).
At least one constraint is required — a rule matching every created node is
rejected at the boundary as a footgun that would flood the canvas, mirroring the
kiosk assistant's guard against an empty allowlist.

The push itself is injected as a callback (``push(session_id, node_dict)``) so
this core module has no dependency on the service/transport layer.
"""

import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .events.models import EntityKind, EventType

logger = logging.getLogger(__name__)

# Bounds so an unauthenticated caller cannot grow the registry without limit.
MAX_RULES_PER_SESSION = 20
MAX_SESSIONS_WITH_RULES = 5000
MAX_PATTERN_ENTRIES = 50
MAX_ENTRY_LENGTH = 200


class AutoAddRuleError(ValueError):
    """Raised when an auto-add rule fails validation at the boundary."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_pattern(values: Optional[List[str]]) -> List[str]:
    """Clean a node_types/keywords list: coerce to str, strip, drop empties,
    dedupe (order-preserving), and cap count and entry length."""
    if not values:
        return []
    cleaned: List[str] = []
    seen = set()
    for raw in values:
        if not isinstance(raw, str):
            raw = str(raw)
        entry = raw.strip()[:MAX_ENTRY_LENGTH]
        if not entry or entry in seen:
            continue
        seen.add(entry)
        cleaned.append(entry)
        if len(cleaned) >= MAX_PATTERN_ENTRIES:
            break
    return cleaned


def _searchable_text(node_data: Dict[str, Any]) -> str:
    """Build the lowercased text a keyword match runs against.

    Mirrors EventDispatcher._matches_keywords: name, description, summary, tags.
    """
    parts: List[str] = []
    for key in ("name", "description", "summary"):
        value = node_data.get(key)
        if isinstance(value, str) and value:
            parts.append(value)
    tags = node_data.get("tags")
    if isinstance(tags, list):
        parts.extend(str(t) for t in tags)
    return " ".join(parts).lower()


def node_matches(
    node_type: str,
    node_data: Dict[str, Any],
    node_types: List[str],
    keywords: List[str],
) -> bool:
    """Return True if a created node matches a rule's pattern.

    Node-type and keyword constraints are ANDed (both must pass when set), the
    same way EventDispatcher combines a subscription's ``node_types`` and
    ``keywords.any``. A rule with neither constraint would match everything;
    such rules are rejected at creation, so this stays a pure predicate.
    """
    if node_types and node_type not in node_types:
        return False
    if keywords:
        text = _searchable_text(node_data)
        if not any(keyword.lower() in text for keyword in keywords):
            return False
    return True


@dataclass
class AutoAddRule:
    """One session-scoped auto-add agent."""

    agent_id: str
    session_id: str
    node_types: List[str] = field(default_factory=list)
    keywords: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=_now_iso)

    def matches(self, node_type: str, node_data: Dict[str, Any]) -> bool:
        return node_matches(node_type, node_data, self.node_types, self.keywords)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "node_types": list(self.node_types),
            "keywords": list(self.keywords),
            "created_at": self.created_at,
        }


class SessionAutoAddRegistry:
    """In-memory registry of session-scoped auto-add agents.

    Rules are keyed by ``session_id`` and are ephemeral, mirroring the
    visualization-session push registry: they hold no durable state, are pruned
    when their session goes away, and are lost on restart (acceptable because a
    visualization session is itself ephemeral).
    """

    def __init__(self) -> None:
        self._by_session: Dict[str, Dict[str, AutoAddRule]] = {}

    def add_rule(
        self,
        session_id: str,
        node_types: Optional[List[str]] = None,
        keywords: Optional[List[str]] = None,
    ) -> AutoAddRule:
        """Register an auto-add agent on *session_id* and return it.

        Raises AutoAddRuleError if the pattern has no constraint, or a per-session
        or global capacity limit is reached.
        """
        clean_types = _normalize_pattern(node_types)
        clean_keywords = _normalize_pattern(keywords)
        if not clean_types and not clean_keywords:
            raise AutoAddRuleError(
                "an auto-add agent needs at least one node type or keyword; "
                "a rule with neither would add every created node to the view"
            )

        existing = self._by_session.get(session_id)
        if existing is None:
            if len(self._by_session) >= MAX_SESSIONS_WITH_RULES:
                raise AutoAddRuleError(
                    "the maximum number of sessions with auto-add agents has "
                    "been reached"
                )
        elif len(existing) >= MAX_RULES_PER_SESSION:
            raise AutoAddRuleError(
                "this session already has the maximum number of auto-add agents "
                f"({MAX_RULES_PER_SESSION})"
            )

        rule = AutoAddRule(
            agent_id=secrets.token_hex(8),
            session_id=session_id,
            node_types=clean_types,
            keywords=clean_keywords,
        )
        self._by_session.setdefault(session_id, {})[rule.agent_id] = rule
        return rule

    def list_rules(self, session_id: str) -> List[AutoAddRule]:
        return list(self._by_session.get(session_id, {}).values())

    def remove_rule(self, session_id: str, agent_id: str) -> bool:
        """Remove one agent. Returns True if it existed."""
        rules = self._by_session.get(session_id)
        if not rules or agent_id not in rules:
            return False
        del rules[agent_id]
        if not rules:
            del self._by_session[session_id]
        return True

    def clear_session(self, session_id: str) -> int:
        """Drop all agents for a session. Returns the number removed."""
        rules = self._by_session.pop(session_id, None)
        return len(rules) if rules else 0

    def matching_sessions(self, node_type: str, node_data: Dict[str, Any]) -> List[str]:
        """Return the session ids with at least one rule matching this node.

        Each session appears at most once even if several of its rules match, so
        a session receives a newly created node exactly once.
        """
        matched: List[str] = []
        for session_id, rules in self._by_session.items():
            if any(rule.matches(node_type, node_data) for rule in rules.values()):
                matched.append(session_id)
        return matched

    def prune_to_sessions(self, live_session_ids) -> int:
        """Drop rules for sessions no longer live. Returns sessions removed.

        Called from the session-registry eviction cycle so rules for a session
        that has been TTL-evicted (its browser gone) don't linger in memory.
        """
        live = set(live_session_ids)
        stale = [sid for sid in self._by_session if sid not in live]
        for sid in stale:
            del self._by_session[sid]
        return len(stale)

    @property
    def total_rules(self) -> int:
        return sum(len(rules) for rules in self._by_session.values())

    @property
    def session_count(self) -> int:
        return len(self._by_session)


def build_node_create_listener(
    registry: SessionAutoAddRegistry,
    push: Callable[[str, Dict[str, Any]], None],
) -> Callable[[Any], None]:
    """Build a graph system-listener that feeds matching new nodes to sessions.

    On each ``node.create`` event the listener finds every session whose
    auto-add agent matches the node and calls ``push(session_id, node_dict)`` —
    which the caller wires to the additive ``add_to_visualization`` push path.
    The node's ``embedding`` is stripped so the pushed payload matches the shape
    the frontend already receives from search results.

    A push failure for one session is isolated so it can't stop delivery to the
    others; the graph mutation is never affected (the emitter also guards
    listeners, so a match-time error cannot break ``add_nodes``).
    """

    def listener(event: Any) -> None:
        if getattr(event, "event_type", None) != EventType.NODE_CREATE:
            return
        entity = getattr(event, "entity", None)
        if entity is None or entity.kind != EntityKind.NODE:
            return
        node_data = entity.after or {}
        sessions = registry.matching_sessions(entity.type, node_data)
        if not sessions:
            return
        payload = {key: value for key, value in node_data.items() if key != "embedding"}
        for session_id in sessions:
            try:
                push(session_id, payload)
            except Exception:  # pragma: no cover - defensive isolation
                logger.exception(
                    "auto-add push failed for session %s (node %s)",
                    session_id,
                    entity.id,
                )

    return listener
