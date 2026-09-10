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
from datetime import datetime, timedelta, timezone

import pytest

from backend.core.events.models import EntityKind, EventType
from backend.core.models import Edge, Node, NodeType, RelationshipType
from backend.core.storage import EXTERNAL_CHANGE_ORIGIN, GraphStorage
from backend.core.storage_backends import (
    SNAPSHOT_ONLY,
    BackendCapabilities,
    EntityOperation,
    ExternalChange,
    ExternalChangeRefused,
    FileGraphPersistenceBackend,
    GraphPersistenceBackend,
    IncrementalGraphPersistenceBackend,
    capabilities_of,
)


def _node_payload(node_id: str, name: str):
    """A node as GraphStorage serialises one, for a store to hand back.

    Stamped now: a payload standing in for another instance's write has to
    look like one, and the refresh resolves a node both instances touched by
    last-writer-wins.
    """
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
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _edge_payload(edge_id: str, source: str, target: str, **overrides):
    payload = {
        "id": edge_id,
        "source": source,
        "target": target,
        "type": "RELATES_TO",
        "label": "",
        "metadata": {},
        "archived": False,
        "created_at": "2026-09-06T00:00:00+00:00",
    }
    payload.update(overrides)
    return payload


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

    def __init__(self, on_subscribe=None, incremental=True, **kwargs):
        super().__init__(**kwargs)
        self._incremental = incremental
        self.listener = None
        self.subscribes = 0
        self.unsubscribes = 0
        self._on_subscribe = on_subscribe
        # When set, every write also reports one - from inside the write,
        # which is the application's own writer thread.
        self.report_from_writes = None

    def capabilities(self):
        return BackendCapabilities(
            incremental_writes=self._incremental,
            transactions=self._incremental and self._transactions,
            change_notification=True,
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

    def save_graph_data(self, data):
        super().save_graph_data(data)
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

    def test_a_declaration_of_the_wrong_type_is_refused_at_construction(self):
        """The case above pins capabilities_of() in isolation; GraphStorage.
        __init__ calls it eagerly, so the same backend must fail to
        construct a storage at all, not just fail a direct capabilities_of()
        call."""

        class Wrong(_SnapshotBackend):
            def capabilities(self):
                return {"incremental_writes": True}

        with pytest.raises(TypeError, match="BackendCapabilities"):
            GraphStorage(persistence_backend=Wrong())

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
            # subscription filtered on the type silently never fires. The
            # kind is the other half of that: EventDispatcher._matches and
            # the history both key on it.
            assert seen[0].entity.type == "Actor"
            assert seen[0].entity.kind == EntityKind.NODE

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
            assert [e.entity.kind for e in seen] == [EntityKind.NODE, EntityKind.EDGE]

            # An edge upsert over one already there is an update carrying a
            # real before-state, the same as the node step above. Reported as
            # a create, or with before taken from the new edge, a subscriber
            # filtered on the update never fires and history records no diff.
            seen.clear()
            backend.listener(
                ExternalChange.entities(
                    [
                        EntityOperation.upsert_edge(
                            _edge_payload("bc", "b", "c", type="DEPENDS_ON")
                        )
                    ]
                )
            )
            assert [e.event_type for e in seen] == [EventType.EDGE_UPDATE]
            assert seen[0].entity.kind == EntityKind.EDGE
            assert seen[0].entity.before["type"] == "RELATES_TO"
            assert seen[0].entity.after["type"] == "DEPENDS_ON"
            assert seen[0].entity.type == "DEPENDS_ON"

            seen.clear()
            backend.listener(
                ExternalChange.entities([EntityOperation.delete_edge("bc")])
            )
            assert [e.event_type for e in seen] == [EventType.EDGE_DELETE]
            assert seen[0].origin.event_origin == EXTERNAL_CHANGE_ORIGIN
            assert seen[0].entity.type == "DEPENDS_ON"
            assert seen[0].entity.kind == EntityKind.EDGE
            assert seen[0].entity.before["id"] == "bc"
            assert seen[0].entity.after is None

            seen.clear()
            backend.listener(
                ExternalChange.entities([EntityOperation.delete_node("b")])
            )
            assert [e.event_type for e in seen] == [EventType.NODE_DELETE]
            assert seen[0].origin.event_origin == EXTERNAL_CHANGE_ORIGIN
            assert seen[0].entity.before["name"] == "Renamed"
            assert seen[0].entity.type == "Actor"
            assert seen[0].entity.kind == EntityKind.NODE
            assert seen[0].entity.after is None
        finally:
            storage.shutdown_events()

    def test_a_refresh_emits_each_event_after_its_own_change_landed(self):
        """A subscriber reads the model when it is told. An event emitted
        before the mutation it announces hands out a graph that does not have
        it yet - and on a delete, one that still does."""
        backend = _NotifyingBackend()
        storage = GraphStorage(persistence_backend=backend)
        observed = []

        def observe(event):
            observed.append(
                (
                    event.event_type,
                    {n.id for n in storage.get_all_nodes()},
                    {e.id for e in storage.get_all_edges()},
                    storage.graph.number_of_edges(),
                )
            )

        storage.add_system_listener(observe)
        try:
            backend.listener(
                ExternalChange.entities(
                    [
                        EntityOperation.upsert_node(_node_payload("b", "Beacon")),
                        EntityOperation.upsert_node(_node_payload("c", "Cedar")),
                        EntityOperation.upsert_edge(_edge_payload("bc", "b", "c")),
                    ]
                )
            )
            assert observed[-1][0] == EventType.EDGE_CREATE
            assert observed[-1][2] == {"bc"}
            assert observed[-1][3] == 1

            observed.clear()
            backend.listener(
                ExternalChange.entities([EntityOperation.delete_node("b")])
            )
            # The cascade goes first, and the edge is gone from both places
            # by the time either event is delivered.
            assert [step[0] for step in observed] == [
                EventType.EDGE_DELETE,
                EventType.NODE_DELETE,
            ]
            assert [step[2] for step in observed] == [set(), set()]
            assert [step[3] for step in observed] == [0, 0]
            assert observed[-1][1] == {"c"}
        finally:
            storage.shutdown_events()

    def test_a_reload_takes_departed_entities_out_of_the_graph_too(self):
        """A reload that removes things is where a graph left uncleared
        shows: the dictionaries follow the store, and every read path that
        walks the graph goes on serving nodes and edges that are gone."""
        backend = _NotifyingBackend()
        storage = GraphStorage(persistence_backend=backend)
        try:
            storage.add_nodes(
                [
                    Node(id="a", type=NodeType.ACTOR, name="Alpha"),
                    Node(id="b", type=NodeType.ACTOR, name="Beacon"),
                ],
                [Edge(id="ab", source="a", target="b")],
            )
            storage.flush()

            # The other writer replaced the store wholesale.
            backend.nodes = {"z": _node_payload("z", "Zulu")}
            backend.edges = {}
            backend.listener(ExternalChange.unknown())

            assert {n.id for n in storage.get_all_nodes()} == {"z"}
            assert set(storage.graph.nodes) == {"z"}
            assert storage.get_all_edges() == []
            assert storage.graph.number_of_edges() == len(storage.get_all_edges())
            assert set(storage._searchable_text_cache) == {"z"}
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


def _generated(name: str):
    """The vector `_stub_generator` produces for a node of this name."""
    return [float(len(name)), 1.0]


def _stub_generator(storage):
    """Stand in for the embedding model, which CI does not install.

    Without it every test of the settle's generation branch is vacuous: the
    real call raises ImportError and the branch does nothing.

    The vector is derived from the node's name rather than being a constant,
    so an assertion can tell which node was embedded. A constant cannot: it
    reads the same whether the batch handed generation the node it left or
    the one it replaced, and embedding the replaced node's text is exactly
    the stale vector the refresh exists to prevent.
    """

    def generate(nodes):
        storage.vector_store._absorb({node.id: _generated(node.name) for node in nodes})

    storage.vector_store.update_nodes_embeddings = generate


class TestExternalRefreshGeneratesWhatTheStoreDidNotSupply:
    """The half of the settle CI cannot reach on its own."""

    def _storage(self):
        backend = _NotifyingBackend()
        storage = GraphStorage(persistence_backend=backend)
        storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="Alpha")], [])
        storage.flush()
        return storage, backend

    def test_a_supplied_vector_is_not_overwritten_by_generation(self):
        """The asymmetry `_adopt_supplied_vectors` documents: add_nodes
        generates over the whole batch so generation wins there, while a
        refresh generates only where the store supplied nothing. Computing
        what is missing before adopting rather than after inverts it."""
        storage, backend = self._storage()
        _stub_generator(storage)
        try:
            backend.listener(
                ExternalChange.entities(
                    [
                        EntityOperation.upsert_node(
                            dict(_node_payload("b", "Beacon"), embedding=[9.0, 9.0])
                        ),
                        EntityOperation.upsert_node(_node_payload("c", "Cedar")),
                    ]
                )
            )

            assert storage.vector_store.get_vector_list("b") == pytest.approx(
                [9.0, 9.0]
            ), "the store's own vector was overwritten by a generated one"
            # And the one the store said nothing about did get generated.
            assert storage.vector_store.get_vector_list("c") == pytest.approx(
                _generated("Cedar")
            )
        finally:
            storage.shutdown_events()

    def test_a_mixed_width_batch_does_not_lose_the_index(self):
        """The index anchors which width is right, and the batch empties it
        of exactly the ids being replaced. Read the anchor after that and it
        comes from the supplied vectors instead: a report of the wrong width
        is accepted rather than refused, and the generated vectors that
        follow then look like a model change and discard everything."""
        storage, backend = self._storage()
        storage.add_nodes(
            [Node(id="b", type=NodeType.ACTOR, name="Beacon", embedding=[2.0, 0.0])],
            [],
        )
        storage.flush()
        _stub_generator(storage)
        try:
            backend.listener(
                ExternalChange.entities(
                    [
                        # Three wide, where the index is two.
                        EntityOperation.upsert_node(
                            dict(
                                _node_payload("a", "Renamed"),
                                embedding=[1.0, 2.0, 3.0],
                            )
                        ),
                        EntityOperation.upsert_node(_node_payload("b", "Rebeacon")),
                    ]
                )
            )

            # The odd width is refused and both nodes fall back to generation.
            assert storage.vector_store.get_vector_list("a") == pytest.approx(
                _generated("Renamed")
            )
            assert storage.vector_store.get_vector_list("b") == pytest.approx(
                _generated("Rebeacon")
            )
        finally:
            storage.shutdown_events()

    @pytest.mark.parametrize(
        "reported, expected",
        [
            # Three reports, so the commonest width is not the first one.
            # With only two, a majority vote always ties and the tie resolves
            # to first-seen - which is the answer first-wins gives anyway, so
            # a two-report arrangement cannot tell the two rules apart at all.
            (
                [
                    ("a", "Alpha", [1.0, 1.0]),
                    ("b", "Beacon", [1.0, 2.0, 3.0]),
                    ("c", "Cedar", [4.0, 5.0, 6.0]),
                ],
                {
                    "a": [1.0, 1.0],
                    "b": _generated("Beacon"),
                    "c": _generated("Cedar"),
                },
            ),
            # First is the widest. Its vector is adopted and the narrow one
            # refused; generation then comes back at the model's width, which
            # reads as a model change and empties the index of what was just
            # adopted. The settle notices and generates for what was stranded,
            # so both nodes end with a vector. The per-operation path did not:
            # it left the first one with nothing, depending on the order the
            # store reported them in.
            (
                [("a", "Wide", [1.0, 2.0, 3.0]), ("b", "Narrow", [7.0, 7.0])],
                {"a": _generated("Wide"), "b": _generated("Narrow")},
            ),
            # Operation order and id order disagree, and both widths decide
            # nothing on their own.
            (
                [("z", "Zed", [7.0, 7.0]), ("a", "Ay", [1.0, 2.0, 3.0])],
                {"z": [7.0, 7.0], "a": _generated("Ay")},
            ),
        ],
        ids=[
            "first-is-not-commonest",
            "first-is-widest",
            "first-is-not-lowest-id",
        ],
    )
    def test_an_empty_index_takes_its_width_from_the_first_report(
        self, reported, expected
    ):
        """Nothing local anchors the width, so the batch establishes it. A
        majority vote would adopt the commonest width and refuse the rest;
        the refused ones are then generated at the model's width, which reads
        as a model change and discards what was just adopted, with nothing
        left to regenerate it. First wins is what the per-operation path did,
        one node at a time."""
        backend = _NotifyingBackend()
        storage = GraphStorage(persistence_backend=backend)
        _stub_generator(storage)
        try:
            assert storage.vector_store.dimension is None, "the index must start empty"

            backend.listener(
                ExternalChange.entities(
                    [
                        EntityOperation.upsert_node(
                            dict(_node_payload(node_id, name), embedding=embedding)
                        )
                        for node_id, name, embedding in reported
                    ]
                )
            )

            for node_id, vector in expected.items():
                got = storage.vector_store.get_vector_list(node_id)
                if vector is None:
                    assert got is None, f"{node_id} kept a vector it should not have"
                else:
                    assert got == pytest.approx(vector), f"{node_id} is wrong"

        finally:
            storage.shutdown_events()

    def test_a_model_narrower_than_the_store_strands_nobody(self):
        """A peer running a different embedding model is the ordinary way the
        two widths disagree. The supplied vector is adopted, generation for
        the rest comes back at the local model's width, and the index is
        emptied of what was adopted - so the settle looks again and generates
        for whoever was left behind."""
        backend = _NotifyingBackend()
        storage = GraphStorage(persistence_backend=backend)

        def wide(nodes):
            storage.vector_store._absorb(
                {node.id: [float(len(node.name))] * 3 for node in nodes}
            )

        storage.vector_store.update_nodes_embeddings = wide
        try:
            backend.listener(
                ExternalChange.entities(
                    [
                        EntityOperation.upsert_node(_node_payload("c", "Cedar")),
                        EntityOperation.upsert_node(
                            dict(_node_payload("a", "Alpha"), embedding=[1.0, 2.0])
                        ),
                    ]
                )
            )

            # Both named, both generation-eligible, so neither is left without.
            assert storage.vector_store.get_vector_list("c") == pytest.approx(
                [5.0, 5.0, 5.0]
            )
            assert storage.vector_store.get_vector_list("a") == pytest.approx(
                [5.0, 5.0, 5.0]
            )
        finally:
            storage.shutdown_events()

    def test_a_replaced_width_still_refuses_a_wrong_one_with_no_generator(self):
        """The anchor's own job, stated where nothing can paper over it. With
        a generator available a refused vector is generated instead, so every
        node ends with one either way and the refusal is invisible. Without
        one - the ML-free install this repo supports as first class - refusing
        is the whole observable, and accepting the wrong width would put a
        vector of a foreign model's shape into the index."""
        backend = _NotifyingBackend()
        storage = GraphStorage(persistence_backend=backend)
        storage.add_nodes(
            [
                Node(id="a", type=NodeType.ACTOR, name="Alpha", embedding=[1.0, 0.0]),
                Node(id="b", type=NodeType.ACTOR, name="Beacon", embedding=[2.0, 0.0]),
            ],
            [],
        )
        storage.flush()
        try:
            # Both ids replaced, so the index empties - and the width it held
            # is still the one to judge against.
            backend.listener(
                ExternalChange.entities(
                    [
                        EntityOperation.upsert_node(
                            dict(
                                _node_payload("a", "Renamed"),
                                embedding=[1.0, 2.0, 3.0],
                            )
                        ),
                        EntityOperation.upsert_node(
                            dict(
                                _node_payload("b", "Rebeacon"),
                                embedding=[4.0, 5.0, 6.0],
                            )
                        ),
                    ]
                )
            )

            for node_id in ("a", "b"):
                assert storage.vector_store.get_vector_list(node_id) is None, (
                    f"{node_id} took a vector of the wrong width"
                )
        finally:
            storage.shutdown_events()

    def test_a_deleted_width_does_not_refuse_a_supplied_vector(self):
        """The anchor exists so a batch that empties the index by replacing
        everything is still judged against the width it replaced. A batch that
        empties it by deleting everything is the other case: the old width
        belongs to nobody, and defending it refuses a supplied vector for
        disagreeing with vectors that are gone."""
        backend = _NotifyingBackend()
        storage = GraphStorage(persistence_backend=backend)
        storage.add_nodes(
            [Node(id="x", type=NodeType.ACTOR, name="Xeno", embedding=[1.0, 0.0])],
            [],
        )
        storage.flush()
        try:
            backend.listener(
                ExternalChange.entities(
                    [
                        EntityOperation.delete_node("x"),
                        EntityOperation.upsert_node(
                            dict(
                                _node_payload("a", "Alpha"),
                                embedding=[1.0, 2.0, 3.0],
                            )
                        ),
                    ]
                )
            )

            assert storage.vector_store.get_vector_list("a") == pytest.approx(
                [1.0, 2.0, 3.0]
            )
        finally:
            storage.shutdown_events()

    def test_generation_embeds_the_node_the_batch_left(self):
        """A rename with no vector of its own has to be embedded from the
        text it now has. Handing generation the node the batch replaced
        instead leaves the index describing text that is gone - and the
        settle runs after the loop, so both nodes are within reach of it."""
        storage, backend = self._storage()
        _stub_generator(storage)
        try:
            backend.listener(
                ExternalChange.entities(
                    [EntityOperation.upsert_node(_node_payload("a", "Alphabetical"))]
                )
            )

            assert storage.get_node("a").name == "Alphabetical"
            assert storage.vector_store.get_vector_list("a") == pytest.approx(
                _generated("Alphabetical")
            )
            assert storage.vector_store.get_vector_list("a") != pytest.approx(
                _generated("Alpha")
            )
        finally:
            storage.shutdown_events()

    def test_a_second_upsert_without_a_vector_generates_rather_than_keeping_the_first(
        self,
    ):
        """Last operation wins on the vector half too. The first operation
        supplied one; the second says nothing, which means generate - not
        carry the first one forward onto text it never described."""
        storage, backend = self._storage()
        _stub_generator(storage)
        try:
            backend.listener(
                ExternalChange.entities(
                    [
                        EntityOperation.upsert_node(
                            dict(_node_payload("a", "First"), embedding=[9.0, 9.0])
                        ),
                        EntityOperation.upsert_node(
                            _node_payload("a", "Second and longer")
                        ),
                    ]
                )
            )

            assert storage.get_node("a").name == "Second and longer"
            assert storage.vector_store.get_vector_list("a") == pytest.approx(
                _generated("Second and longer")
            )
        finally:
            storage.shutdown_events()

    def test_generation_is_batched_too(self):
        """The rebuild bound has to hold for the generated half as well, and
        a batch whose nodes all carry vectors never reaches it."""
        rebuilds = []
        for count in (2, 20):
            storage, backend = self._storage()
            _stub_generator(storage)
            try:
                before = storage.vector_store.revision
                backend.listener(
                    ExternalChange.entities(
                        [
                            EntityOperation.upsert_node(
                                _node_payload(f"g{i}", f"Gen {i}")
                            )
                            for i in range(count)
                        ]
                    )
                )
                rebuilds.append(storage.vector_store.revision - before)
            finally:
                storage.shutdown_events()

        assert rebuilds[0] == rebuilds[1]
        assert rebuilds[1] <= 2

    def test_a_generator_that_fails_does_not_reach_the_backend(self):
        """Containment is what the whole refresh path promises the backend,
        and the absent ML stack raises ImportError - so an `except` narrowed
        to that would look right and let everything else through."""
        storage, backend = self._storage()

        def explode(nodes):
            raise RuntimeError("the model went away")

        storage.vector_store.update_nodes_embeddings = explode
        try:
            backend.listener(
                ExternalChange.entities(
                    [EntityOperation.upsert_node(_node_payload("b", "Beacon"))]
                )
            )

            # Contained: the upsert stands, and nothing was raised at us.
            assert storage.get_node("b").name == "Beacon"
        finally:
            storage.shutdown_events()


class TestExternalRefreshSettlesTheVectorIndexOnce:
    """The index matrix is rebuilt whole on every change to it, so the cost of
    a refresh is decided by how many times a batch changes it, not by how big
    the batch is."""

    def _storage_with(self, backend, count):
        storage = GraphStorage(persistence_backend=backend)
        storage.add_nodes(
            [
                Node(
                    id=f"n{i}",
                    type=NodeType.ACTOR,
                    name=f"Node {i}",
                    embedding=[float(i), 0.5],
                )
                for i in range(count)
            ],
            [],
        )
        storage.flush()
        return storage

    @staticmethod
    def _report(storage, backend, count):
        """Report `count` node upserts as one batch; return the rebuild count."""
        before = storage.vector_store.revision
        backend.listener(
            ExternalChange.entities(
                [
                    EntityOperation.upsert_node(
                        dict(
                            _node_payload(f"n{i}", f"Renamed {i}"), embedding=[9.0, 9.0]
                        )
                    )
                    for i in range(count)
                ]
            )
        )
        return storage.vector_store.revision - before

    def test_a_batch_that_uses_every_pass_costs_exactly_three(self):
        """The constant the docs teach a backend author. Both other counting
        tests are built so only two of the three passes fire - one supplies a
        vector for every node so generation never runs, the other names only
        new ids so the eviction changes nothing - and a batch that evicts,
        adopts and generates was never measured."""
        backend = _NotifyingBackend()
        storage = GraphStorage(persistence_backend=backend)
        storage.add_nodes(
            [
                Node(id="a", type=NodeType.ACTOR, name="Alpha", embedding=[1.0, 0.0]),
                Node(id="b", type=NodeType.ACTOR, name="Beacon", embedding=[2.0, 0.0]),
            ],
            [],
        )
        storage.flush()
        _stub_generator(storage)
        try:
            before = storage.vector_store.revision
            backend.listener(
                ExternalChange.entities(
                    [
                        EntityOperation.upsert_node(
                            dict(_node_payload("a", "Renamed"), embedding=[9.0, 9.0])
                        ),
                        EntityOperation.upsert_node(_node_payload("c", "Cedar")),
                        EntityOperation.delete_node("b"),
                    ]
                )
            )

            # Eviction, adoption, generation - one pass each.
            assert storage.vector_store.revision - before == 3
        finally:
            storage.shutdown_events()

    def test_the_index_is_rebuilt_the_same_number_of_times_whatever_the_batch(self):
        """The property, stated as a count rather than a clock: rebuilding per
        reported node is linear in the index per operation, so a batch costs
        the square of it. Two batches of very different sizes must cost the
        index the same."""
        small_backend = _NotifyingBackend()
        small = self._storage_with(small_backend, 20)
        large_backend = _NotifyingBackend()
        large = self._storage_with(large_backend, 20)
        try:
            rebuilds_for_two = self._report(small, small_backend, 2)
            rebuilds_for_twenty = self._report(large, large_backend, 20)

            assert rebuilds_for_two == rebuilds_for_twenty
            # One eviction pass and one adoption is the whole cost.
            assert rebuilds_for_twenty <= 2
        finally:
            small.shutdown_events()
            large.shutdown_events()

    def test_the_batch_still_leaves_every_vector_where_it_belongs(self):
        """Settling once must land exactly what settling per node did: the
        reported vectors in, the replaced ones out."""
        backend = _NotifyingBackend()
        storage = self._storage_with(backend, 3)
        try:
            self._report(storage, backend, 2)

            assert storage.vector_store.get_vector_list("n0") == pytest.approx(
                [9.0, 9.0]
            )
            assert storage.vector_store.get_vector_list("n1") == pytest.approx(
                [9.0, 9.0]
            )
            # Untouched by the batch, so untouched in the index.
            assert storage.vector_store.get_vector_list("n2") == pytest.approx(
                [2.0, 0.5]
            )
        finally:
            storage.shutdown_events()

    def test_the_last_operation_for_an_id_decides_what_the_index_keeps(self):
        """A batch can touch the same id twice, and the store applied them in
        order. Settling at the end must land the last one, not the first."""
        for operations, expected in (
            (["upsert", "delete"], None),
            (["delete", "upsert"], [9.0, 9.0]),
        ):
            backend = _NotifyingBackend()
            storage = self._storage_with(backend, 2)
            try:
                upsert = EntityOperation.upsert_node(
                    dict(_node_payload("n0", "Renamed"), embedding=[9.0, 9.0])
                )
                delete = EntityOperation.delete_node("n0")
                backend.listener(
                    ExternalChange.entities(
                        [upsert if name == "upsert" else delete for name in operations]
                    )
                )

                got = storage.vector_store.get_vector_list("n0")
                if expected is None:
                    assert got is None, f"{operations} left a vector behind"
                    assert storage.get_node("n0") is None
                else:
                    assert got == pytest.approx(expected), f"{operations} lost it"
                    assert storage.get_node("n0") is not None
            finally:
                storage.shutdown_events()


class TestExternalRefreshSettlesEvenWhenTheBatchFails:
    """A reload supersedes the settle - but _reload_from_store deliberately
    does not land in two cases, and then the applied prefix is what this
    instance serves."""

    def _storage(self):
        backend = _NotifyingBackend()
        storage = GraphStorage(persistence_backend=backend)
        storage.add_nodes(
            [
                Node(
                    id="a",
                    type=NodeType.ACTOR,
                    name="Alpha",
                    embedding=[1.0, 0.0],
                )
            ],
            [],
        )
        storage.flush()
        return storage, backend

    def test_a_half_applied_batch_whose_reload_cannot_land_still_settles(self):
        """The store reports it is not there - another writer emptied it, a
        restore is in progress - so the reload declines rather than writing
        this instance's graph over it. What applied before the batch failed
        stays in memory, and its vectors must describe it: a renamed node
        left with the vector for its old text is the stale hit the refresh
        exists to prevent."""
        storage, backend = self._storage()
        try:
            backend.written = False

            backend.listener(
                ExternalChange.entities(
                    [
                        EntityOperation.upsert_node(
                            dict(_node_payload("a", "Renamed"), embedding=[9.0, 9.0])
                        ),
                        EntityOperation(
                            kind="node",
                            action="nonsense",
                            entity_id="a",
                            payload=None,
                        ),
                    ]
                )
            )

            assert storage.get_node("a").name == "Renamed"
            current = storage.vector_store.get_vector_list("a")
            assert current is None or current != pytest.approx([1.0, 0.0]), (
                "the applied prefix kept the vector for the text it no longer has"
            )
        finally:
            storage.shutdown_events()

    def test_a_settle_that_itself_fails_is_contained_and_still_reloads(self):
        """The settle is wrapped for the same reason the loop is: everything
        in it reaches numpy, and a raise there would land in the backend's
        thread, where the instance that made the write reads it as its own
        write having failed. Containing it is only half the answer - the
        batch is half settled, so the reload still has to run."""
        storage, backend = self._storage()
        try:

            def explode(node_ids):
                raise RuntimeError("the index went away")

            storage.vector_store.remove_nodes_embeddings = explode
            try:
                backend.listener(
                    ExternalChange.entities(
                        [EntityOperation.upsert_node(_node_payload("a", "Renamed"))]
                    )
                )
            finally:
                del storage.vector_store.remove_nodes_embeddings

            # Reloaded, so memory is the store's copy rather than the
            # half-settled prefix - a node renamed in memory carrying the
            # vector for the text it no longer has.
            assert storage.get_node("a").name == "Alpha"
        finally:
            storage.shutdown_events()

    def test_a_reload_that_does_land_is_not_undone_by_the_settle(self):
        """The other side of the same window. When the reload lands it has
        rebuilt the index from the store, so a settle running after it would
        evict exactly what the reload just restored."""
        backend = _NotifyingBackend()
        backend.save_graph_data(
            {
                "nodes": [
                    dict(_node_payload("a", "Alpha"), embedding=[0.25, 0.5]),
                    dict(_node_payload("c", "Cedar"), embedding=[0.75, 0.5]),
                ],
                "edges": [],
                "metadata": {"version": "1.0", "graph_name": "g"},
            }
        )
        storage = GraphStorage(persistence_backend=backend)
        try:
            assert storage.vector_store.get_vector_list("c") == pytest.approx(
                [0.75, 0.5]
            )

            backend.listener(
                ExternalChange.entities(
                    [
                        EntityOperation.upsert_node(
                            dict(_node_payload("c", "Renamed"), embedding=[9.0, 9.0])
                        ),
                        EntityOperation(
                            kind="node",
                            action="nonsense",
                            entity_id="c",
                            payload=None,
                        ),
                    ]
                )
            )

            # The reload put the store's graph back, vectors included.
            assert storage.get_node("c").name == "Cedar"
            assert storage.vector_store.get_vector_list("c") == pytest.approx(
                [0.75, 0.5]
            )
        finally:
            storage.shutdown_events()

    def test_a_deleted_node_leaves_no_vector_when_the_reload_cannot_land(self):
        """The same window, the other operation: an orphan vector survives
        into the sidecar on the next save and takes an over-fetch slot."""
        storage, backend = self._storage()
        try:
            backend.written = False

            backend.listener(
                ExternalChange.entities(
                    [
                        EntityOperation.delete_node("a"),
                        EntityOperation(
                            kind="node",
                            action="nonsense",
                            entity_id="a",
                            payload=None,
                        ),
                    ]
                )
            )

            assert storage.get_node("a") is None
            assert storage.vector_store.get_vector_list("a") is None
        finally:
            storage.shutdown_events()


class TestExternalRefreshEventsCarryNoVector:
    """The history store strips `embedding` by name, because a vector belongs
    in the sidecar rather than in every mutation record. Webhook and agent
    payloads have no such filter, so the refresh must not hand them one."""

    def test_an_external_upsert_emits_no_embedding(self):
        backend = _NotifyingBackend()
        storage = GraphStorage(persistence_backend=backend)
        seen = []
        storage.add_system_listener(seen.append)
        try:
            backend.listener(
                ExternalChange.entities(
                    [
                        EntityOperation.upsert_node(
                            dict(_node_payload("b", "Beacon"), embedding=[9.0, 9.0])
                        )
                    ]
                )
            )

            assert [e.event_type for e in seen] == [EventType.NODE_CREATE]
            assert seen[0].entity.after.get("embedding") is None
            # And it did land where it belongs.
            assert storage.vector_store.get_vector_list("b") == pytest.approx(
                [9.0, 9.0]
            )
        finally:
            storage.shutdown_events()

    def test_a_delete_in_the_same_batch_emits_no_embedding_either(self):
        """`before` is built from the node the batch put there, so it carries
        the vector too unless the upsert took it off."""
        backend = _NotifyingBackend()
        storage = GraphStorage(persistence_backend=backend)
        seen = []
        storage.add_system_listener(seen.append)
        try:
            backend.listener(
                ExternalChange.entities(
                    [
                        EntityOperation.upsert_node(
                            dict(_node_payload("b", "Beacon"), embedding=[9.0, 9.0])
                        ),
                        EntityOperation.delete_node("b"),
                    ]
                )
            )

            assert [e.event_type for e in seen] == [
                EventType.NODE_CREATE,
                EventType.NODE_DELETE,
            ]
            assert seen[1].entity.before.get("embedding") is None
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

    def test_a_delete_drops_a_vector_the_graph_file_still_carries_inline(self):
        """The delete half of the same bookkeeping. Left in the fallback, the
        departed node's vector is written back out under whatever is next
        created with that id - a dead vector resurrected into the store and
        served by semantic search."""
        backend = _NotifyingBackend()
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
                ExternalChange.entities([EntityOperation.delete_node("c")])
            )
            assert "c" not in storage._inline_fallback

            # The id comes back as a different node. A local add does not
            # touch the fallback map, so whatever is left in it is what gets
            # written out for the newcomer.
            storage.add_nodes([Node(id="c", type=NodeType.ACTOR, name="Cypress")], [])
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
            with pytest.raises(ExternalChangeRefused, match="backend's own thread"):
                refused.result(timeout=30)

            # And the queue still works: refusing is not wedging.
            storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="Alpha")], [])
            storage.flush()
            assert storage.get_node("a") is not None
        finally:
            storage.shutdown_events()

    @pytest.mark.parametrize("write", ["entity", "snapshot"])
    def test_a_backend_reporting_from_inside_its_write_does_not_wedge(self, write):
        """The realistic shape of the same mistake: the report comes from
        inside the write that made it. What must not happen is the queue
        stopping forever - nor the refusal being read as the write having
        failed, which would answer a backend's reporting bug by re-issuing
        the whole graph over a store that has another writer in it.

        Both write paths, because there are two and they contain the refusal
        separately: an entity write and a whole-graph one."""
        backend = _NotifyingBackend(incremental=(write == "entity"))
        storage = GraphStorage(persistence_backend=backend)
        try:
            storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="Alpha")], [])
            storage.flush()
            backend.report_from_writes = ExternalChange.unknown()
            backend.snapshots = 0

            storage.update_node("a", {"name": "Renamed"})
            drain = threading.Thread(target=storage.flush, daemon=True)
            drain.start()
            drain.join(30)
            assert not drain.is_alive(), "the write queue wedged on itself"

            backend.report_from_writes = None
            assert storage.get_node("a").name == "Renamed"
            # The write itself landed - the backend reported after applying
            # it - so nothing is owed to the store. Reading the refusal as a
            # failed write would answer a backend's reporting bug by
            # re-issuing the whole graph over a store that has another writer
            # in it, so no re-issue may have been queued.
            assert backend.nodes["a"]["name"] == "Renamed"
            assert not storage._resync_pending
            assert backend.snapshots == (0 if write == "entity" else 1)
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

    @pytest.mark.parametrize(
        "source, target", [("a", "ghost"), ("ghost", "a")], ids=["target", "source"]
    )
    def test_an_edge_with_an_absent_endpoint_is_added_nowhere(self, source, target):
        """NetworkX would invent the missing endpoint as a node with no data,
        and every read path walking the graph would trip over it.

        Either end: a guard that checked only one of them would invent the
        node whenever the edge pointed the other way."""
        storage, backend = self._storage()
        try:
            backend.listener(
                ExternalChange.entities(
                    [EntityOperation.upsert_edge(_edge_payload("ax", source, target))]
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

    @pytest.mark.parametrize(
        "ends", [("a", "ghost"), ("ghost", "b")], ids=["target", "source"]
    )
    def test_an_edge_repointed_at_an_absent_endpoint_keeps_serving_the_old_one(
        self, ends
    ):
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
                    [EntityOperation.upsert_edge(_edge_payload("ab", ends[0], ends[1]))]
                )
            )

            assert [e.target for e in storage.get_all_edges()] == ["b"]
            assert [e.id for e in storage.get_edges_for_node("b")] == ["ab"]
            assert not storage.graph.has_node("ghost")
            assert len(storage.graph.edges) == 1
        finally:
            storage.shutdown_events()

    def test_an_edge_from_a_node_to_itself_arrives_whole(self):
        """A self-loop has one endpoint, not two, and both the guard and the
        removal of a moved edge read source and target as if they differed."""
        storage, backend = self._storage()
        try:
            backend.listener(
                ExternalChange.entities(
                    [EntityOperation.upsert_edge(_edge_payload("aa", "a", "a"))]
                )
            )

            assert [e.id for e in storage.get_all_edges()] == ["aa"]
            assert [e.id for e in storage.get_edges_for_node("a")] == ["aa"]
            assert storage.graph.number_of_edges() == 1
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

    @pytest.mark.parametrize("unreadable", ["node", "edge"])
    def test_a_reload_that_cannot_read_the_store_leaves_the_graph_whole(
        self, unreadable
    ):
        """The store is readable but holds an entity this build is not: a
        rolling upgrade. Clearing the graph and then failing to refill it
        would leave every read path short of entities the store still has,
        reported as merely being behind it.

        The unreadable entity goes *first*, before anything this build can
        read. Put it last and a reload that clears as it goes still ends up
        with the right contents by accident, and the test passes against the
        very failure it is here for."""
        backend = _NotifyingBackend()
        storage = GraphStorage(persistence_backend=backend)
        try:
            storage.add_nodes(
                [
                    Node(id="a", type=NodeType.ACTOR, name="Alpha"),
                    Node(id="b", type=NodeType.ACTOR, name="Beacon"),
                ],
                [Edge(id="ab", source="a", target="b", type="RELATES_TO")],
            )
            storage.flush()
            # A node the instance does not hold. Without it the store's node
            # set equals memory's, and a swap torn between the two parse
            # loops leaves the same model behind as one that never ran.
            backend.nodes["z"] = _node_payload("z", "Zulu")
            if unreadable == "node":
                backend.nodes = {
                    "bad": {"id": "bad", "type": "Nonsense"},
                    **backend.nodes,
                }
            else:
                backend.edges = {"bad": {"id": "bad"}, **backend.edges}

            backend.listener(ExternalChange.unknown())

            assert {n.id for n in storage.get_all_nodes()} == {"a", "b"}
            # Every container moved together or none of them did.
            assert set(storage.graph.nodes) == {"a", "b"}
            assert set(storage._searchable_text_cache) == {"a", "b"}
            assert [n.id for n in storage.search_nodes("Beacon")] == ["b"]
            assert set(storage.graph.nodes) == {"a", "b"}
            # The edge half fails out of sight of get_all_edges, which reads
            # a dictionary; a torn reload shows as the two disagreeing.
            assert {e.id for e in storage.get_all_edges()} == {"ab"}
            assert storage.graph.number_of_edges() == 1
        finally:
            storage.shutdown_events()

    def test_a_refresh_over_a_failed_write_neither_drops_it_nor_writes(self):
        """The other half of the in-flight case: the write did not just not
        land yet, it failed, so a mutation is in memory and nowhere else.
        Reloading drops it. Writing it first is worse - the only write that
        carries it is the whole graph, and the store being refreshed from is
        one another writer is committing to. So the refresh does neither: it
        stops, and leaves both sides intact."""
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

            # What the other writer committed while our write was failing.
            backend.nodes["z"] = _node_payload("z", "Zulu")

            backend.listener(ExternalChange.unknown())

            # The refresh wrote nothing: what the other writer committed is
            # intact, and our own mutation is still owed to the store.
            assert set(backend.nodes) == {"a", "z"}
            # And it dropped nothing: the accepted mutation is still in
            # memory, still flagged as owed.
            assert {n.id for n in storage.get_all_nodes()} == {"a", "c"}
            assert storage._resync_pending
        finally:
            storage.shutdown_events()

    def test_an_external_write_does_not_undo_a_newer_local_one(self):
        """Both instances wrote the same node. The store keeps whichever
        write reached it last - ours, here - and we are never told about our
        own, so applying a report that predates it would leave this instance
        serving a value the store does not hold, with nothing left to report
        that would correct it."""
        proceed = threading.Event()
        backend = _NotifyingBackend()
        real_upsert = backend.upsert_node

        def slow_upsert(node):
            proceed.wait()
            real_upsert(node)

        storage = GraphStorage(persistence_backend=backend)
        try:
            storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="Alpha")], [])
            storage.flush()
            backend.upsert_node = slow_upsert
            storage.update_node("a", {"name": "Local"})

            stale = _node_payload("a", "External")
            stale["updated_at"] = "2020-01-01T00:00:00+00:00"
            refresh = threading.Thread(
                target=storage.apply_external_change,
                args=(ExternalChange.entities([EntityOperation.upsert_node(stale)]),),
            )
            refresh.start()
            proceed.set()
            refresh.join(5)
            assert not refresh.is_alive()
            storage.flush()

            assert storage.get_node("a").name == "Local"
            assert backend.nodes["a"]["name"] == "Local"
            assert [n.id for n in storage.search_nodes("External")] == []
        finally:
            proceed.set()
            storage.shutdown_events()

    @pytest.mark.parametrize("shape", ["serialised", "legacy"])
    def test_a_reported_payload_is_read_not_taken(self, shape):
        """The dict a report carries belongs to the backend, which may still
        be holding the record it reported - a poller's cache, a payload kept
        to retry. Parsing it in place would hand back something the backend
        did not give.

        Two shapes, because from_dict rewrites its argument two ways and one
        payload only shows one of them: it parses string stamps, and it fills
        in defaults for keys that are absent. A payload carrying every key
        already - the shape this instance itself writes - can only show the
        first."""
        backend = _NotifyingBackend()
        storage = GraphStorage(persistence_backend=backend)
        try:
            storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="Alpha")], [])
            storage.flush()

            node = _node_payload("b", "Beacon")
            edge = _edge_payload("ab", "a", "b")
            if shape == "legacy":
                # A record of the backend's own: stamps already parsed, and
                # none of the keys from_dict would fill in.
                stamp = datetime.now(timezone.utc)
                node["created_at"] = node["updated_at"] = stamp
                edge["created_at"] = stamp
                del node["archived"], edge["archived"], edge["label"]
            kept = (dict(node), dict(edge))
            keys = (set(node), set(edge))

            backend.listener(
                ExternalChange.entities(
                    [
                        EntityOperation.upsert_node(node),
                        EntityOperation.upsert_edge(edge),
                    ]
                )
            )
            assert storage.get_node("b") is not None

            assert (node, edge) == kept
            # Nothing added either: from_dict fills in defaults, and a copy
            # taken only when a stamp needs parsing would let that half by.
            assert (set(node), set(edge)) == keys
        finally:
            storage.shutdown_events()

    def test_an_ignored_external_write_changes_nothing_and_says_nothing(self):
        """An ignored report moved nothing, so there is nothing to announce.
        An event for it would tell subscribers of a change this instance is
        not serving - and would be a before equal to its own after."""
        backend = _NotifyingBackend()
        storage = GraphStorage(persistence_backend=backend)
        seen = []
        storage.add_system_listener(seen.append)
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
            seen.clear()

            stale = _node_payload("a", "External")
            stale["updated_at"] = "2020-01-01T00:00:00+00:00"
            backend.listener(
                ExternalChange.entities([EntityOperation.upsert_node(stale)])
            )

            assert seen == []
            assert storage.get_node("a").name == "Alpha"
            assert storage.vector_store.get_vector_list("a") == pytest.approx(
                [0.5, 0.25]
            )
            assert [n.id for n in storage.search_nodes("Alpha")] == ["a"]
        finally:
            storage.shutdown_events()

    @pytest.mark.parametrize("shape", ["tie", "incomparable"])
    def test_a_report_that_cannot_be_ruled_older_is_applied(self, shape):
        """The two directions the rule resolves toward the report: a tie is
        unresolvable, and a pair that cannot be compared at all is not an
        answer. Both take the store's side, which is what converges the two
        instances."""
        backend = _NotifyingBackend()
        storage = GraphStorage(persistence_backend=backend)
        seen = []
        try:
            storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="Alpha")], [])
            storage.flush()
            storage.add_system_listener(seen.append)

            payload = _node_payload("a", "External")
            if shape == "tie":
                payload["updated_at"] = storage.get_node("a").updated_at.isoformat()
            else:
                # Naive against the aware stamp the model holds: a backend
                # handing over datetime objects of its own produces this.
                payload["updated_at"] = datetime.now().replace(tzinfo=None)
            backend.listener(
                ExternalChange.entities([EntityOperation.upsert_node(payload)])
            )

            assert storage.get_node("a").name == "External"
            # Applied, so announced: a branch that returned the other way
            # would be silent here as well as wrong.
            assert [e.event_type for e in seen] == [EventType.NODE_UPDATE]
        finally:
            storage.shutdown_events()

    def test_a_payload_with_no_stamp_is_stamped_now_not_treated_as_absent(self):
        """The rule is written down as if a missing stamp were a gap the
        comparison sees. It is not: the model fills one in at parse time,
        stamped now. Normally that makes the report the newer one - and
        against a held stamp dated in the future, which is what a
        clock-skewed peer produces, it makes it the older one instead. Both
        halves are the documented behaviour, so both are pinned."""
        backend = _NotifyingBackend()
        storage = GraphStorage(persistence_backend=backend)
        try:
            storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="Alpha")], [])
            storage.flush()

            unstamped = _node_payload("a", "External")
            del unstamped["updated_at"]
            backend.listener(
                ExternalChange.entities([EntityOperation.upsert_node(unstamped)])
            )
            assert storage.get_node("a").name == "External"

            # Now the held stamp is ahead of any clock the report can carry.
            storage.nodes["a"].updated_at = datetime.now(timezone.utc) + timedelta(
                minutes=5
            )
            later = _node_payload("a", "Ignored")
            del later["updated_at"]
            backend.listener(
                ExternalChange.entities([EntityOperation.upsert_node(later)])
            )
            assert storage.get_node("a").name == "External"
        finally:
            storage.shutdown_events()

    def test_a_payload_whose_stamp_is_null_is_unreadable_not_undated(self):
        """The other reading of "missing". An explicit null fails validation
        rather than reaching the comparison, and an unreadable payload is a
        whole-graph reload - which is a different outcome from applying the
        report, so the doc has to say which one it is."""
        backend = _NotifyingBackend()
        storage = GraphStorage(persistence_backend=backend)
        try:
            storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="Alpha")], [])
            storage.flush()
            backend.nodes["z"] = _node_payload("z", "Zulu")

            nulled = _node_payload("a", "External")
            nulled["updated_at"] = None
            backend.listener(
                ExternalChange.entities([EntityOperation.upsert_node(nulled)])
            )

            # Reloaded from the store rather than applied: the other writer's
            # node is here, and the unreadable payload's name is not.
            assert {n.id for n in storage.get_all_nodes()} == {"a", "z"}
            assert storage.get_node("a").name == "Alpha"
        finally:
            storage.shutdown_events()

    def test_an_external_write_newer_than_ours_still_applies(self):
        """The other side of it: last writer wins, so a report that postdates
        what we hold is applied, not treated as a conflict to keep out."""
        backend = _NotifyingBackend()
        storage = GraphStorage(persistence_backend=backend)
        try:
            storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="Alpha")], [])
            storage.flush()

            fresh = _node_payload("a", "External")
            fresh["updated_at"] = "2099-01-01T00:00:00+00:00"
            backend.listener(
                ExternalChange.entities([EntityOperation.upsert_node(fresh)])
            )

            assert storage.get_node("a").name == "External"
            assert [n.id for n in storage.search_nodes("External")] == ["a"]
        finally:
            storage.shutdown_events()

    def test_a_named_operation_settles_the_same_as_a_reload(self):
        """Both halves of a refresh go through the same settle, not just the
        reload half. A report naming entities, applied while a local write of
        ours had failed, takes the entity it names out of memory - where our
        mutation is the only copy, the write having failed."""
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

            backend.listener(
                ExternalChange.entities([EntityOperation.delete_node("c")])
            )

            assert {n.id for n in storage.get_all_nodes()} == {"a", "c"}
            assert set(backend.nodes) == {"a"}
        finally:
            storage.shutdown_events()

    def test_the_settle_runs_under_the_lock(self):
        """Waiting for the queue is only enough while nothing new can be
        queued behind it. Settle outside the lock and a write accepted
        between the drain and the refresh is reloaded away in memory and
        then lands in the store - and nothing reports our own write back to
        us, so that disagreement is for good."""
        backend = _NotifyingBackend()
        storage = GraphStorage(persistence_backend=backend)
        settled_without_the_lock = []
        original = storage._settle_before_refresh

        def watched():
            def probe():
                got = storage._lock.acquire(blocking=False)
                settled_without_the_lock.append(got)
                if got:
                    storage._lock.release()

            # From another thread: _lock is reentrant, so this one holds it
            # already if the caller does.
            elsewhere = threading.Thread(target=probe)
            elsewhere.start()
            elsewhere.join(5)
            return original()

        storage._settle_before_refresh = watched
        # The settle mutates the index, so it needs the lock for the same
        # reason the drain does: a reader let in mid-batch sees neither the
        # old index nor the new one.
        settle = storage._settle_vector_index

        def watched_settle(touched):
            def probe():
                got = storage._lock.acquire(blocking=False)
                settled_without_the_lock.append(got)
                if got:
                    storage._lock.release()

            elsewhere = threading.Thread(target=probe)
            elsewhere.start()
            elsewhere.join(5)
            return settle(touched)

        storage._settle_vector_index = watched_settle
        try:
            storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="Alpha")], [])
            storage.flush()

            backend.listener(ExternalChange.unknown())
            backend.listener(
                ExternalChange.entities(
                    [EntityOperation.upsert_node(_node_payload("b", "Beacon"))]
                )
            )

            # Three probes: the drain on each report, and the settle on the
            # one that named entities. The reload path returns before there
            # is anything to settle.
            assert settled_without_the_lock == [False, False, False]
        finally:
            storage._settle_before_refresh = original
            storage._settle_vector_index = settle
            storage.shutdown_events()

    @pytest.mark.parametrize("change", _RESYNCING_CHANGES, ids=_RESYNC_IDS)
    def test_a_refresh_does_not_drop_a_write_that_fails_during_the_drain(self, change):
        """The failure lands between the two halves of the guard: the write
        is still queued when the report arrives, so nothing is flagged yet,
        and it fails while the refresh is already waiting for it. A guard
        read before the drain passes on a value that stopped being true
        before the refresh used it, and the accepted mutation is lost - from
        memory, and from the store, which never got it."""
        proceed = threading.Event()
        backend = _NotifyingBackend()

        def slow_refusal(node):
            proceed.wait()
            raise OSError("the write failed")

        storage = GraphStorage(persistence_backend=backend)
        try:
            storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="Alpha")], [])
            storage.flush()
            backend.upsert_node = slow_refusal
            storage.add_nodes([Node(id="c", type=NodeType.ACTOR, name="Cedar")], [])
            # What the other writer committed while our write was queued.
            backend.nodes["z"] = _node_payload("z", "Zulu")

            refresh = threading.Thread(
                target=storage.apply_external_change, args=(change(),)
            )
            refresh.start()
            refresh.join(0.5)
            assert refresh.is_alive(), "the refresh went ahead without draining"

            proceed.set()
            refresh.join(5)
            assert not refresh.is_alive()

            assert storage._resync_pending, "the failed write did not raise the flag"
            assert {n.id for n in storage.get_all_nodes()} == {"a", "c"}
            assert set(backend.nodes) == {"a", "z"}
        finally:
            proceed.set()
            del backend.upsert_node
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
            # The store has moved on. Without this, "skipped the operation"
            # and "resynced from the store" leave the same model behind, and
            # the assertions below cannot tell them apart.
            backend.nodes["z"] = _node_payload("z", "Zulu")

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

            # Reloaded, not skipped: what the other writer committed is
            # here. Skipping the operation would leave the model exactly as
            # it was, which is what this used to assert and could not
            # distinguish from a resync.
            assert {n.id for n in storage.get_all_nodes()} == {"a", "b", "z"}
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
        """The listener is called from a thread of the backend's own - never
        the thread the write queue runs on - so a refresh runs alongside
        local mutations and must not tear the model."""
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

    def test_add_nodes_with_no_nodes_still_snapshots_on_a_snapshot_backend(self):
        """A snapshot-only backend has no notion of "nothing changed" -
        _persist() always calls save() for it, empty operations list or not.
        add_nodes([], edges) still runs the nodes phase (empty) and the edges
        phase, so it snapshots twice, the same as a non-empty call."""
        backend = _SnapshotBackend()
        storage = _storage(backend)
        storage.add_nodes([_node("a"), _node("b")], [])
        storage.flush()
        snapshots_before = backend.snapshots

        result = storage.add_nodes([], [_edge("e", "a", "b")])
        storage.flush()

        assert result.success
        assert backend.snapshots == snapshots_before + 2
        assert {e["id"] for e in backend.data["edges"]} == {"e"}

    def test_delete_edges_with_no_ids_still_snapshots_on_a_snapshot_backend(self):
        backend = _SnapshotBackend()
        storage = _storage(backend)
        storage.add_nodes([_node("a"), _node("b")], [_edge("e", "a", "b")])
        storage.flush()
        snapshots_before = backend.snapshots

        result = storage.delete_edges([])
        storage.flush()

        assert result.success
        assert backend.snapshots == snapshots_before + 1
        assert {e["id"] for e in backend.data["edges"]} == {"e"}


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

    def test_deleting_several_edges_in_one_call_is_one_batch(self):
        """The single-operation case is pinned below
        (test_a_single_operation_uses_its_own_method_not_a_batch,
        delete_edges-of-one); a multi-edge delete_edges call was unexercised."""
        backend = _IncrementalBackend()
        storage = _storage(backend)
        storage.add_nodes(
            [_node("a"), _node("b")], [_edge("e", "a", "b"), _edge("f", "b", "a")]
        )
        storage.flush()
        backend.calls.clear()

        result = storage.delete_edges(["e", "f"])
        storage.flush()

        assert result.success
        assert _kinds(backend) == ["apply_batch"]
        batch = backend.calls[0][1]
        assert [(op.kind, op.action, op.entity_id) for op in batch] == [
            ("edge", "delete", "e"),
            ("edge", "delete", "f"),
        ]
        assert backend.edges == {}
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

    def test_a_rejected_duplicate_node_does_not_overwrite_the_original_payload(self):
        """The two duplicate-id cases above send a payload identical to the
        original, so a regression that appended to unpersisted_nodes BEFORE
        the `node.id in self.nodes` check (rather than after, as the code
        does) would still pass: the duplicate's payload is indistinguishable
        from what is already stored. Giving the duplicate a different name
        closes that: if the rejected duplicate were persisted anyway, both
        memory and the backend would show the duplicate's name, not the
        original's."""
        backend = _IncrementalBackend()
        storage = _storage(backend)
        storage.add_nodes([_node("a"), _node("b")], [_edge("e", "a", "b")])
        storage.flush()
        backend.calls.clear()

        duplicate = Node(id="a", type=NodeType.ACTOR, name="Other")
        result = storage.add_nodes([duplicate], [])
        storage.flush()

        assert not result.success and "already exists" in result.message
        assert backend.calls == []
        assert storage.nodes["a"].name == "A"
        assert backend.nodes["a"]["name"] == "A"

    def test_a_rejected_duplicate_edge_does_not_overwrite_the_original_payload(self):
        """Same gap as the node case above, for edges: the duplicate id here
        arrives reversed (source and target swapped) rather than payload-
        identical, so a regression that persists the rejected duplicate would
        leave the store pointing the wrong way."""
        backend = _IncrementalBackend()
        storage = _storage(backend)
        storage.add_nodes([_node("a"), _node("b")], [_edge("e", "a", "b")])
        storage.flush()
        backend.calls.clear()

        duplicate = Edge(id="e", source="b", target="a")
        result = storage.add_nodes([], [duplicate])
        storage.flush()

        assert not result.success and "already exists" in result.message
        assert backend.calls == []
        assert storage.edges["e"].source == "a" and storage.edges["e"].target == "b"
        assert (
            backend.edges["e"]["source"] == "a" and backend.edges["e"]["target"] == "b"
        )

    def test_an_edge_rejected_by_applicability_after_a_good_one_is_never_persisted(
        self,
    ):
        """No incremental-backend test exercised the applicability exit
        (_validate_edge_applicability, raised after the edge has been
        resolved but before it is added to self.edges/unpersisted_edges): a
        regression that added the edge to unpersisted_edges before validating
        it would leak the rejected edge into the store."""
        backend = _IncrementalBackend()
        storage = _storage(backend)
        storage.add_nodes(
            [
                Node(id="i", type=NodeType.INITIATIVE, name="Init"),
                Node(id="a", type=NodeType.ACTOR, name="Actor"),
            ],
            [],
        )
        storage.flush()
        backend.calls.clear()

        good = Edge(id="good", source="i", target="a", type=RelationshipType.RELATES_TO)
        bad = Edge(id="bad", source="i", target="a", type=RelationshipType.IMPLEMENTS)
        result = storage.add_nodes([], [good, bad])
        storage.flush()

        assert not result.success
        assert set(storage.edges) == {"good"} == set(backend.edges)
        assert _kinds(backend) == ["upsert_edge"]
        assert backend.calls[0][1]["id"] == "good"

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

    def test_a_duplicate_first_node_writes_nothing_more_to_a_snapshot_backend(self):
        """persist_landed's empty guard: when the very first entity in the
        batch is itself the duplicate, nothing landed before the rejection.
        Dropping the guard would have a snapshot backend re-save the
        unchanged graph on every such failure."""
        backend = _SnapshotBackend()
        storage = _storage(backend)
        storage.add_nodes([_node("a")], [])
        storage.flush()
        snapshots_before = backend.snapshots

        result = storage.add_nodes([_node("a")], [])
        storage.flush()

        assert not result.success and "already exists" in result.message
        assert backend.snapshots == snapshots_before

    @pytest.mark.parametrize(
        "nodes, edges, expect_nodes, expect_edges, expect_snapshots",
        [
            ([_node("c"), _node("a")], [], {"a", "b", "c"}, {"e"}, 1),
            # An empty `nodes` list still triggers the nodes-phase persist
            # call unconditionally (pinned separately in
            # test_add_nodes_with_no_nodes_still_snapshots_on_a_snapshot_backend),
            # so this case gets that snapshot plus persist_landed's - two,
            # not one.
            (
                [],
                [_edge("e2", "a", "b"), _edge("e", "a", "b")],
                {"a", "b"},
                {"e", "e2"},
                2,
            ),
        ],
        ids=["duplicate-node-id", "duplicate-edge-id"],
    )
    def test_a_rejected_duplicate_id_with_something_landed_saves_on_a_snapshot_backend(
        self, nodes, edges, expect_nodes, expect_edges, expect_snapshots
    ):
        """The duplicate-id exits are pinned above against the incremental
        backend only. Here something lands before the rejected duplicate, so
        (unlike the all-duplicate case above) persist_landed's guard does not
        apply and the snapshot backend gets what it landed."""
        backend = _SnapshotBackend()
        storage = _storage(backend)
        storage.add_nodes([_node("a"), _node("b")], [_edge("e", "a", "b")])
        storage.flush()
        snapshots_before = backend.snapshots

        result = storage.add_nodes(nodes, edges)
        storage.flush()

        assert not result.success and "already exists" in result.message
        assert backend.snapshots == snapshots_before + expect_snapshots
        assert {n["id"] for n in backend.data["nodes"]} == expect_nodes
        assert {e["id"] for e in backend.data["edges"]} == expect_edges


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


class TestFaultInjectionOnlyPaths:
    """add_nodes has two failure windows real input cannot reach on its own:
    between a node landing in memory/unpersisted_nodes and the node-phase
    persist call, and after the edges have already persisted successfully
    but before add_nodes returns. Both are exercised here by forcing a raise
    with monkeypatch, which is the only way to reach them - the guarantee in
    both cases is the same one add_nodes documents for every failure exit:
    whatever landed before the raise is what the store ends up holding, and
    persist_landed() never re-sends what a persist call already sent."""

    def test_a_raise_between_node_insert_and_node_persist_still_persists_what_landed(
        self, monkeypatch
    ):
        backend = _IncrementalBackend()
        storage = _storage(backend)

        def boom(self, node):
            raise RuntimeError("boom-before-node-persist")

        monkeypatch.setattr(GraphStorage, "_build_searchable_text", boom)

        result = storage.add_nodes([_node("a")], [])
        storage.flush()

        assert not result.success and "boom-before-node-persist" in result.message
        assert "a" in storage.nodes
        assert "a" in backend.nodes
        assert _kinds(backend) == ["upsert_node"]

    def test_a_raise_after_the_edges_persisted_reports_failure_but_writes_nothing_twice(
        self, monkeypatch
    ):
        backend = _IncrementalBackend()
        storage = _storage(backend)

        original_emit = storage._emit_event
        calls = {"count": 0}

        def flaky_emit(*args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("boom-after-edges")
            return original_emit(*args, **kwargs)

        monkeypatch.setattr(storage, "_emit_event", flaky_emit)

        result = storage.add_nodes([_node("a"), _node("b")], [_edge("e", "a", "b")])
        storage.flush()

        assert not result.success and "boom-after-edges" in result.message
        # The nodes and the edge were already persisted before the raise -
        # unpersisted_nodes/unpersisted_edges were already cleared, so
        # persist_landed() in the except handler has nothing left to send.
        # A regression that forgot to clear one of those lists would show up
        # here as an extra call re-sending what already landed.
        assert set(storage.nodes) == {"a", "b"} == set(backend.nodes)
        assert set(storage.edges) == {"e"} == set(backend.edges)
        assert _kinds(backend) == ["apply_batch", "upsert_edge"]


class TestADeferredReport:
    """`ExternalChange.entities_read_on_demand` - the constructor a backend
    that can re-read its own store should use.

    What makes it worth the extra call is entirely a matter of WHEN the read
    happens, so that is what this class pins, and it pins it without a
    database: the PostgreSQL clauses drive the real transport, but they cannot
    run on a clone with nothing installed, and every property below belongs to
    the seam rather than to that backend.
    """

    def _deferred(self, storage, backend, operations):
        """Deliver one deferred report, recording the state at the read."""
        seen = []

        def read_content():
            seen.append(
                {
                    "lock_held": storage._lock._is_owned(),
                    "store": dict(backend.nodes),
                }
            )
            return operations

        backend.listener(ExternalChange.entities_read_on_demand(read_content))
        return seen

    def test_the_content_is_read_once_under_the_lock(self):
        """Twice would be two answers from two moments, of which only the
        first was made after the settle - and the lock is what stops a
        mutation being queued between them."""
        backend = _NotifyingBackend()
        storage = _storage(backend)
        try:
            seen = self._deferred(
                storage,
                backend,
                [EntityOperation.upsert_node(_node_payload("b", "Beacon"))],
            )
            assert len(seen) == 1, f"the content was read {len(seen)} times"
            assert seen[0]["lock_held"], (
                "the content was read without the lock, so a mutation could "
                "be queued between the settle and the read that used it"
            )
            assert storage.get_node("b").name == "Beacon"
        finally:
            storage.shutdown_events()

    def test_asking_twice_returns_the_first_answer_rather_than_a_fresher_one(
        self,
    ):
        """A report is observed in more than one place - the application
        applies it, a harness records what arrived - and asking twice would
        mean two reads at two moments, of which only the first was made after
        the settle. So the second ask is not a read."""
        answers = [
            [EntityOperation.upsert_node(_node_payload("b", "Beacon"))],
            [EntityOperation.upsert_node(_node_payload("b", "Later"))],
        ]
        reads = []

        def read_content():
            reads.append(1)
            return answers[len(reads) - 1]

        change = ExternalChange.entities_read_on_demand(read_content)
        first = change.with_content()
        second = change.with_content()

        assert len(reads) == 1, f"the content was read {len(reads)} times"
        assert first.operations == second.operations, (
            "the second ask returned a fresher answer than the first, so the "
            "two observers of one report disagree about what it said"
        )
        assert [op.payload["name"] for op in first.operations] == ["Beacon"]
        # And the answer is an answer: a change that still carried the callable
        # would read the store again the moment anyone asked it, which is the
        # same defect one indirection further along.
        assert not first.content_read_on_demand()
        assert first.with_content() is first
        assert len(reads) == 1

    def test_an_empty_answer_is_an_answer_and_is_not_asked_for_twice(self):
        """The store's answer to "what happened to these" can be "nothing is
        there any more", which is an empty batch of operations. Remembering it
        as "not read yet" would send the second observer back to the store -
        and the second read is the one taken at the wrong moment."""
        reads = []

        def read_content():
            reads.append(1)
            return []

        change = ExternalChange.entities_read_on_demand(read_content)
        assert change.with_content().operations == ()
        assert change.with_content().operations == ()
        assert len(reads) == 1, (
            f"an empty answer was not remembered; the store was read "
            f"{len(reads)} times for one report"
        )
        # An empty answer and "I cannot say what changed" are the two reports
        # that carry no operations, and only the second is a reload. The
        # predicate that separates them is the presence of the callable, not
        # the absence of operations.
        assert not ExternalChange.unknown().content_read_on_demand()
        assert not ExternalChange.entities([]).content_read_on_demand()

    def test_the_content_is_read_after_this_instances_queued_writes(self):
        """The whole reason the read is deferred. A read taken while a write
        of ours is still queued answers for a store that does not have it, and
        the instance then has nothing to tell the two apart by."""
        backend = _NotifyingBackend()
        storage = _storage(backend)
        released = threading.Event()
        real_upsert = backend.upsert_node

        def stall(node):
            assert released.wait(30.0), "the stalled write was never released"
            return real_upsert(node)

        backend.upsert_node = stall
        try:
            storage.add_nodes([_node("c", "Cedar")], [])
            assert "c" not in backend.nodes, "the write was not queued at all"

            reader = threading.Thread(
                target=lambda: seen.extend(
                    self._deferred(
                        storage,
                        backend,
                        [EntityOperation.upsert_node(_node_payload("b", "Beacon"))],
                    )
                ),
                daemon=True,
            )
            seen = []
            reader.start()
            # The report is now waiting on the settle, which is waiting on the
            # stalled write. Releasing it is what lets both through, in that
            # order.
            released.set()
            reader.join(30.0)
            assert not reader.is_alive(), "the report never completed"
        finally:
            released.set()
            del backend.upsert_node
            storage.shutdown_events()

        assert len(seen) == 1
        assert "c" in seen[0]["store"], (
            "the content was read before this instance's own queued write had "
            "landed, so the answer it gave predates it"
        )

    def test_a_failed_local_write_stops_it_before_the_content_is_read(self):
        """The refusal `_settle_before_refresh` reports is not advisory. A
        mutation that failed is in memory and nowhere else, so an answer read
        from the store cannot contain it - and applying that answer as the
        store's own, which is exactly what this path does, would drop it."""
        backend = _NotifyingBackend()
        storage = _storage(backend)
        try:
            storage.add_nodes([_node("a", "Alpha")], [])
            storage.flush()

            def refuse(node):
                raise OSError("the write failed")

            backend.upsert_node = refuse
            storage.add_nodes([_node("c", "Cedar")], [])
            storage._io_executor.submit(lambda: None).result()
            assert storage._resync_pending, "the failed write did not raise the flag"
            del backend.upsert_node

            seen = self._deferred(
                storage,
                backend,
                [EntityOperation.delete_node("a")],
            )

            assert seen == [], (
                "the content was read over a failed local write; the answer "
                "cannot contain the mutation, and applying it drops it"
            )
            assert {n.id for n in storage.get_all_nodes()} == {"a", "c"}
            assert storage._resync_pending
        finally:
            storage.shutdown_events()

    def test_an_answer_that_changes_nothing_leaves_the_fallback_vector_alone(
        self,
    ):
        """ "Applied as nothing at all" has to mean the inline fallback too.

        That fallback is the raw copy a node was loaded with, kept for a
        vector the index would not take - a width it refuses, say. For such a
        node it is the only durable copy there is, so dropping it on a report
        that changed nothing would take the vector out of every structure at
        once, and the next snapshot would write the node without it.
        """
        backend = _NotifyingBackend()
        storage = _storage(backend)
        try:
            storage.add_nodes([_node("a", "Alpha")], [])
            storage.flush()
            storage._inline_fallback["a"] = [0.5, 0.25]
            assert storage._vector_for_payload("a") == [0.5, 0.25]

            held = storage.get_node("a").to_dict()
            backend.listener(
                ExternalChange.entities_read_on_demand(
                    lambda: [EntityOperation.upsert_node(held)]
                )
            )

            assert storage._vector_for_payload("a") == [0.5, 0.25], (
                "a report that changed nothing took the node's only durable "
                "copy of its vector with it"
            )
        finally:
            storage.shutdown_events()

    def test_the_eager_constructor_is_not_quietly_given_the_same_treatment(
        self,
    ):
        """The suppression belongs to the deferred path and to nothing else.

        An eager report's content was gathered before this instance settled,
        so "identical to what we hold" does not mean the store agrees with us
        - it means the store agreed with us at some earlier moment. Applying
        it is what that path has always done, and a subscriber that stopped
        hearing about it would be losing an event this change never set out to
        remove.
        """
        backend = _NotifyingBackend()
        storage = _storage(backend)
        try:
            storage.add_nodes([_node("a", "Alpha")], [])
            storage.flush()
            seen = []
            storage.add_system_listener(seen.append)

            held = storage.get_node("a").to_dict()
            backend.listener(
                ExternalChange.entities([EntityOperation.upsert_node(held)])
            )

            assert [e.event_type for e in seen] == [EventType.NODE_UPDATE], (
                "an eager report carrying what this instance already holds was "
                f"suppressed; that is the deferred path's rule, not this one "
                f"(saw {[e.event_type for e in seen]})"
            )
        finally:
            storage.shutdown_events()

    def test_a_read_that_raises_reloads_the_graph_exactly_once(self):
        """The change is real whatever the read did, so it may not be dropped.
        A whole graph is the most expensive read this seam has, so it is also
        not done twice."""
        backend = _NotifyingBackend()
        storage = _storage(backend)
        loads = []
        real_load = backend.load_graph_data
        backend.load_graph_data = lambda: (loads.append(1), real_load())[1]
        try:
            backend.nodes["z"] = _node_payload("z", "Zulu")

            def read_content():
                raise OSError("the store would not answer")

            backend.listener(ExternalChange.entities_read_on_demand(read_content))

            assert loads == [1], f"the graph was reloaded {len(loads)} times"
            assert storage.get_node("z").name == "Zulu", (
                "the report was dropped rather than turned into a reload"
            )
        finally:
            del backend.load_graph_data
            storage.shutdown_events()

    def test_a_read_that_raises_does_not_write_this_instances_graph(self):
        """And the reload it turns into does not bootstrap.

        A store reporting that it is not there - mid-restore, being replaced -
        is not an invitation to write this instance's image over it, and on a
        store whose whole point is that someone else is writing it too that
        would destroy their work. `_reload_from_store` says bootstrapping is
        off; this is that road, which is not the one `unknown()` takes.
        """
        backend = _NotifyingBackend()
        storage = _storage(backend)
        try:
            backend.exists = lambda: False
            snapshots = backend.snapshots

            def read_content():
                raise OSError("the store would not answer")

            backend.listener(ExternalChange.entities_read_on_demand(read_content))

            assert backend.snapshots == snapshots, (
                "the reload bootstrapped: this instance wrote its whole graph "
                "over a store it had just been told it could not read"
            )
        finally:
            del backend.exists
            storage.shutdown_events()

    def test_a_report_with_no_content_to_read_is_not_a_reload(self):
        """`unknown()` and a deferred report both arrive with `operations`
        unset, and only the first means reload the whole graph. Reading the
        one as the other would throw away the named change - or reload on
        every announcement, whichever way round the confusion ran."""
        backend = _NotifyingBackend()
        storage = _storage(backend)
        loads = []
        real_load = backend.load_graph_data
        backend.load_graph_data = lambda: (loads.append(1), real_load())[1]
        try:
            backend.nodes["z"] = _node_payload("z", "Zulu")
            self._deferred(storage, backend, [])

            assert loads == [], "a deferred report was mistaken for unknown()"
            assert storage.get_node("z") is None, (
                "the graph was reloaded by some other road"
            )
        finally:
            del backend.load_graph_data
            storage.shutdown_events()
