"""The executable contract every persistence backend has to meet.

`docs/PERSISTENCE_BACKENDS.md` describes the seam in prose; this module is
what a backend is actually held to. Subclass `PersistenceBackendContract` in
a test module, provide a `factory` fixture, and every test here runs against
your backend. Three implementations are held to it in this repo: the file
backend, the in-memory reference backend below, and the optional PostgreSQL
backend, which is developed against exactly this class and meets everything
here but the change-notification clauses.

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
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Sequence

import pytest

from backend.core.models import Edge, Node, NodeType
from backend.core.storage import GraphStorage
from backend.core.storage_backends import (
    BackendCapabilities,
    ChangeNotifyingBackend,
    EntityOperation,
    ExternalChange,
    IncrementalGraphPersistenceBackend,
    capabilities_of,
)

# --- payload helpers ------------------------------------------------------


def node_payload(node_id: str, **overrides: Any) -> Dict[str, Any]:
    """A node exactly as GraphStorage serialises one for a backend.

    `updated_at` is stamped now, because a payload standing in for another
    instance's write has to look like one: the refresh resolves a write to a
    node both instances touched by last-writer-wins, and a fixed stamp in the
    past would make every such payload the loser.
    """
    now = datetime.now(timezone.utc).isoformat()
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
        "updated_at": now,
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
        store.setdefault("listeners", {})
        store.setdefault("dispatched", [])
        store.setdefault("written", False)

    def capabilities(self) -> BackendCapabilities:
        if not self._incremental:
            return BackendCapabilities()
        return BackendCapabilities(
            incremental_writes=True,
            transactions=True,
            change_notification=True,
        )

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
        # A whole-graph write replaced everything; naming what changed would
        # mean diffing it, which is the case ExternalChange.unknown() is for.
        self._notify_others(ExternalChange.unknown())

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
        self._notify_others(ExternalChange.entities(operations))

    def checkpoint(self) -> None:
        pass  # nothing is deferred

    # -- change notification --------------------------------------------------

    def start_change_notification(self, listener) -> None:
        with self._lock:
            self._store["listeners"][id(self)] = listener

    def stop_change_notification(self) -> None:
        notifier = None
        with self._lock:
            self._store["listeners"].pop(id(self), None)
            if not self._store["listeners"]:
                notifier = self._store.pop("notifier", None)
        # Outside the lock, and after the last listener is gone: a delivery
        # still in flight refreshes an application that reads this backend
        # straight back, and would take this lock to do it.
        if notifier is not None:
            notifier.shutdown(wait=True)

    def settle_notifications(self) -> None:
        """Wait for every report dispatched so far to have been delivered."""
        with self._lock:
            notifier = self._store.get("notifier")
            pending, self._store["dispatched"] = self._store["dispatched"], []
        if notifier is not None:
            notifier.submit(lambda: None).result(timeout=30)
        for future in pending:
            future.result(timeout=30)

    def _notify_others(self, change: ExternalChange) -> None:
        """Report a write to every instance on this store except the writer.

        The writer's own application already has the change; telling it would
        re-apply its own work and emit a second event for it.

        Delivered on a thread of this store's own, never on the thread that
        made the write - the rule ChangeNotifyingBackend states. A listener
        refreshes an application that may wait for its own write queue, so a
        report handed over inside a write would have that queue wait for the
        write it is running, and two instances reporting into each other that
        way would wait for each other for good. One worker, so reports are
        delivered in the order the writes happened.
        """
        with self._lock:
            if not [key for key in self._store["listeners"] if key != id(self)]:
                return  # nobody to tell: do not start a thread to say nothing
            notifier = self._store.get("notifier")
            if notifier is None:
                notifier = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="in-memory-notify"
                )
                self._store["notifier"] = notifier
            future = notifier.submit(self._deliver, id(self), change)
            self._store["dispatched"].append(future)

    def _deliver(self, writer_key: int, change: ExternalChange) -> None:
        """Call the listeners registered *now*, on the notifier thread.

        Read at delivery rather than at dispatch, so an instance that has
        since shut down is not refreshed after the fact.
        """
        with self._lock:
            others = [
                listener
                for key, listener in self._store["listeners"].items()
                if key != writer_key
            ]
        for listener in others:
            listener(change)


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

    def settle_notifications(self, backend) -> None:
        """Wait for the reports `backend` has dispatched to be delivered.

        A backend reports from a thread of its own (ChangeNotifyingBackend),
        so a clause that writes through one instance and then reads another
        has to wait for that thread first. Assume you must override this:
        the default does nothing, and it only suits a backend that has
        somehow finished delivering before the write returns. Handing the
        report to another thread and joining it inside the write is not that
        - it is the shape *Which thread reports* rules out, because two
        instances doing it wait for each other for good.
        """

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _notifying(backend) -> bool:
        return capabilities_of(backend).change_notification

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

    def test_a_change_notification_declaration_is_backed_by_the_protocol(self, factory):
        backend = factory()
        if not self._notifying(backend):
            pytest.skip("backend does not report external changes")
        assert isinstance(backend, ChangeNotifyingBackend)

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
        """Every part of it, metadata included.

        Metadata is the third a store can quietly get wrong: a backend that
        replaces the entities but only *inserts* the metadata leaves the
        first save's version and graph name behind for the life of the
        store, and every later save is a partial one. Round-tripping a
        single save cannot tell, because with one save an insert and a
        replace agree.
        """
        backend = factory()
        first = snapshot([node_payload("a"), node_payload("b")])
        first["metadata"] = {"version": "1.0", "graph_name": "before"}
        backend.save_graph_data(first)

        second = snapshot([node_payload("c")])
        second["metadata"] = {"version": "2.0", "graph_name": "after"}
        backend.save_graph_data(second)

        reloaded = factory().load_graph_data()
        assert set(by_id(reloaded, "nodes")) == {"c"}
        assert reloaded["metadata"]["graph_name"] == "after"
        assert reloaded["metadata"]["version"] == "2.0"

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

    # -- change notification --------------------------------------------------

    def _running_storage_and_writer(self, factory):
        """A storage on one backend, plus a second backend on the same store
        standing in for the other instance."""
        storage = GraphStorage(persistence_backend=factory())
        storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="Alpha")], [])
        storage.flush()
        return storage, factory()

    def test_an_external_write_reaches_a_running_storage(self, factory):
        """What this seam exists for: a write another instance makes to the
        same store shows up in a running instance's reads with no restart -
        in the structures behind search too, not just the node dictionary."""
        if not self._notifying(factory()):
            pytest.skip("backend does not report external changes")
        storage, elsewhere = self._running_storage_and_writer(factory)
        try:
            elsewhere.apply_batch(
                [
                    EntityOperation.upsert_node(
                        node_payload("b", name="Beacon", embedding=[0.5, 0.25])
                    ),
                    EntityOperation.upsert_edge(edge_payload("ab", "a", "b")),
                ]
            )
            self.settle_notifications(elsewhere)

            assert storage.get_node("b").name == "Beacon"
            assert {e.id for e in storage.get_all_edges()} == {"ab"}
            # Lexical search reads a per-node cache; a stale entry is served.
            assert [n.id for n in storage.search_nodes("Beacon")] == ["b"]
            # And the vector index, which semantic search reads.
            assert storage.vector_store.get_vector_list("b") == pytest.approx(
                [0.5, 0.25]
            )
        finally:
            storage.shutdown_events()

    def test_an_external_update_replaces_what_search_had(self, factory):
        """An update is where a stale cache shows: the old text must stop
        matching, and the vector that described it must not survive it."""
        if not self._notifying(factory()):
            pytest.skip("backend does not report external changes")
        storage, elsewhere = self._running_storage_and_writer(factory)
        try:
            elsewhere.apply_batch(
                [
                    EntityOperation.upsert_node(
                        node_payload("a", name="Renamed", embedding=[1.0, 0.0])
                    )
                ]
            )
            self.settle_notifications(elsewhere)

            assert storage.get_node("a").name == "Renamed"
            assert storage.search_nodes("Alpha") == []
            assert [n.id for n in storage.search_nodes("Renamed")] == ["a"]
            assert storage.vector_store.get_vector_list("a") == pytest.approx(
                [1.0, 0.0]
            )
            # The graph hands out node objects of its own; a refresh that
            # repointed only the dictionary would leave the old one here.
            assert storage.graph.nodes["a"]["data"].name == "Renamed"
        finally:
            storage.shutdown_events()

    def test_an_external_update_does_not_leave_the_old_vector_behind(self, factory):
        """A vector describes the text a node had. When the store hands back
        new text and no vector, the old one is not merely unhelpful - semantic
        search would go on matching a description this node no longer has."""
        if not self._notifying(factory()):
            pytest.skip("backend does not report external changes")
        storage = GraphStorage(persistence_backend=factory())
        try:
            storage.add_nodes(
                [
                    Node(
                        id="a",
                        type=NodeType.ACTOR,
                        name="Alpha",
                        embedding=[0.5, 0.25],
                    )
                ],
                [],
            )
            storage.flush()
            assert storage.vector_store.get_vector_list("a") == pytest.approx(
                [0.5, 0.25]
            )

            elsewhere = factory()
            elsewhere.apply_batch(
                [EntityOperation.upsert_node(node_payload("a", name="Renamed"))]
            )
            self.settle_notifications(elsewhere)

            # Either regenerated from the new text, or gone until something
            # regenerates it. Never still the vector for "Alpha".
            current = storage.vector_store.get_vector_list("a")
            assert current is None or current != pytest.approx([0.5, 0.25])
        finally:
            storage.shutdown_events()

    def test_an_external_delete_reaches_a_running_storage(self, factory):
        if not self._notifying(factory()):
            pytest.skip("backend does not report external changes")
        storage, elsewhere = self._running_storage_and_writer(factory)
        try:
            elsewhere.apply_batch(
                [
                    EntityOperation.upsert_node(
                        node_payload("b", name="Beacon", embedding=[0.5, 0.25])
                    ),
                    EntityOperation.upsert_edge(edge_payload("ab", "a", "b")),
                ]
            )
            elsewhere.apply_batch([EntityOperation.delete_node("b")])
            self.settle_notifications(elsewhere)

            assert storage.get_node("b") is None
            assert storage.search_nodes("Beacon") == []
            assert storage.vector_store.get_vector_list("b") is None
            # An edge cannot outlive an endpoint, however the store reported it.
            assert storage.get_all_edges() == []
            assert not storage.graph.has_node("b")
        finally:
            storage.shutdown_events()

    def test_a_change_the_backend_cannot_describe_reloads_the_graph(self, factory):
        """A backend that knows only that something changed says so, and the
        application reloads rather than going on serving a stale graph."""
        if not self._notifying(factory()):
            pytest.skip("backend does not report external changes")
        storage, elsewhere = self._running_storage_and_writer(factory)
        try:
            elsewhere.save_graph_data(snapshot([node_payload("z", name="Zulu")]))
            self.settle_notifications(elsewhere)

            assert {n.id for n in storage.get_all_nodes()} == {"z"}
            assert [n.id for n in storage.search_nodes("Zulu")] == ["z"]
        finally:
            storage.shutdown_events()

    def test_two_storages_writing_one_store_do_not_wait_on_each_other(self, factory):
        """The shape this seam exists for, and the one that breaks when a
        report is handed over inside a write: two instances writing the same
        store at once. Each whole-graph write is reported to the other, whose
        refresh waits for its own write queue - which must never be the queue
        that is waiting for that report to return. Content is not asserted;
        two whole-graph writers overwrite each other by design. That they
        both finish is the property."""
        if not self._notifying(factory()):
            pytest.skip("backend does not report external changes")
        first, second = factory(), factory()
        one = GraphStorage(persistence_backend=first)
        two = GraphStorage(persistence_backend=second)
        errors: List[Exception] = []

        def hammer(storage, prefix):
            try:
                for i in range(5):
                    storage.add_nodes(
                        [Node(id=f"{prefix}{i}", type=NodeType.ACTOR, name=prefix)],
                        [],
                    )
                    storage.save().result(timeout=60)
            except Exception as exc:  # reported, not raised off-thread
                errors.append(exc)

        threads = [
            threading.Thread(target=hammer, args=(one, "a"), daemon=True),
            threading.Thread(target=hammer, args=(two, "b"), daemon=True),
        ]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(120)
            assert not [t for t in threads if t.is_alive()], (
                "two instances writing one store waited on each other"
            )
            assert errors == []
        finally:
            one.shutdown_events()
            two.shutdown_events()

    def test_notification_stops_when_the_storage_shuts_down(self, factory):
        """A torn-down storage must not still be refreshed: its executor is
        gone, and what it holds is nobody's view any more."""
        if not self._notifying(factory()):
            pytest.skip("backend does not report external changes")
        storage, elsewhere = self._running_storage_and_writer(factory)
        storage.shutdown_events()

        elsewhere.apply_batch(
            [EntityOperation.upsert_node(node_payload("q", name="Quiet"))]
        )
        self.settle_notifications(elsewhere)
        assert storage.get_node("q") is None


__all__ = [
    "PersistenceBackendContract",
    "InMemoryGraphPersistenceBackend",
    "node_payload",
    "edge_payload",
    "snapshot",
    "by_id",
]
