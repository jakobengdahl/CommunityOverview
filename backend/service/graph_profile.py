"""Deterministic, access-filtered statistics for the visible local graph."""

import asyncio
import logging
from collections import Counter, defaultdict, namedtuple
from contextlib import nullcontext
from contextvars import ContextVar, copy_context
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


_Node = namedtuple("_Node", "id name type subtypes tags graph_id")
_Edge = namedtuple("_Edge", "source type target")


def snapshot(storage):
    """Copy the plain fields ``aggregate`` reads, under the storage write lock.

    Writes mutate nodes in place, so the fields are read while writers are
    excluded; the hold is one pass of attribute reads, never a graph copy.
    """
    with getattr(storage, "write_lock", nullcontext()):
        nodes = [
            _Node(
                n.id,
                n.name,
                n.type_str,
                tuple(n.subtypes),
                tuple(n.tags),
                access.node_graph_id(n),
            )
            for n in storage.get_all_nodes()
            if not n.archived
        ]
        edges = [
            _Edge(e.source, e.type_str, e.target)
            for e in storage.get_all_edges()
            if not e.archived
        ]
    return nodes, edges


def profile(storage, graph_access):
    return aggregate(snapshot(storage), graph_access)


def aggregate(graph, graph_access):
    """The profile of a ``snapshot`` as ``graph_access`` sees it; no lock held."""
    nodes = [n for n in graph[0] if graph_access.matches(graph_id=n.graph_id)]
    by_id = {node.id: node for node in nodes}
    # As ``access.filter_nodes_and_edges``: an edge needs both endpoints visible.
    edges = [e for e in graph[1] if e.source in by_id and e.target in by_id]
    types = Counter(n.type for n in nodes)
    subtypes = Counter((n.type, s) for n in nodes for s in set(n.subtypes))
    tags = Counter(tag for n in nodes for tag in set(n.tags))
    triples = defaultdict(set)
    degree = Counter()
    for edge in edges:
        source, target = by_id[edge.source], by_id[edge.target]
        triples[(source.type, edge.type, target.type)].add(source.id)
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
                "type": bounded(n.type),
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


# Baseline of a stream whose first flush has not run yet.
_PENDING = object()


@dataclass(frozen=True)
class _ProfileUpdates:
    # token -> (scope, profile) computed for that stream's changed profile.
    updates: dict


class ProfileChangePublisher:
    """Bus adapter comparing the profile separately for every listen stream.

    Only streams whose listen filter names ``graph://profile`` are tracked.
    Graph events only mark the profile dirty; one flusher, in a worker thread,
    takes one ``snapshot`` at most once per ``debounce`` seconds and aggregates
    it once per distinct access scope, without the storage write lock.

    A new stream's baseline is taken by the next flush, not by ``subscribe``
    on the event loop; a change landing before that flush is part of it.

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
                update = event.updates.get(token)
                if update is None:
                    return
                current, denied = self._access(context, self.hook)
                if denied or current != update[0]:
                    # Baseline kept: the next graph change compares against
                    # what this stream was last told, so nothing is lost.
                    return
                with self.lock:
                    if token not in self.listeners:
                        return
                    self.listeners[token] = (context, update[1])
                listener(ResourceUpdated(uri=PROFILE_URI))

        if not tracked:
            return self.bus.subscribe(deliver)
        with self.lock:
            self.listeners[token] = (context, _PENDING)
            unsubscribe = self.bus.subscribe(deliver)
            self._schedule(prompt=True)

        def close():
            with self.lock:
                self.listeners.pop(token, None)
                unsubscribe()

        return close

    async def publish(self, event):
        await self.bus.publish(event)

    def on_event(self, event=None):
        # Also the storage reload listener (no event). Runs under the storage
        # write lock: O(1), no profile work here.
        with self.lock:
            if self.listeners:
                self._schedule()

    def _schedule(self, prompt=False):
        self.dirty = True
        if self.scheduled or self.loop is None or self.loop.is_closed():
            return
        self.scheduled = True
        asyncio.run_coroutine_threadsafe(self._flush(prompt), self.loop)

    async def _flush(self, prompt=False):
        # Only an idle flusher starting for a new stream skips the debounce,
        # so opening streams in a loop still snapshots once per window.
        try:
            while True:
                if not prompt:
                    await asyncio.sleep(self.debounce)
                prompt = False
                with self.lock:
                    self.dirty = False
                    listeners = list(self.listeners.items())
                changed, baselines = await asyncio.to_thread(self._recompute, listeners)
                with self.lock:
                    for token, baseline in baselines.items():
                        entry = self.listeners.get(token)
                        if entry is not None and entry[1] is _PENDING:
                            self.listeners[token] = (entry[0], baseline)
                if changed:
                    await self.bus.publish(_ProfileUpdates(changed))
                with self.lock:
                    if not self.dirty:
                        self.scheduled = False
                        return
        except BaseException:
            with self.lock:
                self.scheduled = False
            raise

    def _recompute(self, listeners):
        """Return ``(changed, baselines)`` from one snapshot of the graph."""
        changed, baselines, profiles = {}, {}, {}
        try:
            graph = snapshot(self.storage)
        except Exception:
            logger.exception("Graph profile snapshot failed")
            return changed, baselines
        for token, (context, previous) in listeners:
            scope, denied = self._access(context, self.hook)
            if denied:
                # Keep the last visible baseline: access returning to an
                # unchanged profile announces nothing. A new stream starts
                # from none, so its first visible profile is announced.
                if previous is _PENDING:
                    baselines[token] = None
                continue
            try:
                updated = self._aggregate(profiles, graph, scope)
            except Exception:
                logger.exception("Graph profile recompute failed")
                continue
            if previous is _PENDING:
                baselines[token] = updated
            # The baseline advances only when ``deliver`` announces it.
            elif updated != previous:
                changed[token] = (scope, updated)
        return changed, baselines

    @staticmethod
    def _aggregate(profiles, graph, scope):
        """``aggregate`` once per distinct scope within a flush."""
        try:
            return profiles[scope]
        except KeyError:
            profiles[scope] = result = aggregate(graph, scope)
            return result
        except TypeError:  # A hook's unhashable scope is not shared.
            return aggregate(graph, scope)
