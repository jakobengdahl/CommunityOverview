"""Deterministic, access-filtered statistics for the visible local graph."""

import asyncio
import logging
from collections import Counter, defaultdict
from contextlib import nullcontext
from contextvars import ContextVar, copy_context
from copy import deepcopy
from dataclasses import dataclass
from threading import RLock

from mcp_types import SubscriptionsListenRequestParams
from pydantic import ValidationError
from starlette.requests import Request

from backend.runtime.authorization import GRAPH_ACTION_READ, use_request_authorization
from . import access

logger = logging.getLogger(__name__)

# Bound every collection in the response, including type and subtype counts.
MAX_ITEMS = 100
MAX_STRING = 256
PROFILE_URI = "graph://profile"
# Resource URIs named by the subscriptions/listen request being served.
_LISTEN_URIS = ContextVar("listen_uris", default=frozenset())


def bounded(value):
    return value[:MAX_STRING]


def profile(storage, graph_access):
    # Capture visibility and content together while writers are excluded.
    with getattr(storage, "write_lock", nullcontext()):
        nodes = deepcopy(storage.get_all_nodes())
        edges = deepcopy(storage.get_all_edges())
    nodes, edges = access.filter_nodes_and_edges(
        nodes=[n for n in nodes if not n.archived],
        edges=[e for e in edges if not e.archived],
        graph_access=graph_access,
    )
    by_id = {node.id: node for node in nodes}
    types = Counter(n.type_str for n in nodes)
    subtypes = Counter((n.type_str, s) for n in nodes for s in set(n.subtypes))
    tags = Counter(tag for n in nodes for tag in set(n.tags))
    triples = defaultdict(set)
    degree = Counter()
    for edge in edges:
        source, target = by_id[edge.source], by_id[edge.target]
        triples[(source.type_str, edge.type_str, target.type_str)].add(source.id)
        degree[source.id] += 1
        degree[target.id] += 1
    return {
        "node_count": len(nodes),
        "node_types": [
            {"type": bounded(k), "count": v}
            for k, v in sorted(types.items())[:MAX_ITEMS]
        ],
        "subtypes": [
            {"type": bounded(k[0]), "subtype": bounded(k[1]), "count": v}
            for k, v in sorted(subtypes.items())[:MAX_ITEMS]
        ],
        "tags": [
            {"tag": bounded(k), "count": v}
            for k, v in sorted(tags.items(), key=lambda item: (-item[1], item[0]))[
                :MAX_ITEMS
            ]
        ],
        "relationships": [
            {
                "source_type": bounded(k[0]),
                "relationship_type": bounded(k[1]),
                "target_type": bounded(k[2]),
                "source_count": len(v),
                "share": len(v) / types[k[0]],
            }
            for k, v in sorted(triples.items())[:MAX_ITEMS]
        ],
        "central_nodes": [
            {
                "id": bounded(n.id),
                "name": bounded(n.name),
                "type": bounded(n.type_str),
                "degree": degree[n.id],
            }
            for n in sorted(nodes, key=lambda n: (-degree[n.id], n.id))[:MAX_ITEMS]
        ],
    }


@dataclass(frozen=True)
class Intersection:
    scopes: tuple

    def matches(self, *, graph_id):
        return all(scope.matches(graph_id=graph_id) for scope in self.scopes)


def read_access(hook):
    decisions = [
        access.evaluate_graph_access(hook, action=GRAPH_ACTION_READ, target=target)
        for target in ("describe_graph", "get_node_details")
    ]
    for target, decision in zip(("describe_graph", "get_node_details"), decisions):
        if not decision.allowed:
            return None, access.build_access_denied_result(
                action=GRAPH_ACTION_READ, target=target, decision=decision
            )
    return Intersection(tuple(d.graph_access for d in decisions)), None


def describe(storage, hook, publisher=None):
    scope, denied = read_access(hook)
    if denied is not None:
        return denied
    return profile(storage, scope)


@dataclass(frozen=True)
class _ProfileUpdates:
    tokens: frozenset


class ProfileChangePublisher:
    """Bus adapter comparing the profile separately for every listen stream.

    Only streams whose listen filter names ``graph://profile`` are tracked.
    Graph events only mark the profile dirty; one flusher recomputes it at most
    once per ``debounce`` seconds, outside the storage write lock.

    Change notifications follow the main graph only: staged session layers do
    not trigger them. A session-bound caller reading ``describe_graph`` or the
    resource still gets its composed view on read.
    """

    @staticmethod
    def _access(context, hook):
        """``read_access`` in a listener's context; a failing hook denies."""
        try:
            return context.copy().run(read_access, hook)
        except Exception:
            logger.exception("Graph profile access check failed")
            return None, True

    debounce = 0.25

    def __init__(self, storage, bus, hook):
        self.storage, self.bus, self.hook = storage, bus, hook
        self.loop = None
        self.lock = RLock()
        self.listeners = {}
        self.generation = 0
        self.dirty = self.scheduled = False

    async def listen_filter(self, ctx, call_next):
        """Server middleware exposing a listen request's URIs to ``subscribe``."""
        if ctx.method != "subscriptions/listen":
            return await call_next(ctx)
        try:
            params = SubscriptionsListenRequestParams.model_validate(
                ctx.params or {}, by_name=False
            )
            uris = frozenset(params.notifications.resource_subscriptions or ())
        except ValidationError:
            uris = frozenset()  # The listen handler rejects the request.
        # As for tool calls: on legacy SSE the handler inherits the opening
        # request's context, so bind this listen message's own headers.
        request = getattr(ctx, "request", None)
        authorization = (
            use_request_authorization(headers=request.headers)
            if isinstance(request, Request)
            else nullcontext()
        )
        reset = _LISTEN_URIS.set(uris)
        try:
            with authorization:
                return await call_next(ctx)
        finally:
            _LISTEN_URIS.reset(reset)

    def subscribe(self, listener):
        from mcp.shared.subscriptions import ResourceUpdated

        token = object()
        context = copy_context()
        tracked = PROFILE_URI in _LISTEN_URIS.get()

        def deliver(event):
            if not isinstance(event, _ProfileUpdates):
                listener(event)
            elif tracked:
                current, denied = self._access(context, self.hook)
                if not denied and (token, current) in event.tokens:
                    listener(ResourceUpdated(uri=PROFILE_URI))

        if not tracked:
            return self.bus.subscribe(deliver)
        with self.lock:
            generation = self.generation
        scope, denied = self._access(context, self.hook)
        baseline = profile(self.storage, scope) if not denied else None
        with self.lock:
            self.listeners[token] = (context, baseline)
            unsubscribe = self.bus.subscribe(deliver)
            # A change that landed while the baseline was computed is not lost.
            if self.generation != generation:
                self._schedule()

        def close():
            with self.lock:
                self.listeners.pop(token, None)
                unsubscribe()

        return close

    async def publish(self, event):
        await self.bus.publish(event)

    def on_event(self, event):
        # Runs under the storage write lock: O(1), no profile work here.
        with self.lock:
            self.generation += 1
            if self.listeners:
                self._schedule()

    def _schedule(self):
        self.dirty = True
        if self.scheduled or self.loop is None or self.loop.is_closed():
            return
        self.scheduled = True
        asyncio.run_coroutine_threadsafe(self._flush(), self.loop)

    async def _flush(self):
        try:
            while True:
                await asyncio.sleep(self.debounce)
                with self.lock:
                    self.dirty = False
                    listeners = list(self.listeners.items())
                changed = await asyncio.to_thread(self._recompute, listeners)
                if changed:
                    await self.bus.publish(_ProfileUpdates(frozenset(changed)))
                with self.lock:
                    if not self.dirty:
                        self.scheduled = False
                        return
        except BaseException:
            with self.lock:
                self.scheduled = False
            raise

    def _recompute(self, listeners):
        changed = set()
        for token, (context, previous) in listeners:
            scope, denied = self._access(context, self.hook)
            if denied:
                # Keep the last visible baseline: access returning to an
                # unchanged profile announces nothing.
                continue
            try:
                updated = profile(self.storage, scope)
            except Exception:
                logger.exception("Graph profile recompute failed")
                continue
            if updated != previous:
                changed.add((token, scope))
            with self.lock:
                if token in self.listeners:
                    self.listeners[token] = (context, updated)
        return changed
