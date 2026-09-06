"""The persistence seam's entity contract, driven through GraphStorage.

Every backend here is a fake. What is under test is which shape GraphStorage
hands a backend for each mutation - decided by the capabilities the backend
declares, with the file backend's sidecar as the one type-bound exception -
and that the shape is one a backend can store and a later load can rebuild
the graph from.
"""

import json
import os
import tempfile
import threading

import pytest

from backend.core.events.models import EventType
from backend.core.models import Edge, Node, NodeType
from backend.core.storage import EXTERNAL_CHANGE_ORIGIN, GraphStorage
from backend.core.storage_backends import (
    SNAPSHOT_ONLY,
    BackendCapabilities,
    EntityOperation,
    ExternalChange,
    FileGraphPersistenceBackend,
    GraphPersistenceBackend,
    IncrementalGraphPersistenceBackend,
    capabilities_of,
)


def _node_payload(node_id: str, name: str):
    """A node as GraphStorage serialises one, for a store to hand back."""
    return {
        "id": node_id,
        "type": "Actor",
        "name": name,
        "description": "",
        "summary": "",
        "tags": [],
        "subtypes": [],
        "aliases": [],
        "metadata": {},
        "archived": False,
        "created_at": "2026-09-06T00:00:00+00:00",
        "updated_at": "2026-09-06T00:00:00+00:00",
    }


def _edge_payload(edge_id: str, source: str, target: str):
    return {
        "id": edge_id,
        "source": source,
        "target": target,
        "type": "RELATES_TO",
        "label": "",
        "metadata": {},
        "archived": False,
        "created_at": "2026-09-06T00:00:00+00:00",
    }


class _SnapshotBackend:
    """The pre-contract shape: four methods and no capability declaration."""

    def __init__(self):
        self.data = None
        self.snapshots = 0

    def exists(self):
        return self.data is not None

    def load_graph_data(self):
        return json.loads(json.dumps(self.data))

    def save_graph_data(self, data):
        self.snapshots += 1
        self.data = json.loads(json.dumps(data))

    def default_graph_name(self):
        return "snapshot"


class _IncrementalBackend:
    """Records every write it is handed, in order, and keeps a live store so a
    later load sees what those writes built."""

    def __init__(self, transactions=True):
        self._transactions = transactions
        self.calls = []
        self.snapshots = 0
        self.checkpoints = 0
        self.nodes = {}
        self.edges = {}
        self.metadata = {}
        self.written = False

    def capabilities(self):
        return BackendCapabilities(
            incremental_writes=True, transactions=self._transactions
        )

    def exists(self):
        return self.written

    def load_graph_data(self):
        return json.loads(
            json.dumps(
                {
                    "nodes": list(self.nodes.values()),
                    "edges": list(self.edges.values()),
                    "metadata": self.metadata,
                }
            )
        )

    def save_graph_data(self, data):
        self.calls.append(("snapshot", None))
        self.snapshots += 1
        self.nodes = {n["id"]: n for n in data["nodes"]}
        self.edges = {e["id"]: e for e in data["edges"]}
        self.metadata = data["metadata"]
        self.written = True

    def default_graph_name(self):
        return "incremental"

    def upsert_node(self, node):
        self.calls.append(("upsert_node", node))
        self._apply(EntityOperation.upsert_node(node))

    def delete_node(self, node_id):
        self.calls.append(("delete_node", node_id))
        self._apply(EntityOperation.delete_node(node_id))

    def upsert_edge(self, edge):
        self.calls.append(("upsert_edge", edge))
        self._apply(EntityOperation.upsert_edge(edge))

    def delete_edge(self, edge_id):
        self.calls.append(("delete_edge", edge_id))
        self._apply(EntityOperation.delete_edge(edge_id))

    def apply_batch(self, operations):
        self.calls.append(("apply_batch", tuple(operations)))
        for op in operations:
            self._apply(op)

    def checkpoint(self):
        # Not a write: flush() asks for it on every call, and the assertions
        # below are about what the mutations sent.
        self.checkpoints += 1

    def _apply(self, op):
        store = self.nodes if op.kind == "node" else self.edges
        if op.action == "upsert":
            store[op.entity_id] = json.loads(json.dumps(op.payload))
        else:
            store.pop(op.entity_id, None)


def _storage(backend):
    """A storage on the backend, with the bootstrap write already forgotten."""
    storage = GraphStorage(persistence_backend=backend)
    storage.flush()
    if hasattr(backend, "calls"):
        backend.calls.clear()
    backend.snapshots = 0
    return storage


def _node(node_id, name=None):
    return Node(id=node_id, type=NodeType.ACTOR, name=name or node_id.upper())


def _edge(edge_id, source, target):
    return Edge(id=edge_id, source=source, target=target)


def _kinds(backend):
    return [name for name, _ in backend.calls]


class _NotifyingBackend(_IncrementalBackend):
    """An incremental backend that also reports what someone else wrote.

    `on_subscribe` lets a test deliver a change at the moment the listener is
    registered, which is how the wiring's ordering is pinned.
    """

    def __init__(self, on_subscribe=None, **kwargs):
        super().__init__(**kwargs)
        self.listener = None
        self.subscribes = 0
        self.unsubscribes = 0
        self._on_subscribe = on_subscribe
        # When set, every write also reports one - from inside the write,
        # which is the application's own writer thread.
        self.report_from_writes = None

    def capabilities(self):
        return BackendCapabilities(
            incremental_writes=True, transactions=True, change_notification=True
        )

    def start_change_notification(self, listener):
        self.listener = listener
        self.subscribes += 1
        if self._on_subscribe is not None:
            self._on_subscribe(listener)

    def stop_change_notification(self):
        self.listener = None
        self.unsubscribes += 1

    def _report_from_write(self):
        if self.report_from_writes is not None and self.listener is not None:
            self.listener(self.report_from_writes)

    def upsert_node(self, node):
        super().upsert_node(node)
        self._report_from_write()

    def apply_batch(self, operations):
        super().apply_batch(operations)
        self._report_from_write()


class TestCapabilityDeclaration:
    def test_a_backend_without_a_declaration_is_snapshot_only(self):
        assert capabilities_of(_SnapshotBackend()) is SNAPSHOT_ONLY
        assert not any(vars(SNAPSHOT_ONLY).values())

    def test_the_file_backend_declares_incremental_atomic_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            backend = FileGraphPersistenceBackend(os.path.join(tmp, "g.json"))
            assert backend.capabilities() == BackendCapabilities(
                incremental_writes=True, transactions=True
            )

    def test_declaring_incremental_without_the_operations_is_refused(self):
        class Overclaims(_SnapshotBackend):
            def capabilities(self):
                return BackendCapabilities(incremental_writes=True)

            def upsert_node(self, node):
                pass

        with pytest.raises(TypeError) as exc:
            GraphStorage(persistence_backend=Overclaims())
        message = str(exc.value)
        assert "upsert_node" not in message
        for method in (
            "delete_node",
            "upsert_edge",
            "delete_edge",
            "apply_batch",
            "checkpoint",
        ):
            assert method in message

    def test_a_declaration_of_the_wrong_type_is_refused(self):
        class Wrong(_SnapshotBackend):
            def capabilities(self):
                return {"incremental_writes": True}

        with pytest.raises(TypeError, match="BackendCapabilities"):
            capabilities_of(Wrong())

    def test_the_protocols_are_structural(self):
        with tempfile.TemporaryDirectory() as tmp:
            file_backend = FileGraphPersistenceBackend(os.path.join(tmp, "g.json"))
        assert isinstance(file_backend, GraphPersistenceBackend)
        assert isinstance(file_backend, IncrementalGraphPersistenceBackend)
        assert isinstance(_IncrementalBackend(), IncrementalGraphPersistenceBackend)
        assert not isinstance(_SnapshotBackend(), IncrementalGraphPersistenceBackend)
        # A pre-contract backend fails the isinstance check (no capabilities)
        # yet is still driven by GraphStorage: capabilities_of tolerates it.
        assert not isinstance(_SnapshotBackend(), GraphPersistenceBackend)


class TestChangeNotificationWiring:
    """What GraphStorage does about a backend that reports external changes:
    when it subscribes, when it stops, and what a refresh must not do."""

    def test_declaring_change_notification_without_the_methods_is_refused(self):
        class Overclaims(_SnapshotBackend):
            def capabilities(self):
                return BackendCapabilities(change_notification=True)

            def start_change_notification(self, listener):
                pass

        with pytest.raises(TypeError) as exc:
            GraphStorage(persistence_backend=Overclaims())
        message = str(exc.value)
        assert "start_change_notification" not in message
        assert "stop_change_notification" in message

    def test_declaring_it_with_only_the_stop_method_is_refused_too(self):
        class Overclaims(_SnapshotBackend):
            def capabilities(self):
                return BackendCapabilities(change_notification=True)

            def stop_change_notification(self):
                pass

        with pytest.raises(TypeError) as exc:
            GraphStorage(persistence_backend=Overclaims())
        assert "start_change_notification" in str(exc.value)

    def test_a_backend_that_does_not_declare_it_is_never_subscribed(self):
        backend = _NotifyingBackend()
        backend.capabilities = lambda: BackendCapabilities(
            incremental_writes=True, transactions=True
        )
        storage = GraphStorage(persistence_backend=backend)
        try:
            assert backend.subscribes == 0
        finally:
            storage.shutdown_events()
        assert backend.unsubscribes == 0

    def test_the_listener_is_subscribed_once_the_graph_is_loaded(self):
        """A change reported against a model that does not exist yet would
        refresh nothing, and the seeded graph would be gone."""
        delivered = []

        def deliver(listener):
            delivered.append(True)
            listener(
                ExternalChange.entities(
                    [EntityOperation.upsert_node(_node_payload("b", "Beacon"))]
                )
            )

        backend = _NotifyingBackend(on_subscribe=deliver)
        backend.save_graph_data(
            {
                "nodes": [_node_payload("a", "Alpha")],
                "edges": [],
                "metadata": {"version": "1.0", "graph_name": "g"},
            }
        )
        backend.calls.clear()

        storage = GraphStorage(persistence_backend=backend)
        try:
            assert delivered == [True]
            assert backend.subscribes == 1
            assert {n.id for n in storage.get_all_nodes()} == {"a", "b"}
        finally:
            storage.shutdown_events()

    def test_shutdown_stops_the_notification_before_tearing_down(self):
        backend = _NotifyingBackend()
        storage = GraphStorage(persistence_backend=backend)
        assert backend.subscribes == 1
        storage.shutdown_events()
        assert backend.unsubscribes == 1
        assert backend.listener is None

    def test_a_refresh_is_never_written_back(self):
        """The change is already in the store. Persisting it would hand the
        writer that made it a second copy of its own work."""
        backend = _NotifyingBackend()
        storage = GraphStorage(persistence_backend=backend)
        try:
            storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="Alpha")], [])
            storage.flush()
            backend.calls.clear()

            backend.listener(
                ExternalChange.entities(
                    [
                        EntityOperation.upsert_node(_node_payload("b", "Beacon")),
                        EntityOperation.upsert_node(_node_payload("c", "Cedar")),
                        EntityOperation.upsert_edge(_edge_payload("bc", "b", "c")),
                        EntityOperation.delete_edge("bc"),
                        EntityOperation.delete_node("a"),
                    ]
                )
            )
            storage.flush()

            assert backend.calls == []
            assert {n.id for n in storage.get_all_nodes()} == {"b", "c"}
        finally:
            storage.shutdown_events()

    def test_a_refresh_emits_events_marked_as_someone_else_s_write(self):
        """Subscriptions, agents and the history see external changes too -
        and can tell them from this instance's own work, or an agent that
        reacts by writing would bounce the change between instances."""
        backend = _NotifyingBackend()
        storage = GraphStorage(persistence_backend=backend)
        seen = []
        storage.add_system_listener(seen.append)
        try:
            backend.listener(
                ExternalChange.entities(
                    [EntityOperation.upsert_node(_node_payload("b", "Beacon"))]
                )
            )
            assert [e.event_type for e in seen] == [EventType.NODE_CREATE]
            assert seen[0].origin.event_origin == EXTERNAL_CHANGE_ORIGIN
            assert seen[0].entity.after["name"] == "Beacon"
            # The same spelling every local emit site produces, or a
            # subscription filtered on the type silently never fires.
            assert seen[0].entity.type == "Actor"

            seen.clear()
            backend.listener(
                ExternalChange.entities(
                    [EntityOperation.upsert_node(_node_payload("b", "Renamed"))]
                )
            )
            assert [e.event_type for e in seen] == [EventType.NODE_UPDATE]
            assert seen[0].origin.event_origin == EXTERNAL_CHANGE_ORIGIN
            assert seen[0].entity.before["name"] == "Beacon"
            assert seen[0].entity.after["name"] == "Renamed"

            seen.clear()
            backend.listener(
                ExternalChange.entities(
                    [
                        EntityOperation.upsert_node(_node_payload("c", "Cedar")),
                        EntityOperation.upsert_edge(_edge_payload("bc", "b", "c")),
                    ]
                )
            )
            assert [e.event_type for e in seen] == [
                EventType.NODE_CREATE,
                EventType.EDGE_CREATE,
            ]
            assert {e.origin.event_origin for e in seen} == {EXTERNAL_CHANGE_ORIGIN}
            assert seen[1].entity.type == "RELATES_TO"

            seen.clear()
            backend.listener(
                ExternalChange.entities([EntityOperation.delete_edge("bc")])
            )
            assert [e.event_type for e in seen] == [EventType.EDGE_DELETE]
            assert seen[0].origin.event_origin == EXTERNAL_CHANGE_ORIGIN

            seen.clear()
            backend.listener(
                ExternalChange.entities([EntityOperation.delete_node("b")])
            )
            assert [e.event_type for e in seen] == [EventType.NODE_DELETE]
            assert seen[0].origin.event_origin == EXTERNAL_CHANGE_ORIGIN
            assert seen[0].entity.before["name"] == "Renamed"
        finally:
            storage.shutdown_events()

    def test_a_reload_does_not_emit_per_entity_events(self):
        """A backend that cannot name what changed gets a reload, and a reload
        has no before-states to report. The docs say so; this pins it."""
        backend = _NotifyingBackend()
        storage = GraphStorage(persistence_backend=backend)
        seen = []
        storage.add_system_listener(seen.append)
        try:
            backend.nodes = {"z": _node_payload("z", "Zulu")}
            backend.written = True
            backend.listener(ExternalChange.unknown())

            assert {n.id for n in storage.get_all_nodes()} == {"z"}
            assert seen == []
        finally:
            storage.shutdown_events()


class TestExternalRefreshLeavesNothingStale:
    def test_an_upsert_drops_a_vector_the_graph_file_still_carries_inline(self):
        """A pre-split store hands its vectors on the node objects, and
        `_serialize_node` keeps writing them back until a sidecar covers
        them. A refresh that only cleared the index would leave the vector
        for the old text to be written out and re-adopted on the next load."""
        backend = _NotifyingBackend()
        # The odd width is what keeps a vector in the fallback: the index
        # refuses it, and the graph file is then its only copy.
        backend.save_graph_data(
            {
                "nodes": [
                    dict(_node_payload("a", "Alpha"), embedding=[0.5, 0.25]),
                    dict(_node_payload("b", "Beacon"), embedding=[0.5, 0.25]),
                    dict(_node_payload("c", "Cedar"), embedding=[1.0]),
                ],
                "edges": [],
                "metadata": {"version": "1.0", "graph_name": "g"},
            }
        )
        storage = GraphStorage(persistence_backend=backend)
        try:
            assert "c" in storage._inline_fallback

            backend.listener(
                ExternalChange.entities(
                    [EntityOperation.upsert_node(_node_payload("c", "Renamed"))]
                )
            )

            assert "c" not in storage._inline_fallback
            # Still in the fallback, this would be written back out as the
            # vector for "Cedar" and re-adopted on the next load. Where the ML
            # stack is installed a fresh vector is generated for the new text
            # instead, which is equally not the old one.
            written = storage._serialize_node(storage.get_node("c"))["embedding"]
            assert written is None or written != pytest.approx([1.0])
        finally:
            storage.shutdown_events()

    def test_a_change_naming_no_entities_is_not_a_reload(self):
        """`entities([])` says nothing changed. Treating it as `unknown()`
        would throw away in-memory state on a message that carries none."""
        assert ExternalChange.entities([]).operations == ()

        backend = _NotifyingBackend()
        storage = GraphStorage(persistence_backend=backend)
        seen = []
        storage.add_system_listener(seen.append)
        try:
            storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="Alpha")], [])
            storage.flush()
            seen.clear()
            # The store says something else entirely; only a reload would
            # bring it in.
            backend.nodes = {"z": _node_payload("z", "Zulu")}

            backend.listener(ExternalChange.entities([]))

            assert {n.id for n in storage.get_all_nodes()} == {"a"}
            assert seen == []
        finally:
            storage.shutdown_events()


class TestExternalRefreshOnTheWriterThread:
    """The one thread a change may not be reported on is the one the write
    queue runs on. A refresh may have to wait for that queue, and waiting
    there waits for ourselves; not waiting reloads over the write we are
    inside; and handing it to another thread was tried and was worse - two
    such refreshes race, and one can outlive the shutdown meant to stop it.
    Every real transport reports from its own thread, so this is said plainly
    rather than worked around."""

    def test_a_report_from_the_write_queues_own_thread_is_refused(self):
        backend = _NotifyingBackend()
        storage = GraphStorage(persistence_backend=backend)
        try:
            refused = storage._io_executor.submit(
                storage.apply_external_change, ExternalChange.unknown()
            )
            with pytest.raises(RuntimeError, match="backend's own thread"):
                refused.result(timeout=30)

            # And the queue still works: refusing is not wedging.
            storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="Alpha")], [])
            storage.flush()
            assert storage.get_node("a") is not None
        finally:
            storage.shutdown_events()

    def test_a_backend_reporting_from_inside_its_write_does_not_wedge(self):
        """The realistic shape of the same mistake: the report comes from
        inside the write that made it. The write fails and is healed; what
        must not happen is the queue stopping forever."""
        backend = _NotifyingBackend()
        storage = GraphStorage(persistence_backend=backend)
        try:
            storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="Alpha")], [])
            storage.flush()
            backend.report_from_writes = ExternalChange.unknown()

            storage.update_node("a", {"name": "Renamed"})
            drain = threading.Thread(target=storage.flush, daemon=True)
            drain.start()
            drain.join(30)
            assert not drain.is_alive(), "the write queue wedged on itself"

            backend.report_from_writes = None
            assert storage.get_node("a").name == "Renamed"
        finally:
            backend.report_from_writes = None
            storage.shutdown_events()


class TestExternalEdgeRefresh:
    """The edge half of the refresh path. Its failures hide from
    `get_all_edges`, which reads `self.edges`; the graph is where they show."""

    def _storage(self):
        backend = _NotifyingBackend()
        storage = GraphStorage(persistence_backend=backend)
        storage.add_nodes(
            [
                Node(id="a", type=NodeType.ACTOR, name="Alpha"),
                Node(id="b", type=NodeType.ACTOR, name="Beacon"),
            ],
            [],
        )
        storage.flush()
        return storage, backend

    def test_an_edge_with_an_absent_endpoint_is_added_nowhere(self):
        """NetworkX would invent the missing endpoint as a node with no data,
        and every read path walking the graph would trip over it."""
        storage, backend = self._storage()
        try:
            backend.listener(
                ExternalChange.entities(
                    [EntityOperation.upsert_edge(_edge_payload("ax", "a", "ghost"))]
                )
            )
            assert storage.get_all_edges() == []
            assert storage.get_edges_for_node("a") == []
            assert not storage.graph.has_node("ghost")
            assert {n.id for n in storage.get_all_nodes()} == {"a", "b"}
        finally:
            storage.shutdown_events()

    def test_an_edge_that_moves_an_endpoint_leaves_one_graph_edge(self):
        """The graph edge is keyed on where it used to point; left in place it
        is served to the old endpoint forever."""
        storage, backend = self._storage()
        try:
            backend.listener(
                ExternalChange.entities(
                    [
                        EntityOperation.upsert_node(_node_payload("c", "Cedar")),
                        EntityOperation.upsert_edge(_edge_payload("ab", "a", "b")),
                    ]
                )
            )
            backend.listener(
                ExternalChange.entities(
                    [EntityOperation.upsert_edge(_edge_payload("ab", "a", "c"))]
                )
            )

            assert [e.id for e in storage.get_all_edges()] == ["ab"]
            assert storage.get_edges_for_node("b") == []
            assert [e.id for e in storage.get_edges_for_node("c")] == ["ab"]
            assert len(storage.graph.edges) == 1
        finally:
            storage.shutdown_events()

    def test_an_edge_repointed_at_an_absent_endpoint_keeps_serving_the_old_one(self):
        """Refusing the new edge is right - inventing the endpoint would be
        worse - but the edge already there must be left whole rather than
        half removed, or reads would serve an edge the graph no longer has."""
        storage, backend = self._storage()
        try:
            backend.listener(
                ExternalChange.entities(
                    [EntityOperation.upsert_edge(_edge_payload("ab", "a", "b"))]
                )
            )
            backend.listener(
                ExternalChange.entities(
                    [EntityOperation.upsert_edge(_edge_payload("ab", "a", "ghost"))]
                )
            )

            assert [e.target for e in storage.get_all_edges()] == ["b"]
            assert [e.id for e in storage.get_edges_for_node("b")] == ["ab"]
            assert not storage.graph.has_node("ghost")
            assert len(storage.graph.edges) == 1
        finally:
            storage.shutdown_events()

    def test_deleting_an_edge_takes_it_out_of_the_graph_too(self):
        storage, backend = self._storage()
        try:
            backend.listener(
                ExternalChange.entities(
                    [EntityOperation.upsert_edge(_edge_payload("ab", "a", "b"))]
                )
            )
            backend.listener(
                ExternalChange.entities([EntityOperation.delete_edge("ab")])
            )

            assert storage.get_all_edges() == []
            assert storage.get_edges_for_node("a") == []
            assert storage.get_edges_between_nodes(["a", "b"]) == []
            assert len(storage.graph.edges) == 0
        finally:
            storage.shutdown_events()

    def test_a_node_and_its_edge_deleted_in_one_batch_is_harmless(self):
        """The shape the write side itself produces: edges first, then the
        node. Whichever order it arrives in, deleting one twice is a no-op."""
        for operations in (
            [EntityOperation.delete_edge("ab"), EntityOperation.delete_node("b")],
            [EntityOperation.delete_node("b"), EntityOperation.delete_edge("ab")],
        ):
            storage, backend = self._storage()
            try:
                backend.listener(
                    ExternalChange.entities(
                        [EntityOperation.upsert_edge(_edge_payload("ab", "a", "b"))]
                    )
                )
                backend.listener(ExternalChange.entities(operations))

                assert {n.id for n in storage.get_all_nodes()} == {"a"}
                assert storage.get_all_edges() == []
                assert len(storage.graph.edges) == 0
            finally:
                storage.shutdown_events()


def _unreadable_change():
    """A change whose payload this build cannot read, so the refresh takes
    its error path - the twin of the reload below, and just as bound by
    everything a refresh may not do."""
    return ExternalChange.entities(
        [EntityOperation.upsert_node({"id": "bad", "type": "Nonsense"})]
    )


# Both ways into the resync, so neither entry point is pinned alone.
_RESYNCING_CHANGES = [ExternalChange.unknown, _unreadable_change]
_RESYNC_IDS = ["unknown", "unreadable-payload"]


class TestExternalRefreshFailureModes:
    """What a refresh must not do when the store is not in the state it
    expects: write back, drop a local write, or throw into the caller."""

    @pytest.mark.parametrize("change", _RESYNCING_CHANGES, ids=_RESYNC_IDS)
    def test_a_reload_does_not_write_back_over_a_missing_store(self, change):
        """A store can stop existing - another writer emptied it, a restore is
        in progress. Bootstrapping there would put this instance's graph over
        it, which is a write-back, and a whole-graph one."""
        backend = _NotifyingBackend()
        storage = GraphStorage(persistence_backend=backend)
        try:
            storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="Alpha")], [])
            storage.flush()
            backend.written = False
            backend.calls.clear()

            backend.listener(change())
            storage.flush()

            assert backend.calls == []
            # Nothing to reload from is not a reason to forget what we have.
            assert {n.id for n in storage.get_all_nodes()} == {"a"}
        finally:
            storage.shutdown_events()

    def test_a_reload_that_cannot_read_the_store_leaves_the_graph_whole(self):
        """The store is readable but holds an entity this build is not: a
        rolling upgrade. Clearing the graph and then failing to refill it
        would leave every read path short of entities the store still has,
        reported as merely being behind it."""
        backend = _NotifyingBackend()
        storage = GraphStorage(persistence_backend=backend)
        try:
            storage.add_nodes(
                [
                    Node(id="a", type=NodeType.ACTOR, name="Alpha"),
                    Node(id="b", type=NodeType.ACTOR, name="Beacon"),
                ],
                [],
            )
            storage.flush()
            backend.nodes["bad"] = {"id": "bad", "type": "Nonsense"}

            backend.listener(ExternalChange.unknown())

            assert {n.id for n in storage.get_all_nodes()} == {"a", "b"}
            assert [n.id for n in storage.search_nodes("Beacon")] == ["b"]
            assert set(storage.graph.nodes) == {"a", "b"}
        finally:
            storage.shutdown_events()

    def test_a_reload_does_not_drop_a_write_whose_own_write_failed(self):
        """The other half of the in-flight case: the write did not just not
        land yet, it failed, and the heal that would write it has not run.
        Reloading over that loses a mutation this instance already accepted -
        from memory, and then from the store when the heal writes what it
        reloaded."""
        backend = _NotifyingBackend()
        storage = GraphStorage(persistence_backend=backend)
        try:
            storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="Alpha")], [])
            storage.flush()

            def refuse(node):
                raise OSError("the write failed")

            backend.upsert_node = refuse
            storage.add_nodes([Node(id="c", type=NodeType.ACTOR, name="Cedar")], [])
            storage._io_executor.submit(lambda: None).result()
            assert storage._resync_pending, "the failed write did not raise the flag"
            del backend.upsert_node

            backend.listener(ExternalChange.unknown())
            storage.flush()

            assert {n.id for n in storage.get_all_nodes()} == {"a", "c"}
            assert set(backend.nodes) == {"a", "c"}
        finally:
            storage.shutdown_events()

    @pytest.mark.parametrize("change", _RESYNCING_CHANGES, ids=_RESYNC_IDS)
    def test_a_reload_does_not_drop_a_write_still_in_flight(self, change):
        """A mutation is in memory before it is in the store. Reloading over
        one still queued would take it out of memory while it goes on to
        land, leaving this instance unable to ever see what it wrote."""
        proceed = threading.Event()
        backend = _NotifyingBackend()
        real_upsert = backend.upsert_node

        def slow_upsert(node):
            # Unbounded on purpose: the `finally` below always releases it, so
            # a deadline here could only turn a slow runner into a failure
            # about a state this test never meant to create.
            proceed.wait()
            real_upsert(node)

        storage = GraphStorage(persistence_backend=backend)
        try:
            backend.upsert_node = slow_upsert
            storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="Alpha")], [])

            refresh = threading.Thread(
                target=storage.apply_external_change,
                args=(change(),),
            )
            refresh.start()
            # The refresh must be waiting on the queued write, not racing it.
            refresh.join(0.5)
            assert refresh.is_alive(), "the refresh reloaded without draining"

            proceed.set()
            refresh.join(5)
            assert not refresh.is_alive()
            assert {n.id for n in storage.get_all_nodes()} == {"a"}
        finally:
            proceed.set()
            storage.shutdown_events()

    @pytest.mark.parametrize(
        "kind,action",
        [
            ("node", "archive"),
            ("edge", "archive"),
            ("annotation", "delete"),
            ("annotation", "upsert"),
        ],
        ids=[
            "node-action",
            "edge-action",
            "unknown-kind-delete",
            "unknown-kind-upsert",
        ],
    )
    def test_an_operation_this_build_does_not_know_resyncs(self, kind, action):
        """Neither an unrecognised action nor an unrecognised kind may fall
        through to the delete branch: the entity would go, and every
        subscriber would be told it was deleted, on a message that said
        nothing of the kind."""
        backend = _NotifyingBackend()
        storage = GraphStorage(persistence_backend=backend)
        seen = []
        storage.add_system_listener(seen.append)
        try:
            storage.add_nodes(
                [
                    Node(id="a", type=NodeType.ACTOR, name="Alpha"),
                    Node(id="b", type=NodeType.ACTOR, name="Beacon"),
                ],
                [Edge(id="ab", source="a", target="b")],
            )
            storage.flush()
            seen.clear()
            backend.calls.clear()

            backend.listener(
                ExternalChange.entities(
                    [
                        EntityOperation(
                            kind=kind,
                            action=action,
                            entity_id="ab" if kind == "edge" else "a",
                            payload=_node_payload("a", "Alpha"),
                        )
                    ]
                )
            )

            assert {n.id for n in storage.get_all_nodes()} == {"a", "b"}
            assert [e.id for e in storage.get_all_edges()] == ["ab"]
            assert [e.event_type for e in seen] == []
            assert backend.calls == []
        finally:
            storage.shutdown_events()

    @pytest.mark.parametrize(
        "failure",
        [
            OSError("the store is being replaced"),
            json.JSONDecodeError("half a file", "{", 1),
            RuntimeError("the connection went away"),
        ],
        ids=["oserror", "half-written-json", "runtime"],
    )
    def test_a_resync_that_fails_too_does_not_escape(self, failure):
        """The fallback has its own failure mode - the store is being replaced
        as we read it, a read returns half a file. However it fails, throwing
        from here puts it back in the caller's lap, which is the backend's
        thread, and is what the fallback exists to prevent."""
        backend = _NotifyingBackend()
        storage = GraphStorage(persistence_backend=backend)
        try:
            storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="Alpha")], [])
            storage.flush()

            def unreadable_store():
                raise failure

            backend.load_graph_data = unreadable_store

            # Both ways in: the reload, and the containment fallback.
            backend.listener(ExternalChange.unknown())
            backend.listener(
                ExternalChange.entities(
                    [
                        EntityOperation(
                            kind="node", action="upsert", entity_id="bad", payload=None
                        )
                    ]
                )
            )

            # Incomplete, not wrong, and nothing thrown at the backend.
            assert {n.id for n in storage.get_all_nodes()} == {"a"}
        finally:
            storage.shutdown_events()

    @pytest.mark.parametrize(
        "unreadable",
        [
            EntityOperation.upsert_node({"id": "bad", "type": "Nonsense"}),
            # A backend deserialising its own store builds the operation
            # directly, so the payload need not have survived the classmethod.
            EntityOperation(
                kind="node", action="upsert", entity_id="bad", payload=None
            ),
            EntityOperation(
                kind="edge", action="upsert", entity_id="bad", payload="not a payload"
            ),
        ],
        ids=["unknown-type", "a-node-that-is-not-a-dict", "an-edge-that-is-a-string"],
    )
    def test_a_payload_this_build_cannot_read_does_not_escape(self, unreadable):
        """Two instances mid-upgrade. Half a batch is worse than none, and the
        writing instance must not read our failure as its own. The payload
        need not be a dict at all - a store of another vintage decides what
        it hands over - so the containment cannot be narrowed to one family
        of exception."""
        backend = _NotifyingBackend()
        storage = GraphStorage(persistence_backend=backend)
        try:
            storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="Alpha")], [])
            storage.flush()
            backend.calls.clear()

            backend.listener(
                ExternalChange.entities(
                    [
                        EntityOperation.upsert_node(_node_payload("b", "Beacon")),
                        unreadable,
                        EntityOperation.upsert_node(_node_payload("c", "Cedar")),
                    ]
                )
            )
            storage.flush()

            # Resynced from the store rather than left half applied, and the
            # resync is still a refresh: it writes nothing back.
            assert {n.id for n in storage.get_all_nodes()} == {"a"}
            assert backend.calls == []
        finally:
            storage.shutdown_events()

    def test_a_refresh_waits_for_whoever_holds_the_model(self):
        """Deterministic half of the lock guarantee: while another thread
        holds `_lock`, a refresh cannot be touching the model."""
        backend = _NotifyingBackend()
        storage = GraphStorage(persistence_backend=backend)
        done = threading.Event()

        def refresh():
            storage.apply_external_change(
                ExternalChange.entities(
                    [EntityOperation.upsert_node(_node_payload("b", "Beacon"))]
                )
            )
            done.set()

        try:
            with storage._lock:
                thread = threading.Thread(target=refresh)
                thread.start()
                assert not done.wait(0.5), "the refresh did not take the lock"
                assert storage.get_node("b") is None

            thread.join(5)
            assert done.is_set()
            assert storage.get_node("b").name == "Beacon"
        finally:
            storage.shutdown_events()

    def test_a_refresh_holds_the_lock_against_local_writes(self):
        """The listener may be called from any thread the backend likes, so a
        refresh interleaved with local mutations must not tear the model."""
        backend = _NotifyingBackend()
        storage = GraphStorage(persistence_backend=backend)
        errors = []
        stop = threading.Event()

        def refresher():
            try:
                while not stop.is_set():
                    storage.apply_external_change(
                        ExternalChange.entities(
                            [
                                EntityOperation.upsert_node(
                                    _node_payload("shared", "Shared")
                                )
                            ]
                        )
                    )
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                errors.append(exc)

        try:
            thread = threading.Thread(target=refresher)
            thread.start()
            for index in range(40):
                storage.add_nodes(
                    [Node(id=f"n{index}", type=NodeType.ACTOR, name=f"N{index}")], []
                )
            stop.set()
            thread.join(10)
            assert not thread.is_alive()
            assert errors == []

            ids = {n.id for n in storage.get_all_nodes()}
            assert ids == {f"n{i}" for i in range(40)} | {"shared"}
            # Every derived structure still agrees with the node dictionary.
            assert set(storage.graph.nodes) == ids
            assert set(storage._searchable_text_cache) == ids
        finally:
            stop.set()
            storage.shutdown_events()


class TestSnapshotOnlyBackends:
    def test_a_snapshot_backend_is_driven_exactly_as_before(self):
        backend = _SnapshotBackend()
        storage = _storage(backend)

        storage.add_nodes([_node("a"), _node("b")], [_edge("e", "a", "b")])
        storage.flush()
        # add_nodes has always written twice: once after the nodes, once
        # after the edges. That is the shape to keep, not to tidy.
        assert backend.snapshots == 2

        storage.update_node("a", {"name": "Renamed"})
        storage.flush()
        assert backend.snapshots == 3
        assert {n["id"]: n["name"] for n in backend.data["nodes"]}["a"] == "Renamed"

    def test_the_bootstrap_of_an_empty_store_is_a_snapshot(self):
        backend = _IncrementalBackend()
        GraphStorage(persistence_backend=backend).flush()
        assert backend.calls == [("snapshot", None)]
        assert backend.written

    def test_flush_asks_an_incremental_backend_to_checkpoint(self):
        backend = _IncrementalBackend()
        storage = _storage(backend)
        before = backend.checkpoints

        storage.flush()

        assert backend.checkpoints == before + 1

    def test_the_sidecar_travels_with_an_entity_write(self):
        """The file backend has a vector sidecar AND writes incrementally. A
        mutation that moved a vector lands it in the sidecar before the entity
        write, exactly as a snapshot would - and does not rewrite graph.json."""
        with tempfile.TemporaryDirectory() as tmp:
            graph_path = os.path.join(tmp, "g.json")
            storage = GraphStorage(json_path=graph_path)
            try:
                storage.flush()
                written_at_bootstrap = os.stat(graph_path).st_mtime_ns
                node = Node(
                    id="v", type=NodeType.ACTOR, name="V", embedding=[0.5, 0.25]
                )

                storage.add_nodes([node], [])
                # Drain the write without checkpointing: the queue, not flush().
                storage._io_executor.submit(lambda: None).result()

                assert storage.vectors_persisted
                assert storage.embeddings_path.exists()
                assert os.stat(graph_path).st_mtime_ns == written_at_bootstrap
                assert storage._persistence_backend.journal_path.exists()
            finally:
                storage.flush()


class TestIncrementalBackends:
    def test_a_single_node_update_is_one_upsert_and_no_snapshot(self):
        backend = _IncrementalBackend()
        storage = _storage(backend)
        storage.add_nodes([_node("a")], [])
        storage.flush()
        backend.calls.clear()

        storage.update_node("a", {"name": "Renamed"})
        storage.flush()

        assert _kinds(backend) == ["upsert_node"]
        assert backend.snapshots == 0
        payload = backend.calls[0][1]
        assert payload["id"] == "a" and payload["name"] == "Renamed"
        # With no sidecar the vector travels in the payload, as in a snapshot.
        assert "embedding" in payload

    def test_an_upsert_carries_what_a_snapshot_would_hold_for_the_node(self):
        backend = _IncrementalBackend()
        storage = _storage(backend)
        storage.add_nodes([_node("a")], [])
        storage.update_node("a", {"description": "changed", "tags": ["x"]})
        storage.flush()
        upserted = backend.calls[-1][1]

        storage.save().result()
        assert backend.nodes["a"] == upserted

    def test_adding_nodes_and_edges_lands_the_nodes_before_the_edge(self):
        backend = _IncrementalBackend()
        storage = _storage(backend)

        result = storage.add_nodes([_node("a"), _node("b")], [_edge("e", "a", "b")])
        storage.flush()

        assert result.success
        assert _kinds(backend) == ["apply_batch", "upsert_edge"]
        batch = backend.calls[0][1]
        assert [(op.action, op.entity_id) for op in batch] == [
            ("upsert", "a"),
            ("upsert", "b"),
        ]
        assert backend.calls[1][1]["id"] == "e"
        assert backend.snapshots == 0

    def test_deleting_a_node_is_one_batch_with_its_edges_first(self):
        backend = _IncrementalBackend()
        storage = _storage(backend)
        storage.add_nodes(
            [_node("a"), _node("b"), _node("c")],
            [_edge("ab", "a", "b"), _edge("ca", "c", "a"), _edge("bc", "b", "c")],
        )
        storage.flush()
        backend.calls.clear()

        result = storage.delete_nodes(["a"], confirmed=True)
        storage.flush()

        assert result.success
        assert _kinds(backend) == ["apply_batch"]
        batch = backend.calls[0][1]
        assert [(op.kind, op.action, op.entity_id) for op in batch] == [
            ("edge", "delete", "ab"),
            ("edge", "delete", "ca"),
            ("node", "delete", "a"),
        ]
        assert all(op.payload is None for op in batch)
        assert set(backend.nodes) == {"b", "c"}
        assert set(backend.edges) == {"bc"}
        assert backend.snapshots == 0

    @pytest.mark.parametrize(
        "mutate, expected",
        [
            (lambda s: s.add_edge(_edge("e2", "a", "b")), ("upsert_edge", "e2")),
            (lambda s: s.update_edge("e", {"label": "l"}), ("upsert_edge", "e")),
            (lambda s: s.delete_edge("e"), ("delete_edge", "e")),
            (lambda s: s.delete_edges(["e"]), ("delete_edge", "e")),
            (lambda s: s.set_nodes_archived(["a"], True), ("upsert_node", "a")),
            (lambda s: s.set_edges_archived(["e"], True), ("upsert_edge", "e")),
        ],
        ids=[
            "add_edge",
            "update_edge",
            "delete_edge",
            "delete_edges-of-one",
            "archive-one-node",
            "archive-one-edge",
        ],
    )
    def test_a_single_operation_uses_its_own_method_not_a_batch(self, mutate, expected):
        backend = _IncrementalBackend()
        storage = _storage(backend)
        storage.add_nodes([_node("a"), _node("b")], [_edge("e", "a", "b")])
        storage.flush()
        backend.calls.clear()

        mutate(storage)
        storage.flush()

        method, entity_id = expected
        assert len(backend.calls) == 1
        name, arg = backend.calls[0]
        assert name == method
        assert (arg["id"] if isinstance(arg, dict) else arg) == entity_id
        assert backend.snapshots == 0

    def test_archiving_several_entities_is_one_batch(self):
        backend = _IncrementalBackend()
        storage = _storage(backend)
        storage.add_nodes(
            [_node("a"), _node("b")], [_edge("e", "a", "b"), _edge("f", "b", "a")]
        )
        storage.flush()
        backend.calls.clear()

        storage.set_nodes_archived(["a", "b"], True)
        storage.set_edges_archived(["e", "f"], True)
        storage.flush()

        assert _kinds(backend) == ["apply_batch", "apply_batch"]
        nodes, edges = (c[1] for c in backend.calls)
        assert [op.entity_id for op in nodes] == ["a", "b"]
        assert [op.entity_id for op in edges] == ["e", "f"]
        assert all(op.payload["archived"] for op in nodes + edges)

    def test_nothing_changed_means_nothing_written(self):
        backend = _IncrementalBackend()
        storage = _storage(backend)
        storage.add_nodes([_node("a"), _node("b")], [])
        storage.flush()
        backend.calls.clear()

        # Already unarchived, so no node changes; the edge is the only write.
        storage.set_nodes_archived(["a", "b"], False)
        storage.add_nodes([], [_edge("e", "a", "b")])
        storage.flush()

        assert _kinds(backend) == ["upsert_edge"]
        assert backend.snapshots == 0

    def test_without_transactions_a_multi_entity_mutation_is_a_snapshot(self):
        backend = _IncrementalBackend(transactions=False)
        storage = _storage(backend)
        storage.add_nodes([_node("a"), _node("b")], [_edge("e", "a", "b")])
        storage.flush()
        # Two nodes in one batch cannot be promised atomic, so a snapshot;
        # the single edge still goes as itself.
        assert _kinds(backend) == ["snapshot", "upsert_edge"]
        backend.calls.clear()

        storage.update_node("a", {"name": "Renamed"})
        storage.delete_nodes(["a"], confirmed=True)
        storage.flush()

        assert _kinds(backend) == ["upsert_node", "snapshot"]
        assert "apply_batch" not in _kinds(backend)
        assert set(backend.nodes) == {"b"} and backend.edges == {}

    def test_entity_writes_and_snapshots_share_one_ordered_queue(self):
        backend = _IncrementalBackend()
        storage = _storage(backend)
        storage.add_nodes([_node("a")], [])
        storage.flush()
        backend.calls.clear()

        storage.update_node("a", {"name": "first"})
        storage.save()
        storage.update_node("a", {"name": "second"})
        storage.flush()

        assert _kinds(backend) == ["upsert_node", "snapshot", "upsert_node"]
        assert backend.nodes["a"]["name"] == "second"

    def test_a_failing_entity_write_surfaces_on_the_future(self):
        backend = _IncrementalBackend()
        storage = _storage(backend)

        def refuse(node_id):
            raise RuntimeError("store unavailable")

        backend.delete_node = refuse
        with storage._lock:
            future = storage._persist([EntityOperation.delete_node("x")])
        with pytest.raises(RuntimeError, match="store unavailable"):
            future.result()

    def test_what_the_operations_built_loads_back_as_the_graph(self):
        backend = _IncrementalBackend()
        storage = _storage(backend)
        storage.add_nodes(
            [_node("a"), _node("b"), _node("c")],
            [_edge("ab", "a", "b"), _edge("bc", "b", "c")],
        )
        storage.update_node("b", {"name": "Bee", "tags": ["t"]})
        storage.set_edges_archived(["bc"], True)
        storage.delete_nodes(["c"], confirmed=True)
        storage.flush()
        assert backend.snapshots == 0

        reloaded = GraphStorage(persistence_backend=backend)
        try:
            assert {n.id: n.name for n in reloaded.get_all_nodes()} == {
                "a": "A",
                "b": "Bee",
            }
            assert reloaded.get_node("b").tags == ["t"]
            assert [e.id for e in reloaded.get_all_edges()] == ["ab"]
        finally:
            reloaded.flush()


class TestPartialFailure:
    def test_edges_added_before_a_rejected_one_still_reach_the_store(self):
        """add_nodes returns failure on a bad edge but keeps, in memory, the
        nodes and the edges it had already added. On a snapshot backend the
        next write of anything carried those edges; on an incremental backend
        nothing would ever send them unless the failure path does."""
        backend = _IncrementalBackend()
        storage = _storage(backend)

        result = storage.add_nodes(
            [_node("a"), _node("b")],
            [_edge("good", "a", "b"), _edge("bad", "a", "no-such-node")],
        )
        storage.flush()

        assert not result.success
        assert set(storage.edges) == {"good"}
        assert _kinds(backend) == ["apply_batch", "upsert_edge"]
        assert backend.calls[1][1]["id"] == "good"
        assert set(backend.nodes) == {"a", "b"} and set(backend.edges) == {"good"}

    @pytest.mark.parametrize(
        "nodes, edges, expect_nodes, expect_edges",
        [
            ([_node("c"), _node("a")], [], {"a", "b", "c"}, {"e"}),
            (
                [],
                [_edge("e2", "a", "b"), _edge("e", "a", "b")],
                {"a", "b"},
                {"e", "e2"},
            ),
        ],
        ids=["duplicate-node-id", "duplicate-edge-id"],
    )
    def test_a_rejected_duplicate_id_still_persists_what_landed_before_it(
        self, nodes, edges, expect_nodes, expect_edges
    ):
        """The duplicate-id exits are returns, not raises, and fire after the
        entities before them have landed in memory; the store must follow."""
        backend = _IncrementalBackend()
        storage = _storage(backend)
        storage.add_nodes([_node("a"), _node("b")], [_edge("e", "a", "b")])
        storage.flush()
        backend.calls.clear()

        result = storage.add_nodes(nodes, edges)
        storage.flush()

        assert not result.success and "already exists" in result.message
        assert set(storage.nodes) == expect_nodes == set(backend.nodes)
        assert set(storage.edges) == expect_edges == set(backend.edges)

    def test_a_failure_after_the_executor_is_gone_is_still_a_failure_result(self):
        backend = _IncrementalBackend()
        storage = _storage(backend)
        storage.shutdown_events()

        result = storage.add_nodes([_node("a")], [])

        assert not result.success
        assert "Error during add" in result.message
        assert "a" in storage.nodes and "a" not in backend.nodes

    def test_a_snapshot_backend_writes_once_more_on_the_same_failure(self):
        backend = _SnapshotBackend()
        storage = _storage(backend)

        result = storage.add_nodes(
            [_node("a"), _node("b")],
            [_edge("good", "a", "b"), _edge("bad", "a", "no-such-node")],
        )
        storage.flush()

        assert not result.success
        # The nodes' write, then the failure handler's write of the edge.
        assert backend.snapshots == 2
        assert [e["id"] for e in backend.data["edges"]] == ["good"]


class TestPayloadFidelity:
    def test_a_node_upsert_carries_the_vector_a_snapshot_would(self):
        """Node.to_dict() emits embedding=None - the vector lives in the vector
        store, not on the node - so an upsert built from it would silently
        drop the vector on a backend with no sidecar. Every node-upsert site
        must go through _serialize_node, which puts it back."""
        backend = _IncrementalBackend()
        storage = _storage(backend)
        node = Node(
            id="v", type=NodeType.ACTOR, name="Vectored", embedding=[0.1, 0.2, 0.3]
        )

        storage.add_nodes([node], [])
        storage.update_node("v", {"name": "Renamed"})
        storage.set_nodes_archived(["v"], True)
        storage.flush()

        # One node in add_nodes is a single operation, so three upserts in all.
        upserts = [arg for name, arg in backend.calls if name == "upsert_node"]
        assert len(upserts) == 3
        storage.save().result()
        snapshot = backend.nodes["v"]
        assert snapshot["embedding"] is not None
        for payload in upserts:
            assert payload["embedding"] == snapshot["embedding"]

    def test_an_edge_upsert_carries_what_a_snapshot_would_hold_for_the_edge(self):
        backend = _IncrementalBackend()
        storage = _storage(backend)
        storage.add_nodes([_node("a"), _node("b")], [_edge("e", "a", "b")])
        storage.update_edge("e", {"label": "knows", "metadata": {"w": 2}})
        storage.flush()
        upserted = backend.calls[-1][1]
        assert upserted["label"] == "knows" and upserted["metadata"] == {"w": 2}

        storage.save().result()
        assert backend.edges["e"] == upserted

    def test_archiving_persists_only_the_entities_whose_flag_changed(self):
        backend = _IncrementalBackend()
        storage = _storage(backend)
        storage.add_nodes(
            [_node("a"), _node("b")], [_edge("e", "a", "b"), _edge("f", "b", "a")]
        )
        storage.set_nodes_archived(["a"], True)
        storage.set_edges_archived(["e"], True)
        storage.flush()
        backend.calls.clear()

        storage.set_nodes_archived(["a", "b"], True)
        storage.set_edges_archived(["e", "f"], True)
        storage.flush()

        assert [(n, a["id"]) for n, a in backend.calls] == [
            ("upsert_node", "b"),
            ("upsert_edge", "f"),
        ]

    def test_deleting_an_edgeless_node_is_one_delete_node(self):
        backend = _IncrementalBackend()
        storage = _storage(backend)
        storage.add_nodes([_node("a"), _node("lone")], [])
        storage.flush()
        backend.calls.clear()

        storage.delete_nodes(["lone"], confirmed=True)
        storage.flush()

        assert backend.calls == [("delete_node", "lone")]
        assert set(backend.nodes) == {"a"}
