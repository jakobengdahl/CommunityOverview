"""Entity revisions on a single-writer store (docs/SESSION_OVERLAY_CONTRACT.md §4).

The file backend is the single-writer store: the instance keeps the counter,
behind a durable reservation in graph.json's metadata, and checks every
expectation against its own model. The shared-store half - revisions assigned
by PostgreSQL, synchronous writes, the failure re-read - is in
test_entity_revisions_postgres.py.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

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
from backend.runtime.authorization import DefaultGraphAuthorizationHook
from backend.service import mutations


def _node(node_id: str, **fields: Any) -> Node:
    return Node(id=node_id, type="Actor", name=fields.pop("name", node_id), **fields)


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

    def refuse(_ceiling: int) -> None:
        raise AssertionError("a refresh wrote a reservation")

    storage._persistence_backend.reserve_revisions = refuse  # type: ignore[method-assign]
    storage._revision_reservation = storage._next_revision  # due for a raise
    storage.load(bootstrap_if_missing=False)
    assert "a" in storage.nodes


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


def test_a_create_conflict_never_reports_the_taken_entitys_revision(make_storage):
    storage = make_storage()
    storage.add_nodes([_node("taken")], [])
    result = mutations.add_nodes(
        storage,
        DefaultGraphAuthorizationHook(),
        [{"id": "taken", "type": "Actor", "name": "again"}],
        [],
        expect_absent=True,
    )
    assert result["error"] == "entity_revision_conflict"
    assert result["current_revision"] is None
    assert str(storage.nodes["taken"].revision) not in result["message"]


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
