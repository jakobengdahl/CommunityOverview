"""Deterministic, access-filtered statistics for the visible local graph."""

from collections import Counter, defaultdict

from backend.runtime.authorization import GRAPH_ACTION_READ, GraphAccessNarrowing
from . import access

# Bound every collection in the response, including type and subtype counts.
MAX_ITEMS = 100


def profile(storage, graph_access):
    nodes, edges = access.filter_nodes_and_edges(
        nodes=[n for n in storage.get_all_nodes() if not n.archived],
        edges=[e for e in storage.get_all_edges() if not e.archived],
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
            {"type": k, "count": v} for k, v in sorted(types.items())[:MAX_ITEMS]
        ],
        "subtypes": [
            {"type": k[0], "subtype": k[1], "count": v}
            for k, v in sorted(subtypes.items())[:MAX_ITEMS]
        ],
        "tags": [
            {"tag": k, "count": v}
            for k, v in sorted(tags.items(), key=lambda item: (-item[1], item[0]))[
                :MAX_ITEMS
            ]
        ],
        "relationships": [
            {
                "source_type": k[0],
                "relationship_type": k[1],
                "target_type": k[2],
                "source_count": len(v),
                "share": len(v) / types[k[0]],
            }
            for k, v in sorted(triples.items())[:MAX_ITEMS]
        ],
        "central_nodes": [
            {"id": n.id, "name": n.name, "type": n.type_str, "degree": degree[n.id]}
            for n in sorted(nodes, key=lambda n: (-degree[n.id], n.id))[:MAX_ITEMS]
        ],
    }


def describe(storage, hook, publisher=None):
    decision = access.evaluate_graph_access(
        hook, action=GRAPH_ACTION_READ, target="describe_graph"
    )
    if not decision.allowed:
        return access.build_access_denied_result(
            action=GRAPH_ACTION_READ, target="describe_graph", decision=decision
        )
    result = profile(storage, decision.graph_access)
    if publisher is not None and storage is publisher.storage:
        publisher.observe(decision.graph_access, result)
    return result


class ProfileChangePublisher:
    """Compare the public shape after each graph event and notify on change."""

    def __init__(self, storage, bus):
        self.storage, self.bus = storage, bus
        self.previous = profile(storage, GraphAccessNarrowing())
        self.loop = None
        self.observed = {}
        from threading import RLock

        self.lock = RLock()

    def observe(self, graph_access, result):
        with self.lock:
            self.observed[graph_access] = result

    def on_event(self, event):
        import asyncio
        from mcp.shared.subscriptions import ResourceUpdated

        with self.lock:
            current = profile(self.storage, GraphAccessNarrowing())
            changed = current != self.previous
            self.previous = current
            for scope, previous in self.observed.items():
                updated = profile(self.storage, scope)
                changed = changed or updated != previous
                self.observed[scope] = updated
            if changed and self.loop is not None and not self.loop.is_closed():
                asyncio.run_coroutine_threadsafe(
                    self.bus.publish(ResourceUpdated(uri="graph://profile")), self.loop
                )
