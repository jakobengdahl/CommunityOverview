"""The executable contract every persistence backend has to meet.

`docs/PERSISTENCE_BACKENDS.md` describes the seam in prose; this module is
what a backend is actually held to. Subclass `PersistenceBackendContract` in
a test module, provide a `factory` fixture, and every test here runs against
your backend. The file backend and the in-memory reference backend below are
the two implementations shipped with the repo; a future SQL or SQLite backend
is developed against exactly this class.

What a subclass provides:

- `factory` (fixture): a zero-argument callable that returns a backend bound
  to ONE store, fresh for the test. Calling it again returns a new instance
  on the SAME store - that is how the contract checks what actually landed,
  rather than what an instance remembers.
- `interrupt_next_snapshot(backend, monkeypatch)` (optional): make the next
  whole-graph write fail part-way, as a crash or a full disk would. Without
  it the interrupted-snapshot test is skipped.
- `interrupt_next_append(backend, monkeypatch)` (optional, incremental
  backends): make the next entity write fail after it has started. Without
  it the interrupted-batch test is skipped.
- `previous_version_store(tmp_path)` (optional): a store as the previous
  release wrote it, returned as `(factory, node_ids, ids_with_vectors)` -
  the factory that opens it, every node id it holds, and the ids whose
  entry carries a vector. Without it the backwards-compatibility test is
  skipped.
"""

from __future__ import annotations

import copy
import threading
from typing import Any, Dict, List, Sequence

import pytest

from backend.core.models import Edge, Node, NodeType
from backend.core.storage import GraphStorage
from backend.core.storage_backends import (
    BackendCapabilities,
    EntityOperation,
    IncrementalGraphPersistenceBackend,
    capabilities_of,
)

# --- payload helpers ------------------------------------------------------


def node_payload(node_id: str, **overrides: Any) -> Dict[str, Any]:
    """A node exactly as GraphStorage serialises one for a backend."""
    payload = {
        "id": node_id,
        "type": "Actor",
        "name": node_id.upper(),
        "description": "",
        "summary": "",
        "tags": [],
        "subtypes": [],
        "aliases": [],
        "metadata": {},
        "archived": False,
        "created_at": "2026-09-05T00:00:00+00:00",
        "updated_at": "2026-09-05T00:00:00+00:00",
    }
    payload.update(overrides)
    return payload


def edge_payload(edge_id: str, source: str, target: str, **overrides: Any):
    payload = {
        "id": edge_id,
        "source": source,
        "target": target,
        "type": "RELATES_TO",
        "label": "",
        "metadata": {},
        "archived": False,
        "created_at": "2026-09-05T00:00:00+00:00",
    }
    payload.update(overrides)
    return payload


def snapshot(
    nodes: Sequence[Dict[str, Any]] = (), edges: Sequence[Dict[str, Any]] = ()
):
    return {
        "nodes": list(nodes),
        "edges": list(edges),
        "metadata": {"version": "1.0", "graph_name": "contract"},
    }


def by_id(data: Dict[str, Any], key: str) -> Dict[str, Dict[str, Any]]:
    return {entity.get("id"): entity for entity in data.get(key, [])}


# --- the reference in-memory backend ---------------------------------------


class InMemoryGraphPersistenceBackend:
    """The reference implementation of the incremental contract.

    A dict-backed store shared by every instance created from the same
    `store` dict, so a "reopened" instance sees what another one wrote; the
    lock lives in the store too, so it is one lock across those instances.
    Both writes are atomic the simplest way there is: the new state is built
    first and swapped in at the end, so a failure part-way - in a copy, say -
    leaves the store exactly as it was.
    """

    def __init__(self, store: Dict[str, Any], *, incremental: bool = True):
        self._store = store
        self._incremental = incremental
        self._lock = store.setdefault("lock", threading.Lock())
        store.setdefault("nodes", {})
        store.setdefault("edges", {})
        store.setdefault("metadata", {})
        store.setdefault("written", False)

    def capabilities(self) -> BackendCapabilities:
        if not self._incremental:
            return BackendCapabilities()
        return BackendCapabilities(incremental_writes=True, transactions=True)

    def exists(self) -> bool:
        return self._store["written"]

    def load_graph_data(self) -> Dict[str, Any]:
        with self._lock:
            return copy.deepcopy(
                {
                    "nodes": list(self._store["nodes"].values()),
                    "edges": list(self._store["edges"].values()),
                    "metadata": self._store["metadata"],
                }
            )

    def save_graph_data(self, data: Dict[str, Any]) -> None:
        with self._lock:
            nodes = {n["id"]: copy.deepcopy(n) for n in data["nodes"]}
            edges = {e["id"]: copy.deepcopy(e) for e in data["edges"]}
            metadata = copy.deepcopy(data.get("metadata") or {})
            self._store.update(
                nodes=nodes, edges=edges, metadata=metadata, written=True
            )

    def default_graph_name(self) -> str:
        return "in-memory"

    def upsert_node(self, node: Dict[str, Any]) -> None:
        self.apply_batch([EntityOperation.upsert_node(node)])

    def delete_node(self, node_id: str) -> None:
        self.apply_batch([EntityOperation.delete_node(node_id)])

    def upsert_edge(self, edge: Dict[str, Any]) -> None:
        self.apply_batch([EntityOperation.upsert_edge(edge)])

    def delete_edge(self, edge_id: str) -> None:
        self.apply_batch([EntityOperation.delete_edge(edge_id)])

    def apply_batch(self, operations: Sequence[EntityOperation]) -> None:
        with self._lock:
            nodes = dict(self._store["nodes"])
            edges = dict(self._store["edges"])
            for op in operations:
                target = nodes if op.kind == "node" else edges
                if op.action == "upsert":
                    target[op.entity_id] = copy.deepcopy(op.payload)
                else:
                    target.pop(op.entity_id, None)
            self._store["nodes"] = nodes
            self._store["edges"] = edges
            self._store["written"] = True

    def checkpoint(self) -> None:
        pass  # nothing is deferred


# --- the contract ----------------------------------------------------------


class PersistenceBackendContract:
    """Subclass this and provide a `factory` fixture; see the module docstring."""

    # -- hooks a subclass may override --------------------------------------

    def interrupt_next_snapshot(self, backend, monkeypatch) -> None:
        pytest.skip("this backend has no way to interrupt a snapshot under test")

    def interrupt_next_append(self, backend, monkeypatch) -> None:
        pytest.skip("this backend has no way to interrupt an append under test")

    def previous_version_store(self, tmp_path):
        pytest.skip("no previous-version store defined for this backend")

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _incremental(backend) -> bool:
        return capabilities_of(backend).incremental_writes

    def _require_incremental(self, backend) -> None:
        if not self._incremental(backend):
            pytest.skip("snapshot-only backend: the entity contract does not apply")

    # -- the declaration ------------------------------------------------------

    def test_the_declaration_is_a_backend_capabilities(self, factory):
        backend = factory()
        assert isinstance(backend.capabilities(), BackendCapabilities)
        # capabilities_of is what GraphStorage consults; it must accept the backend.
        assert capabilities_of(backend) == backend.capabilities()

    def test_an_incremental_declaration_is_backed_by_the_six_methods(self, factory):
        backend = factory()
        if not self._incremental(backend):
            pytest.skip("snapshot-only backend")
        assert isinstance(backend, IncrementalGraphPersistenceBackend)

    def test_change_notification_is_not_declared_before_its_seam_exists(self, factory):
        """The notification contract is a later change to the seam; until it
        lands nothing can honour a declaration, so no backend may make one."""
        assert not capabilities_of(factory()).change_notification

    # -- the snapshot contract ------------------------------------------------

    def test_a_fresh_store_does_not_exist_until_something_is_saved(self, factory):
        backend = factory()
        assert not backend.exists()
        backend.save_graph_data(snapshot())
        assert backend.exists()
        assert factory().exists()

    def test_a_snapshot_round_trips_through_a_reopened_backend(self, factory):
        backend = factory()
        data = snapshot(
            [node_payload("a", tags=["x"], metadata={"k": {"nested": [1, 2]}})],
            [edge_payload("e", "a", "a", label="self")],
        )

        backend.save_graph_data(data)

        for instance in (backend, factory()):
            loaded = instance.load_graph_data()
            assert by_id(loaded, "nodes") == by_id(data, "nodes")
            assert by_id(loaded, "edges") == by_id(data, "edges")
            assert loaded["metadata"]["graph_name"] == "contract"

    def test_a_snapshot_replaces_the_previous_graph_whole(self, factory):
        backend = factory()
        backend.save_graph_data(snapshot([node_payload("a"), node_payload("b")]))

        backend.save_graph_data(snapshot([node_payload("c")]))

        assert set(by_id(factory().load_graph_data(), "nodes")) == {"c"}

    def test_the_loaded_dict_is_the_callers_to_mutate(self, factory):
        """GraphStorage rewrites what it loads in place (timestamps become
        datetimes); that must not reach the store."""
        backend = factory()
        backend.save_graph_data(snapshot([node_payload("a")]))

        loaded = backend.load_graph_data()
        loaded["nodes"][0]["name"] = "mutated"
        loaded["nodes"][0]["created_at"] = object()

        assert by_id(backend.load_graph_data(), "nodes")["a"]["name"] == "A"
        assert by_id(factory().load_graph_data(), "nodes")["a"]["name"] == "A"

    # -- the entity contract --------------------------------------------------

    def test_an_upsert_creates_and_a_second_upsert_replaces_whole(self, factory):
        backend = factory()
        self._require_incremental(backend)
        backend.save_graph_data(snapshot())

        # The first payload carries a key the replacement will not: exactly
        # what happens when a node's vector moves out of graph.json into the
        # sidecar and the `embedding` key stops being written.
        backend.upsert_node(node_payload("a", tags=["first"], embedding=[1.0]))
        replacement = node_payload("a", name="Replaced")
        backend.upsert_edge(edge_payload("e", "a", "a"))
        backend.upsert_node(replacement)

        nodes = by_id(factory().load_graph_data(), "nodes")
        assert nodes["a"] == replacement
        assert "embedding" not in nodes["a"]  # replaced whole, not patched
        assert by_id(factory().load_graph_data(), "edges")["e"]["source"] == "a"

    def test_a_delete_removes_and_deleting_the_absent_is_not_an_error(self, factory):
        backend = factory()
        self._require_incremental(backend)
        backend.save_graph_data(
            snapshot(
                [node_payload("a"), node_payload("b")], [edge_payload("e", "a", "b")]
            )
        )

        backend.delete_edge("e")
        backend.delete_node("a")
        backend.delete_node("never-there")
        backend.delete_edge("never-there")

        loaded = factory().load_graph_data()
        assert set(by_id(loaded, "nodes")) == {"b"}
        assert by_id(loaded, "edges") == {}

    def test_a_batch_applies_in_order_and_lands_whole(self, factory):
        backend = factory()
        self._require_incremental(backend)
        backend.save_graph_data(snapshot([node_payload("gone")]))

        backend.apply_batch(
            [
                EntityOperation.upsert_node(node_payload("a", name="first")),
                EntityOperation.upsert_node(node_payload("a", name="second")),
                EntityOperation.upsert_node(node_payload("b")),
                EntityOperation.upsert_edge(edge_payload("e", "a", "b")),
                EntityOperation.delete_node("gone"),
                EntityOperation.upsert_node(node_payload("tmp")),
                EntityOperation.delete_node("tmp"),
            ]
        )

        loaded = factory().load_graph_data()
        nodes = by_id(loaded, "nodes")
        assert set(nodes) == {"a", "b"}
        assert nodes["a"]["name"] == "second"
        assert set(by_id(loaded, "edges")) == {"e"}

    def test_the_stored_payload_is_a_copy(self, factory):
        backend = factory()
        self._require_incremental(backend)
        backend.save_graph_data(snapshot())
        payload = node_payload("a")

        backend.upsert_node(payload)
        payload["name"] = "mutated afterwards"

        assert by_id(factory().load_graph_data(), "nodes")["a"]["name"] == "A"

    def test_entity_writes_survive_a_checkpoint_and_a_reopen(self, factory):
        backend = factory()
        self._require_incremental(backend)
        backend.save_graph_data(snapshot([node_payload("a")]))
        backend.upsert_node(node_payload("b"))
        backend.delete_node("a")

        backend.checkpoint()
        backend.checkpoint()  # nothing pending: harmless

        assert set(by_id(factory().load_graph_data(), "nodes")) == {"b"}

    def test_a_snapshot_after_entity_writes_wins(self, factory):
        """A whole-graph save is the whole graph: nothing an earlier entity
        write put in the store may survive it."""
        backend = factory()
        self._require_incremental(backend)
        backend.save_graph_data(snapshot())
        backend.upsert_node(node_payload("stale"))

        backend.save_graph_data(snapshot([node_payload("fresh")]))

        assert set(by_id(factory().load_graph_data(), "nodes")) == {"fresh"}

    def test_a_declared_atomic_batch_lands_entirely_or_not_at_all(
        self, factory, monkeypatch
    ):
        backend = factory()
        self._require_incremental(backend)
        if not capabilities_of(backend).transactions:
            pytest.skip("this backend does not declare transactions")
        backend.save_graph_data(snapshot([node_payload("a")]))

        self.interrupt_next_append(backend, monkeypatch)
        with pytest.raises(Exception):
            backend.apply_batch(
                [
                    EntityOperation.delete_node("a"),
                    EntityOperation.upsert_node(node_payload("b")),
                ]
            )

        loaded = factory().load_graph_data()
        assert set(by_id(loaded, "nodes")) == {"a"}

    # -- failure behaviour ----------------------------------------------------

    def test_an_interrupted_snapshot_leaves_the_previous_graph_readable(
        self, factory, monkeypatch
    ):
        backend = factory()
        before = snapshot([node_payload("a"), node_payload("b")])
        backend.save_graph_data(before)

        self.interrupt_next_snapshot(backend, monkeypatch)
        with pytest.raises(Exception):
            backend.save_graph_data(snapshot([node_payload("c")]))

        loaded = factory().load_graph_data()
        assert by_id(loaded, "nodes") == by_id(before, "nodes")

    # -- backwards compatibility ---------------------------------------------

    def test_a_store_written_by_the_previous_version_loads(self, tmp_path):
        """Whole, through the backend and through GraphStorage: every node the
        old store held is there, and a vector it carried is in the index."""
        open_previous, node_ids, ids_with_vectors = self.previous_version_store(
            tmp_path
        )
        backend = open_previous()

        assert backend.exists()
        assert set(by_id(backend.load_graph_data(), "nodes")) == set(node_ids)

        storage = GraphStorage(persistence_backend=open_previous())
        try:
            assert {n.id for n in storage.get_all_nodes()} == set(node_ids)
            for node_id in ids_with_vectors:
                assert storage.vector_store.get_vector_list(node_id) is not None
        finally:
            storage.shutdown_events()

    # -- through GraphStorage -------------------------------------------------

    def test_graph_storage_round_trips_a_graph_through_a_reopened_backend(
        self, factory
    ):
        """The whole seam, as the app drives it: mutations of every kind, then
        a second storage on a reopened backend sees the same graph - including
        a vector, whether the backend keeps it inline or in a sidecar."""
        storage = GraphStorage(persistence_backend=factory())
        try:
            storage.add_nodes(
                [
                    Node(id="a", type=NodeType.ACTOR, name="A", embedding=[0.5, 0.25]),
                    Node(id="b", type=NodeType.ACTOR, name="B"),
                    Node(id="c", type=NodeType.INITIATIVE, name="C"),
                ],
                [
                    Edge(id="ab", source="a", target="b"),
                    Edge(id="bc", source="b", target="c"),
                ],
            )
            storage.update_node("b", {"name": "Bee", "tags": ["t"]})
            storage.update_edge("ab", {"label": "knows"})
            storage.set_nodes_archived(["c"], True)
            storage.delete_edges(["bc"])
            storage.add_edge(Edge(id="ca", source="c", target="a"))
            storage.delete_nodes(["c"], confirmed=True)
            storage.flush()
        finally:
            storage.shutdown_events()

        reopened = GraphStorage(persistence_backend=factory())
        try:
            assert {n.id: n.name for n in reopened.get_all_nodes()} == {
                "a": "A",
                "b": "Bee",
            }
            assert reopened.get_node("b").tags == ["t"]
            edges = {e.id: e for e in reopened.get_all_edges()}
            assert set(edges) == {"ab"} and edges["ab"].label == "knows"
            assert reopened.vector_store.get_vector_list("a") == pytest.approx(
                [0.5, 0.25]
            )
        finally:
            reopened.shutdown_events()

    def test_graph_storage_drives_the_backend_by_its_declaration(self, factory):
        """Honesty: a backend declaring incremental writes gets an entity write
        for a single-node update; one that does not gets a snapshot. Either
        way the change is in the store afterwards."""
        backend = factory()
        incremental = self._incremental(backend)
        calls: List[str] = []
        real_save = backend.save_graph_data
        backend.save_graph_data = lambda data: (
            calls.append("snapshot"),
            real_save(data),
        )
        if incremental:
            real_upsert = backend.upsert_node
            backend.upsert_node = lambda node: (
                calls.append("upsert"),
                real_upsert(node),
            )

        storage = GraphStorage(persistence_backend=backend)
        try:
            storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="A")], [])
            storage.flush()
            calls.clear()
            storage.update_node("a", {"name": "Renamed"})
            storage.flush()
        finally:
            storage.shutdown_events()

        assert calls[0] == ("upsert" if incremental else "snapshot")
        assert by_id(factory().load_graph_data(), "nodes")["a"]["name"] == "Renamed"


__all__ = [
    "PersistenceBackendContract",
    "InMemoryGraphPersistenceBackend",
    "node_payload",
    "edge_payload",
    "snapshot",
    "by_id",
]
