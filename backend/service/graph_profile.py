"""Deterministic, access-filtered statistics for the visible local graph."""

from collections import Counter, defaultdict
from contextlib import nullcontext
from contextvars import copy_context
from copy import deepcopy
from dataclasses import dataclass
from threading import RLock

from backend.runtime.authorization import GRAPH_ACTION_READ
from . import access

# Bound every collection in the response, including type and subtype counts.
MAX_ITEMS = 100
MAX_STRING = 256


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
    """Bus adapter comparing the profile separately for every listen stream."""

    def __init__(self, storage, bus, hook):
        self.storage, self.bus, self.hook = storage, bus, hook
        self.loop = None
        self.lock = RLock()
        self.listeners = {}

    def subscribe(self, listener):
        from mcp.shared.subscriptions import ResourceUpdated

        token = object()
        context = copy_context()
        scope, denied = context.copy().run(read_access, self.hook)
        # Keep the same lock order as graph mutation callbacks.
        with getattr(self.storage, "write_lock", nullcontext()):
            with self.lock:
                self.listeners[token] = (
                    context,
                    profile(self.storage, scope) if not denied else None,
                )

                def deliver(event):
                    if isinstance(event, _ProfileUpdates):
                        current, denied = context.copy().run(read_access, self.hook)
                        if not denied and (token, current) in event.tokens:
                            listener(ResourceUpdated(uri="graph://profile"))
                    else:
                        listener(event)

                unsubscribe = self.bus.subscribe(deliver)

        def close():
            with self.lock:
                self.listeners.pop(token, None)
                unsubscribe()

        return close

    async def publish(self, event):
        await self.bus.publish(event)

    def on_event(self, event):
        import asyncio

        with getattr(self.storage, "write_lock", nullcontext()):
            with self.lock:
                changed = set()
                for token, (context, previous) in list(self.listeners.items()):
                    scope, denied = context.copy().run(read_access, self.hook)
                    updated = profile(self.storage, scope) if not denied else None
                    if (
                        updated is not None
                        and previous is not None
                        and updated != previous
                    ):
                        changed.add((token, scope))
                    self.listeners[token] = (context, updated)
                if changed and self.loop is not None and not self.loop.is_closed():
                    asyncio.run_coroutine_threadsafe(
                        self.bus.publish(_ProfileUpdates(frozenset(changed))), self.loop
                    )
