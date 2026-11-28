"""Live profile schema changes: validate, activate, recompute, announce.

A ``SchemaSource`` (``backend/config/config_loader.py``) announces a change;
``SchemaChangeCoordinator.apply`` then

1. builds the new config strictly (no fallback to defaults),
2. refuses it when a stored node or edge the active schema admits would no
   longer be admitted,
3. lets each registered listener prepare its derived state; a listener that
   raises refuses the change,
4. activates the config in place and rebuilds the storage's search index,
5. commits every prepared listener, then emits ``schema.update`` through the
   event dispatcher, so a reaction to the event sees the recomputed state.

A commit that fails leaves derived state behind the active schema; until
every commit succeeds, re-applying the same schema runs the listeners again
instead of reporting it unchanged.

A refused change leaves the active schema and everything derived from it as
it was.
"""

import asyncio
import logging
from contextlib import nullcontext

from backend.config import config_loader
from backend.core.events.models import (
    EntityData,
    EntityKind,
    Event,
    EventContext,
    EventOrigin,
    EventType,
)

logger = logging.getLogger(__name__)

# Conflicts reported back for a refused change; the rest are counted only.
MAX_CONFLICTS = 20


def graph_conflicts(storage, schema, active):
    """Stored nodes and edges ``active`` admits but the candidate ``schema`` not.

    Data the active schema already refuses (it may predate a rule) does not
    block a change.
    """
    nodes = {node.id: node for node in storage.get_all_nodes()}
    conflicts = [
        {"kind": "node", "id": node.id, "type": node.type_str}
        for node in nodes.values()
        if node.type_str in active.node_types and node.type_str not in schema.node_types
    ]

    def allowed(edge, source, target, rules):
        return config_loader.relationship_type_allows_node_types(
            edge.type_str,
            source.type_str,
            target.type_str,
            source.subtypes,
            target.subtypes,
            schema=rules,
        )["allowed"]

    for edge in storage.get_all_edges():
        # A removed type must be checked by name: the endpoint rules below
        # admit any unconfigured relationship type.
        if (
            edge.type_str in active.relationship_types
            and edge.type_str not in schema.relationship_types
        ):
            conflicts.append({"kind": "edge", "id": edge.id, "type": edge.type_str})
            continue
        source, target = nodes.get(edge.source), nodes.get(edge.target)
        if source is None or target is None:
            continue
        if allowed(edge, source, target, active) and not allowed(
            edge, source, target, schema
        ):
            conflicts.append({"kind": "edge", "id": edge.id, "type": edge.type_str})
    return conflicts


def _type_names(config):
    return {
        "node_types": sorted(config.schema_.node_types),
        "relationship_types": sorted(config.schema_.relationship_types),
    }


class SchemaChangeCoordinator:
    """Serializes schema changes for one graph and fans them out to listeners."""

    def __init__(self, storage):
        self.storage = storage
        self.listeners = []
        self.lock = asyncio.Lock()
        # A commit failed after activation: derived state may be stale.
        self.stale = False

    def add_listener(self, prepare):
        """Register ``prepare(config)``, called before activation.

        It may raise to refuse the change, and returns an async ``commit()``
        (or None) awaited after the new schema is active. Commits run newest
        listener first, so the server's own listener, registered at startup,
        sees (and announces) tool descriptions a later listener changed.
        """
        self.listeners.append(prepare)

    def attach(self, source):
        """Follow ``source``'s change announcements, if it makes any."""
        subscribe = getattr(source, "subscribe", None)
        if subscribe is not None:
            subscribe(self.apply)

    async def apply(self, raw_config=None):
        """Apply ``raw_config``, or the source's current content when omitted."""
        async with self.lock:
            refused = await asyncio.to_thread(self._activate, raw_config)
            if refused is not None:
                return refused
            return await self._commit()

    def _activate(self, raw_config):
        # Returns a refusal, or None with the commits and event left pending.
        self._pending = []
        self._pending_event = None
        try:
            if raw_config is None:
                raw_config = config_loader.get_schema_source().load()
            candidate = config_loader.ConfigLoader.candidate(raw_config)
        except Exception:
            logger.warning("Schema change refused: invalid config", exc_info=True)
            return {"accepted": False, "error_code": "invalid_schema"}
        loader = config_loader._get_loader()
        before = loader.config
        unchanged = candidate.config.model_dump() == before.model_dump()
        if unchanged and not self.stale:
            return {"accepted": True, "changed": False}
        # Held to activation, so no write admits a node the check did not see.
        with getattr(self.storage, "write_lock", nullcontext()):
            conflicts = graph_conflicts(
                self.storage, candidate.config.schema_, before.schema_
            )
            if conflicts:
                return {
                    "accepted": False,
                    "error_code": "schema_conflicts_with_graph",
                    "conflict_count": len(conflicts),
                    "conflicts": conflicts[:MAX_CONFLICTS],
                }
            try:
                commits = [prepare(candidate.config) for prepare in self.listeners]
            except Exception:
                logger.warning("Schema change refused by a listener", exc_info=True)
                return {"accepted": False, "error_code": "schema_rejected"}
            loader.activate(candidate)
            refresh = getattr(self.storage, "refresh_schema_search", None)
            if refresh is not None:
                refresh()
        self._pending = [commit for commit in commits if commit is not None]
        if unchanged:
            # A retry of failed commits: the change itself was already announced.
            return None
        self._pending_event = Event(
            event_type=EventType.SCHEMA_UPDATE,
            origin=EventContext(event_origin=EventOrigin.SYSTEM),
            entity=EntityData(
                kind=EntityKind.SCHEMA,
                id="profile",
                type="schema",
                before=_type_names(before),
                after=_type_names(candidate.config),
            ),
        )
        return None

    async def _commit(self):
        self.stale = False
        for commit in reversed(self._pending):
            try:
                await commit()
            except Exception:
                self.stale = True
                logger.exception("Schema change listener failed after activation")
        event, self._pending, self._pending_event = self._pending_event, [], None
        if event is not None:
            try:
                await asyncio.to_thread(self.storage.dispatch_event, event)
            except Exception:
                logger.exception("Schema change event dispatch failed")
        if self.stale:
            return {"accepted": True, "changed": True, "error_code": "listener_failed"}
        return {"accepted": True, "changed": True}


def mcp_schema_listener(mcp, build_instructions, extension=None):
    """Recompute what the MCP server derives from the schema, and signal it.

    The SDK reads instructions on every ``initialize`` and ``server/discover``,
    so replacing them on the low-level server reaches every later client.
    Changes go out on the server's subscription bus as standard events.
    """
    from mcp.shared.subscriptions import ResourceUpdated, ToolsListChanged

    from backend.skills.mcp import (
        GraphSemanticsProvider,
        InProcessSkillChangePublisher,
        SkillsExtension,
        apply_catalog,
        graph_semantics_inputs,
    )

    def tool_listing():
        return [
            (tool.name, tool.description, tool.parameters)
            for tool in mcp._tool_manager.list_tools()
        ]

    def prepare(config):
        updated = None
        if extension is not None:
            schema, presentation = graph_semantics_inputs(config)
            providers = [
                GraphSemanticsProvider(
                    schema, presentation, provider.ttl_ms, provider.cache_scope
                )
                if type(provider) is GraphSemanticsProvider
                else provider
                for provider in extension.providers
            ]
            # Validates and sanitizes now, so a rejected schema is refused.
            updated = SkillsExtension(
                providers, extension.ttl_ms, extension.cache_scope
            )
        tools = tool_listing()

        async def commit():
            bus = mcp._subscriptions
            mcp._lowlevel_server.instructions = build_instructions()
            if updated is not None:
                async with extension.refresh_lock:
                    changed = await apply_catalog(
                        mcp, extension, updated, InProcessSkillChangePublisher(bus)
                    )
                for uri in changed:
                    await bus.publish(ResourceUpdated(uri=uri))
            if tool_listing() != tools:
                await bus.publish(ToolsListChanged())

        return commit

    return prepare
