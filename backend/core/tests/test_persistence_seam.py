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

import pytest

from backend.core.models import Edge, Node, NodeType
from backend.core.storage import GraphStorage
from backend.core.storage_backends import (
    SNAPSHOT_ONLY,
    BackendCapabilities,
    EntityOperation,
    FileGraphPersistenceBackend,
    GraphPersistenceBackend,
    IncrementalGraphPersistenceBackend,
    capabilities_of,
)


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
        for method in ("delete_node", "upsert_edge", "delete_edge", "apply_batch"):
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
