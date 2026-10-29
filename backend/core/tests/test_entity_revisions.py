"""Entity revisions on a single-writer store (docs/SESSION_OVERLAY_CONTRACT.md §4).

The file backend is the single-writer store: the instance keeps the counter,
behind a durable reservation in graph.json's metadata, and checks every
expectation against its own model. The shared-store half - revisions assigned
by PostgreSQL, synchronous writes, the failure re-read - is in
test_entity_revisions_postgres.py.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from typing import Any, Dict, List
from unittest.mock import Mock

import pytest

from backend.core import (
    BackendCapabilities,
    Edge,
    EntityRevisionConflict,
    GraphStorage,
    Node,
    capabilities_of,
)
from backend.core.events.models import EventType
from backend.core.models import AddNodesResult
from backend.core.storage_backends import (
    FileGraphPersistenceBackend,
    GraphWriteFailed,
    GraphWriteOutcomeUnknown,
    WriteRefusedByStore,
)
from backend.runtime.authorization import DefaultGraphAuthorizationHook
from backend.service import mutations


def _node(node_id: str, **fields: Any) -> Node:
    return Node(id=node_id, type="Actor", name=fields.pop("name", node_id), **fields)


@pytest.fixture(autouse=True)
def no_embedding_model(monkeypatch):
    # Revision tests use supplied vectors where needed; never load an ML
    # model or contact a model registry just to exercise persistence.
    def unavailable():
        raise RuntimeError("embedding model unavailable in revision tests")

    monkeypatch.setattr(
        "backend.core.vector_store._ensure_sentence_transformers", unavailable
    )


@pytest.fixture
def make_storage(tmp_path):
    made: List[GraphStorage] = []

    def make(block: int = GraphStorage.REVISION_RESERVATION_BLOCK) -> GraphStorage:
        storage = GraphStorage(json_path=str(tmp_path / "graph.json"))
        storage.REVISION_RESERVATION_BLOCK = block
        made.append(storage)
        return storage

    yield make
    for storage in made:
        storage.flush()


def _events(storage: GraphStorage) -> List[Any]:
    seen: List[Any] = []
    storage.add_system_listener(seen.append)
    return seen


def _all_revisions(storage: GraphStorage) -> List[int]:
    return [n.revision for n in storage.nodes.values()] + [
        e.revision for e in storage.edges.values()
    ]


def test_every_applied_write_takes_a_fresh_graph_wide_revision(make_storage):
    storage = make_storage()
    storage.add_nodes([_node("a"), _node("b")], [Edge(id="e", source="a", target="b")])
    created = sorted(_all_revisions(storage))
    assert created == list(range(created[0], created[0] + 3))
    assert created[0] >= 1

    stamps = [storage.nodes["a"].revision]
    storage.update_node("a", {"name": "renamed"})
    stamps.append(storage.nodes["a"].revision)
    storage.update_edge("e", {"label": "x"})
    stamps.append(storage.edges["e"].revision)
    storage.set_nodes_archived(["b"], True)
    stamps.append(storage.nodes["b"].revision)
    storage.set_edges_archived(["e"], True)
    stamps.append(storage.edges["e"].revision)

    # One counter for nodes and edges alike, strictly increasing, never reused.
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == len(stamps)
    assert stamps[1] > max(created)


def test_a_recreated_entity_never_matches_an_expectation_taken_before_its_delete(
    make_storage,
):
    storage = make_storage()
    storage.add_nodes([_node("a")], [])
    old = storage.nodes["a"].revision
    storage.delete_nodes(["a"], confirmed=True)
    storage.add_nodes([_node("a")], [])

    assert storage.nodes["a"].revision > old
    with pytest.raises(EntityRevisionConflict) as refused:
        storage.update_node("a", {"name": "stale"}, expected_revision=old)
    assert refused.value.current_revision == storage.nodes["a"].revision
    assert storage.nodes["a"].name == "a"


def test_a_refused_expectation_changes_nothing_and_fires_no_event(make_storage):
    storage = make_storage()
    storage.add_nodes([_node("a"), _node("b")], [Edge(id="e", source="a", target="b")])
    events = _events(storage)
    node_before = storage.nodes["a"].to_dict()
    edge_before = storage.edges["e"].to_dict()
    stale = storage.nodes["a"].revision - 1

    with pytest.raises(EntityRevisionConflict):
        storage.update_node("a", {"name": "x"}, expected_revision=stale)
    with pytest.raises(EntityRevisionConflict):
        storage.update_edge("e", {"label": "x"}, expected_revision=stale)
    with pytest.raises(EntityRevisionConflict):
        storage.delete_edge("e", expected_revision=stale)
    with pytest.raises(EntityRevisionConflict):
        storage.delete_nodes(["a"], confirmed=True, expected_revisions={"a": stale})
    with pytest.raises(EntityRevisionConflict):
        storage.set_nodes_archived(["a"], True, expected_revisions={"a": stale})
    with pytest.raises(EntityRevisionConflict):
        storage.add_nodes([_node("a")], [], expect_absent=True)

    assert storage.nodes["a"].to_dict() == node_before
    assert storage.edges["e"].to_dict() == edge_before
    assert events == []


def test_a_matching_expectation_applies_and_waits_for_the_store(make_storage):
    storage = make_storage()
    storage.add_nodes([_node("a")], [])
    current = storage.nodes["a"].revision
    storage.update_node("a", {"name": "ok"}, expected_revision=current)
    # Waited for: it is on disk once the call returns, before any flush.
    journal = (storage.json_path.parent / "graph.journal.ndjson").read_text()
    assert '"ok"' in journal
    assert storage.nodes["a"].revision > current


@pytest.mark.parametrize("expecting", [False, True])
def test_only_an_expected_file_write_waits_and_surfaces_a_failed_persist(
    make_storage, monkeypatch, expecting
):
    storage = make_storage()
    storage.add_nodes([_node("a")], [])
    storage.flush()
    entered = threading.Event()
    release = threading.Event()

    def fail_incremental(payload):
        entered.set()
        assert release.wait(5), "test did not release the blocked write"
        raise OSError("journal unavailable")

    def fail_snapshot(data):
        raise OSError("snapshot unavailable")

    # Both persistence paths fail: an expected write must report the failed
    # healing snapshot too, while an ordinary write returns before either.
    with monkeypatch.context() as patch:
        patch.setattr(storage._persistence_backend, "upsert_node", fail_incremental)
        patch.setattr(storage._persistence_backend, "save_graph_data", fail_snapshot)
        with ThreadPoolExecutor(max_workers=1) as caller:
            write = caller.submit(
                storage.update_node,
                "a",
                {"name": "changed"},
                expected_revision=storage.nodes["a"].revision if expecting else None,
            )
            try:
                assert entered.wait(5), "write did not reach persistence"
                if expecting:
                    with pytest.raises(TimeoutError):
                        write.result(timeout=0.1)
                else:
                    assert write.result(timeout=5).name == "changed"
                release.set()
                if expecting:
                    with pytest.raises(OSError, match="snapshot unavailable"):
                        write.result(timeout=5)
            finally:
                release.set()
        storage._io_executor.submit(lambda: None).result(timeout=5)

    storage.flush()
    assert json.loads(storage.json_path.read_text())["nodes"][0]["name"] == "changed"


def test_a_refused_node_delete_restores_its_vector(make_storage, monkeypatch):
    storage = make_storage()
    vector = [0.25, 0.5]
    storage.add_nodes(
        [_node("a", embedding=vector), _node("b", embedding=[0.5, 0.25])],
        [Edge(id="e", source="a", target="b")],
    )
    storage.flush()
    before = _snapshot(storage)
    events = _events(storage)

    def refuse(operations, write_token=None):
        assert not storage.vector_store.has_embedding("a")
        raise WriteRefusedByStore("delete refused")

    monkeypatch.setattr(storage, "_store_assigns_revisions", True)
    monkeypatch.setattr(storage._persistence_backend, "apply_batch", refuse)
    with pytest.raises(GraphWriteFailed):
        storage.delete_nodes(["a"], confirmed=True)

    assert _snapshot(storage) == before
    assert storage.graph.has_edge("a", "b", key="e")
    assert storage.vector_store.get_vector_list("a") == pytest.approx(vector)
    assert events == []


@pytest.mark.parametrize("second_read", ["unchanged", "changed", "unreadable"])
def test_a_failed_shared_write_is_undone_only_after_two_unchanged_readings(
    make_storage, monkeypatch, second_read
):
    storage = make_storage()
    storage.add_nodes([_node("a")], [])
    storage.flush()
    revision = storage.nodes["a"].revision
    reads = []

    def fail(operations, write_token=None):
        raise OSError("acknowledgement lost")

    def evidence(keys):
        reads.append(list(keys))
        if len(reads) == 2:
            if second_read == "unreadable":
                raise OSError("store unavailable")
            if second_read == "changed":
                return {("node", "a"): (revision + 1, "another-write")}
        return {("node", "a"): (revision, None)}

    monkeypatch.setattr(storage, "_store_assigns_revisions", True)
    monkeypatch.setattr(storage, "WRITE_RESOLVE_DELAY_SECONDS", 0)
    monkeypatch.setattr(storage._persistence_backend, "apply_batch", fail)
    monkeypatch.setattr(
        storage._persistence_backend, "read_write_evidence", evidence, raising=False
    )
    error = GraphWriteFailed if second_read == "unchanged" else GraphWriteOutcomeUnknown
    with pytest.raises(error):
        storage.update_node("a", {"name": "changed"})
    assert reads == [[("node", "a")], [("node", "a")]]
    assert storage.nodes["a"].name == "a"


def test_an_expectation_on_an_absent_entity_is_a_conflict_not_a_miss(make_storage):
    storage = make_storage()
    with pytest.raises(EntityRevisionConflict) as refused:
        storage.update_node("missing", {"name": "x"}, expected_revision=3)
    assert refused.value.current_revision is None


def test_events_carry_the_revision_and_an_edge_update_carries_a_patch(make_storage):
    storage = make_storage()
    storage.add_nodes([_node("a"), _node("b")], [Edge(id="e", source="a", target="b")])
    events = _events(storage)
    storage.update_edge("e", {"label": "new"})

    (event,) = events
    assert event.event_type == EventType.EDGE_UPDATE
    assert event.entity.after["revision"] == storage.edges["e"].revision
    assert event.entity.before["revision"] < event.entity.after["revision"]
    assert event.entity.patch["label"] == "new"
    assert "source" not in event.entity.patch


def test_a_client_supplied_revision_is_dropped_never_stored_or_folded(make_storage):
    storage = make_storage()
    hook = DefaultGraphAuthorizationHook()
    result = mutations.add_nodes(
        storage,
        hook,
        [{"id": "a", "type": "Actor", "name": "a", "revision": 999}],
        [],
    )
    assert result["success"], result
    assert storage.nodes["a"].revision != 999
    assert "revision" not in storage.nodes["a"].metadata

    mutations.update_node(storage, hook, "a", {"revision": 999, "name": "b"})
    assert storage.nodes["a"].revision != 999
    assert "revision" not in storage.nodes["a"].metadata

    # Below the service layer too: the storage stamps whatever it is handed.
    storage.add_nodes([_node("c", revision=999)], [])
    assert storage.nodes["c"].revision < 999


def test_the_service_answers_a_conflict_with_its_code_and_current_revision(
    make_storage,
):
    storage = make_storage()
    hook = DefaultGraphAuthorizationHook()
    storage.add_nodes([_node("a")], [])
    current = storage.nodes["a"].revision
    result = mutations.update_node(
        storage, hook, "a", {"name": "x"}, expected_revision=current + 7
    )
    assert result["success"] is False
    assert result["error"] == "entity_revision_conflict"
    assert result["current_revision"] == current


def test_the_reservation_is_durable_before_any_value_at_or_above_it_is_used(
    make_storage,
):
    storage = make_storage(block=3)
    handed_out: List[int] = []
    for index in range(10):
        storage.add_nodes([_node(f"n{index}")], [])
        handed_out.append(storage.nodes[f"n{index}"].revision)
        # graph.json itself, not the journal: what a crash leaves.
        on_disk = json.loads(storage.json_path.read_text())["metadata"]
        assert on_disk["revision_reservation"] > max(handed_out)


def test_a_restart_never_hands_out_a_value_used_before_it(make_storage):
    first = make_storage(block=3)
    for index in range(5):
        first.add_nodes([_node(f"n{index}")], [])
    # A value handed out whose journal line never landed is the crash shape:
    # it exists nowhere on disk, only in the counter it was taken from.
    lost = first._take_revisions(1)
    first.flush()

    second = make_storage(block=3)
    second.add_nodes([_node("after")], [])
    assert second.nodes["after"].revision > lost


def test_a_legacy_entity_reads_as_zero_and_its_first_write_stamps_it(tmp_path):
    path = tmp_path / "graph.json"
    path.write_text(
        json.dumps(
            {
                "nodes": [{"id": "old", "type": "Actor", "name": "old"}],
                "edges": [],
                "metadata": {"graph_name": "g"},
            }
        )
    )
    storage = GraphStorage(json_path=str(path))
    try:
        assert storage.nodes["old"].revision == 0
        storage.update_node("old", {"name": "new"}, expected_revision=0)
        assert storage.nodes["old"].revision >= 1
    finally:
        storage.flush()


@pytest.mark.parametrize("highest_kind", ["node", "edge"])
def test_a_file_without_a_reservation_resumes_above_its_highest_revision(
    make_storage, tmp_path, highest_kind
):
    highest = 73
    (tmp_path / "graph.json").write_text(
        json.dumps(
            {
                "nodes": [
                    _node(
                        "a", revision=highest if highest_kind == "node" else 1
                    ).to_dict()
                ],
                "edges": [
                    Edge(
                        id="e",
                        source="a",
                        target="a",
                        revision=highest if highest_kind == "edge" else 1,
                    ).to_dict()
                ],
                "metadata": {"graph_name": "g"},
            }
        )
    )
    storage = make_storage()
    storage.add_nodes([_node("new")], [])
    assert storage.nodes["new"].revision > highest


def test_reservations_are_written_once_per_block_and_skipped_with_headroom(
    make_storage, tmp_path, monkeypatch
):
    (tmp_path / "graph.json").write_text(
        json.dumps({"nodes": [], "edges": [], "metadata": {}})
    )
    reserved = []
    reserve = FileGraphPersistenceBackend.reserve_revisions

    def record(backend, ceiling):
        reserved.append(ceiling)
        return reserve(backend, ceiling)

    monkeypatch.setattr(GraphStorage, "REVISION_RESERVATION_BLOCK", 3)
    monkeypatch.setattr(FileGraphPersistenceBackend, "reserve_revisions", record)
    storage = make_storage(block=3)
    assert reserved == [4], "load must reserve ahead once"
    with storage._lock:
        storage._reserve_ahead()
    assert reserved == [4], "headroom must not be reserved again"
    for index in range(1, 9):
        storage.add_nodes([_node(f"n{index}")], [])
        assert reserved == ([4] if index < 4 else [4, 8] if index < 8 else [4, 8, 12])


@pytest.mark.parametrize("action", ["delete", "archive"])
def test_bulk_edge_expectations_are_checked_before_any_edge_changes(
    make_storage, action
):
    storage = make_storage()
    storage.add_nodes(
        [_node("a")],
        [Edge(id="e", source="a", target="a"), Edge(id="f", source="a", target="a")],
    )
    before = _snapshot(storage)
    events = _events(storage)
    expected = {"e": storage.edges["e"].revision, "f": storage.edges["f"].revision + 1}
    with pytest.raises(EntityRevisionConflict) as refused:
        if action == "delete":
            storage.delete_edges(["e", "f"], expected_revisions=expected)
        else:
            storage.set_edges_archived(["e", "f"], True, expected_revisions=expected)
    assert refused.value.entity_id == "f"
    assert _snapshot(storage) == before
    assert events == []

    expected["f"] = storage.edges["f"].revision
    if action == "delete":
        assert storage.delete_edges(["e", "f"], expected_revisions=expected).success
        assert not storage.edges
    else:
        storage.set_edges_archived(["e", "f"], True, expected_revisions=expected)
        assert all(edge.archived for edge in storage.edges.values())
        assert all(edge.revision > expected[edge.id] for edge in storage.edges.values())


def test_replacing_the_graph_restamps_every_entity_above_what_was_held(make_storage):
    storage = make_storage()
    storage.add_nodes([_node("a"), _node("b")], [Edge(id="e", source="a", target="b")])
    highest = max(_all_revisions(storage))
    storage.replace_all_nodes_and_edges(
        [_node("a", revision=highest), _node("b")],
        [Edge(id="e", source="a", target="b", revision=highest)],
    )
    restamped = _all_revisions(storage)
    assert min(restamped) > highest
    assert len(set(restamped)) == len(restamped)


def test_the_journal_never_carries_an_expectation(make_storage):
    storage = make_storage()
    storage.add_nodes([_node("a")], [], expect_absent=True)
    storage.update_node(
        "a", {"name": "x"}, expected_revision=storage.nodes["a"].revision
    )
    lines = (storage.json_path.parent / "graph.journal.ndjson").read_text().splitlines()
    for line in lines:
        for op in json.loads(line)["ops"]:
            assert set(op) == {"kind", "action", "entity_id", "payload"}


@pytest.mark.parametrize("kind", ["node", "edge"])
@pytest.mark.parametrize("expect_absent", [False, True])
def test_a_single_writer_create_waits_only_when_it_carries_an_expectation(
    make_storage, monkeypatch, kind, expect_absent
):
    # The shared store's refusal of an unseen id is not this store's: here
    # the instance's own model is the whole graph, and a plain create stays
    # the fire-and-forget write it always was.
    storage = make_storage()
    storage.add_nodes([_node("a"), _node("b")], [])
    sent: List[Any] = []
    futures: List[Any] = []
    persist = storage._persist

    def spy(operations):
        sent.extend(operations)
        future = Mock(wraps=persist(operations))
        futures.append(future)
        return future

    monkeypatch.setattr(storage, "_persist", spy)
    if kind == "node":
        result = storage.add_nodes(
            [_node("c")],
            [Edge(id="e", source="c", target="a")],
            expect_absent=expect_absent,
        )
        assert result.success
        expected_entities = {("node", "c"), ("edge", "e")}
    else:
        result = storage.add_edge(
            Edge(id="e", source="a", target="b"), expect_absent=expect_absent
        )
        assert result == "e"
        expected_entities = {("edge", "e")}

    assert {(op.kind, op.entity_id) for op in sent} == expected_entities
    assert all(op.expect_absent is expect_absent for op in sent)
    assert any(future.result.called for future in futures) is expect_absent


@pytest.mark.parametrize("failure", ["duplicate_node", "duplicate_edge"])
def test_a_locally_failing_batch_keeps_landed_nodes_when_persistence_raises(
    make_storage, monkeypatch, failure
):
    storage = make_storage()
    storage.add_nodes(
        [_node("held")], [Edge(id="held_edge", source="held", target="held")]
    )
    commit = Mock(side_effect=RuntimeError("executor shut down"))
    monkeypatch.setattr(storage, "_commit", commit)
    nodes = [_node("new")]
    edges = []
    if failure == "duplicate_node":
        nodes.append(_node("held"))
    else:
        edges.append(Edge(id="held_edge", source="new", target="held"))

    result = storage.add_nodes(nodes, edges)

    assert isinstance(result, AddNodesResult)
    assert result.success is False
    assert "already exists" in result.message
    assert result.added_node_ids == [] and result.added_edge_ids == []
    assert result.landed_node_ids == ["new"]
    assert storage.nodes["new"] is nodes[0]
    commit.assert_called_once()


def test_a_backend_declaring_revision_enforcement_must_implement_it():
    class Declares:
        def capabilities(self) -> BackendCapabilities:
            return BackendCapabilities(
                incremental_writes=True, transactions=True, revision_enforcement=True
            )

        def __getattr__(self, name: str) -> Any:
            if name in (
                "upsert_node",
                "delete_node",
                "upsert_edge",
                "delete_edge",
                "apply_batch",
                "checkpoint",
            ):
                return lambda *a, **k: None
            raise AttributeError(name)

    with pytest.raises(TypeError, match="read_write_evidence"):
        capabilities_of(Declares())


def _snapshot(storage: GraphStorage) -> Dict[str, Any]:
    return {
        "nodes": {k: v.to_dict() for k, v in storage.nodes.items()},
        "edges": {k: v.to_dict() for k, v in storage.edges.items()},
    }


def test_a_failed_reservation_undoes_the_write_and_fires_no_event(make_storage):
    storage = make_storage(block=1)
    storage.add_nodes([_node("a")], [])
    before = _snapshot(storage)
    events = _events(storage)

    def refuse(_ceiling: int) -> None:
        raise OSError("disk full")

    storage._persistence_backend.reserve_revisions = refuse  # type: ignore[method-assign]
    storage._revision_reservation = storage._next_revision  # force a raise
    from backend.core import GraphWriteFailed

    with pytest.raises(GraphWriteFailed):
        storage.update_node("a", {"name": "x"})
    with pytest.raises(GraphWriteFailed):
        storage.add_nodes([_node("b")], [])
    assert _snapshot(storage) == before
    assert events == []


def test_a_refresh_never_writes_a_reservation(make_storage):
    storage = make_storage()
    storage.add_nodes([_node("a")], [])
    # The write is queued; a refresh that read the file before it landed would
    # swap in the stale (empty) graph and drop "a".
    storage.flush()

    reserved: List[int] = []

    def refuse(ceiling: int) -> None:
        # Recorded, not raised: the load path catches Exception around a
        # reservation, so a raise here would be swallowed and the test pass.
        reserved.append(ceiling)

    storage._persistence_backend.reserve_revisions = refuse  # type: ignore[method-assign]
    storage._revision_reservation = storage._next_revision  # due for a raise
    storage.load(bootstrap_if_missing=False)
    assert "a" in storage.nodes
    assert reserved == [], "a refresh wrote a reservation"


def test_a_shared_store_without_enforcement_never_persists_a_reservation():
    from backend.core.tests.persistence_contract import (
        InMemoryGraphPersistenceBackend,
    )

    store: Dict[str, Any] = {}
    storage = GraphStorage(persistence_backend=InMemoryGraphPersistenceBackend(store))
    try:
        for index in range(3):
            storage.add_nodes([_node(f"n{index}")], [])
        storage.flush()
        assert "revision_reservation" not in store["metadata"]
        assert len({n.revision for n in storage.nodes.values()}) == 3
    finally:
        storage.shutdown_events()


@pytest.mark.parametrize("staged", [False, True])
def test_a_create_conflict_never_reports_the_taken_entitys_revision(
    make_storage, staged
):
    storage = make_storage()
    storage.add_nodes([_node("taken")], [])
    staging = None
    if staged:
        staging = Mock()
        staging.add_nodes.side_effect = EntityRevisionConflict(
            "node", "taken", None, storage.nodes["taken"].revision
        )
    result = mutations.add_nodes(
        storage,
        DefaultGraphAuthorizationHook(),
        [{"id": "taken", "type": "Actor", "name": "again"}],
        [],
        expect_absent=True,
        staging=staging,
    )
    assert result["success"] is False
    assert result["error"] == "entity_revision_conflict"
    assert result["current_revision"] is None
    assert result["message"] == "node 'taken' already exists"
    assert result["added_node_ids"] == [] and result["added_edge_ids"] == []
    assert str(storage.nodes["taken"].revision) not in result["message"]


def test_a_refused_adoption_never_reports_the_taken_entitys_revision(
    make_storage, monkeypatch
):
    # On a shared store the adoption's create can be refused over an id
    # another instance created; it answers like any other refused create.
    class _Federation:
        enabled = True

        def get_cached_node(self, node_id):
            return Node(
                id=node_id,
                type="Actor",
                name="remote",
                metadata={"origin_graph_id": "g", "origin_node_id": "r"},
            )

        def get_graph_config_for_node(self, node_id):
            return None

    storage = make_storage()

    def refuse(nodes, edges, **kwargs):
        raise EntityRevisionConflict("node", nodes[0].id, None, 7)

    monkeypatch.setattr(storage, "add_nodes", refuse)
    result = mutations.adopt_federated_node(
        storage, _Federation(), DefaultGraphAuthorizationHook(), "federated::g::r"
    )
    assert result["error"] == "entity_revision_conflict"
    assert result["current_revision"] is None
    assert result["message"].endswith("already exists")
    assert result["added_node_ids"] == [] and result["added_edge_ids"] == []


def test_a_refused_edge_create_never_reports_the_taken_entitys_revision(
    make_storage, monkeypatch
):
    storage = make_storage()
    storage.add_nodes([_node("a")], [])

    def refuse(edge, **kwargs):
        raise EntityRevisionConflict("edge", edge.id, None, 7)

    monkeypatch.setattr(storage, "add_edge", refuse)
    result = mutations.add_edge(storage, DefaultGraphAuthorizationHook(), "a", "a")
    assert result["error"] == "entity_revision_conflict"
    assert result["current_revision"] is None
    assert result["message"].endswith("already exists")


def test_an_expectation_on_a_node_the_call_does_not_delete_is_refused(make_storage):
    storage = make_storage()
    storage.add_nodes([_node("a"), _node("b")], [])
    result = mutations.delete_nodes(
        storage,
        DefaultGraphAuthorizationHook(),
        ["a"],
        confirmed=True,
        expected_revisions={"b": storage.nodes["b"].revision},
    )
    assert result["success"] is False
    assert "a" in storage.nodes


def test_an_imported_revision_past_the_ceiling_is_refused():
    from pydantic import ValidationError

    from backend.core.models import MAX_REVISION

    with pytest.raises(ValidationError):
        Node(id="x", type="Actor", name="x", revision=MAX_REVISION + 1)


@pytest.mark.parametrize(
    "code,status",
    [
        ("entity_revision_conflict", 409),
        ("write_failed", 502),
        ("write_outcome_unknown", 503),
    ],
)
def test_rest_answers_each_write_refusal_with_its_contract_status(code, status):
    from fastapi import HTTPException

    from backend.service.rest_api import _raise_for_access_denied

    with pytest.raises(HTTPException) as raised:
        _raise_for_access_denied(
            {"success": False, "error": code, "message": "m", "current_revision": 3}
        )
    assert raised.value.status_code == status
    assert raised.value.detail["error"] == code


def test_the_revision_in_memory_is_the_one_on_disk(make_storage, tmp_path):
    storage = make_storage()
    storage.add_nodes([_node("a"), _node("b")], [Edge(id="e", source="a", target="b")])
    storage.update_node("a", {"name": "x"})
    storage.update_edge("e", {"label": "y"})
    held = (storage.nodes["a"].revision, storage.edges["e"].revision)
    storage.flush()

    reopened = make_storage()
    assert (reopened.nodes["a"].revision, reopened.edges["e"].revision) == held
    reopened.update_node("a", {"name": "z"}, expected_revision=held[0])


def test_an_edge_revision_past_the_ceiling_is_refused_too():
    from pydantic import ValidationError

    from backend.core.models import MAX_REVISION

    with pytest.raises(ValidationError):
        Edge(id="e", source="a", target="b", revision=MAX_REVISION + 1)


def test_expectations_on_some_of_the_deleted_nodes_are_enough(make_storage):
    storage = make_storage()
    storage.add_nodes([_node("a"), _node("b")], [])
    result = mutations.delete_nodes(
        storage,
        DefaultGraphAuthorizationHook(),
        ["a", "b"],
        confirmed=True,
        expected_revisions={"a": storage.nodes["a"].revision},
    )
    assert result["success"], result
    assert not storage.nodes


def test_an_expectation_on_a_hidden_node_reveals_nothing_about_it(make_storage):
    from backend.runtime.authorization import (
        GraphAccessNarrowing,
        GraphAuthorizationDecision,
    )

    class HidesTheLocalGraph:
        def evaluate(self, context):
            return GraphAuthorizationDecision(
                allowed=True,
                mode="fixed",
                source="test",
                graph_access=GraphAccessNarrowing(
                    enabled=True, allow_local_graph=False, include_graph_ids=()
                ),
            )

    storage = make_storage()
    storage.add_nodes([_node("hidden")], [])
    real = storage.nodes["hidden"].revision
    result = mutations.delete_nodes(
        storage,
        HidesTheLocalGraph(),
        ["hidden"],
        confirmed=True,
        expected_revisions={"hidden": real + 5},
    )
    assert result["error"] == "entity_revision_conflict"
    assert result["current_revision"] is None
    assert str(real) not in result["message"]
    assert "hidden" in storage.nodes


@pytest.mark.parametrize(
    "sqlstate,refused",
    [
        ("22P05", True),
        ("23505", True),
        ("42501", True),
        ("54000", True),
        # A lost or uncertain commit is never a refusal: these must be read back.
        ("40001", False),
        ("40P01", False),
        ("40003", False),
        ("08006", False),
        ("57P01", False),
        ("53300", False),
        (None, False),
    ],
)
def test_only_a_statement_refusal_counts_as_refused_outright(sqlstate, refused):
    from backend.core.storage_backends import WriteRefusedByStore, refused_outright

    class Failure(Exception):
        pass

    failure = Failure("x")
    failure.sqlstate = sqlstate
    assert refused_outright(failure) is refused
    assert refused_outright(WriteRefusedByStore("x")) is True


def test_a_direct_subtype_update_carries_incident_revisions_and_undoes_a_race(
    make_storage, monkeypatch
):
    storage = make_storage()
    storage.add_nodes(
        [_node("a", subtypes=["Agency"]), _node("b")],
        [Edge(id="e", source="a", target="b")],
    )
    storage.flush()
    before = _snapshot(storage)
    events = _events(storage)

    def raced(operations, write_token=None):
        (op,) = operations
        assert op.incident_edge_revisions == (("e", storage.edges["e"].revision),)
        raise EntityRevisionConflict("node", "a", None, None)

    monkeypatch.setattr(storage, "_store_assigns_revisions", True)
    monkeypatch.setattr(storage._persistence_backend, "apply_batch", raced)
    with pytest.raises(EntityRevisionConflict):
        storage.update_node("a", {"subtypes": []})
    assert _snapshot(storage) == before
    assert events == []
