"""Targeted tests for actor attribution in mutation results and events."""

import copy
import json
from unittest.mock import Mock

import pytest

from backend.agents.execution import ExecutionJob, ExecutionKind
from backend.agents.execution.memory_store import InMemoryExecutionStore
from backend.agents.execution.sqlite_store import SqliteExecutionStore
from backend.agents.governance.models import AutonomyLevel, Proposal, ProposalStatus
from backend.agents.governance.store import InMemoryProposalStore, SqliteProposalStore
from backend.core import Node, Edge, NodeType, RelationshipType
from backend.core.events import EventContext, EventAttribution, EventActorAttribution
from backend.core.history_store import GraphHistoryStore
from backend.core.session_store import (
    FileSessionPersistenceBackend,
    InMemorySessionPersistenceBackend,
    SessionStore,
)
from backend.service.actor_erasure import ActorErasureService, main

from typing import TYPE_CHECKING

from backend.runtime.authorization import use_request_authorization

if TYPE_CHECKING:
    from backend.service.service import GraphService


class TestMutationAttribution:
    def test_standalone_mutation_defaults_to_no_attribution(
        self, empty_service: "GraphService"
    ):
        captured_events = []
        empty_service.storage.add_system_listener(captured_events.append)

        result = empty_service.add_nodes(
            nodes=[
                {"id": "actor-standalone", "type": "Actor", "name": "Standalone Actor"}
            ],
            edges=[],
            event_origin="web-ui",
        )

        assert result["success"] is True
        assert "attribution" not in result
        assert len(captured_events) == 1
        assert captured_events[0].origin.event_origin == "web-ui"
        assert captured_events[0].origin.attribution is None

    def test_request_bound_mutation_includes_actor_and_scope_attribution(
        self, empty_service: "GraphService"
    ):
        captured_events = []
        empty_service.storage.add_system_listener(captured_events.append)

        with use_request_authorization(
            headers={
                "x-communityoverview-actor-id": "member-123",
                "x-communityoverview-actor-type": "member",
                "x-communityoverview-auth-source": "gateway",
                "x-communityoverview-workspace-id": "workspace-7",
                "x-communityoverview-workspace-kind": "team",
                "x-communityoverview-graph-id": "graph-42",
            }
        ):
            result = empty_service.add_nodes(
                nodes=[
                    {"id": "actor-request", "type": "Actor", "name": "Request Actor"}
                ],
                edges=[],
                event_origin="web-ui",
            )

        assert result["success"] is True
        assert result["attribution"] == {
            "actor": {
                "actor_id": "member-123",
                "actor_type": "member",
                "is_authenticated": True,
                "auth_source": "gateway",
                "source": "request",
            },
            "scope": {
                "workspace_id": "workspace-7",
                "workspace_kind": "team",
                "graph_id": "graph-42",
                "source": "request",
            },
        }

        assert len(captured_events) == 1
        event = captured_events[0]
        assert event.origin.event_origin == "web-ui"
        assert event.origin.attribution is not None
        assert event.origin.attribution.to_dict() == result["attribution"]


ACTOR = "member-private-123"
TOKEN = "erased"


@pytest.fixture(params=["memory", "sqlite"])
def erasure_stores(request, tmp_path):
    if request.param == "memory":
        runs, proposals = InMemoryExecutionStore(), InMemoryProposalStore()
    else:
        runs = SqliteExecutionStore(tmp_path / "runs.db")
        proposals = SqliteProposalStore(tmp_path / "proposals.db")
    yield runs, proposals
    for store in (runs, proposals):
        if hasattr(store, "close"):
            store.close()


def _run(store, actor=ACTOR):
    return store.enqueue(
        ExecutionJob(
            agent_id="agent-1",
            kind=ExecutionKind.EVENT,
            idempotency_key=f"run:{actor}",
            origin=f"agent:{actor}",
            payload={
                "event": {
                    "origin": {
                        "attribution": {"actor": {"actor_id": actor}},
                        "staged_by": actor,
                    },
                    "on_behalf_of": {"id": actor, "type": "user"},
                }
            },
            result={"attribution": {"actor": {"actor_id": actor}}},
        )
    )


def _proposal(store, actor=ACTOR):
    proposal = Proposal(
        agent_id="agent-1",
        tool="graph.update",
        input_args={"name": "Keep"},
        autonomy_level=AutonomyLevel.PROPOSE,
    )
    proposal.status = ProposalStatus.APPROVED
    proposal.decided_by = actor
    return store.create(proposal)


def test_erasure_replaces_all_attribution_and_preserves_graph_bytes(
    empty_storage, erasure_stores, tmp_path, caplog
):
    """All four stores erase attribution once; graph data is never rewritten."""
    runs, proposals = erasure_stores
    context = EventContext(
        attribution=EventAttribution(actor=EventActorAttribution(actor_id=ACTOR))
    )
    empty_storage.add_nodes(
        [
            Node(id="node-a", type=NodeType.ACTOR, name="A"),
            Node(id="node-b", type=NodeType.ACTOR, name="B"),
        ],
        [
            Edge(
                id="edge-a",
                source="node-a",
                target="node-b",
                type=RelationshipType.RELATES_TO,
            )
        ],
        event_context=context,
    )
    empty_storage.flush()
    graph_bytes = empty_storage.json_path.read_bytes()
    before_nodes = copy.deepcopy(empty_storage.nodes)
    before_edges = copy.deepcopy(empty_storage.edges)
    history = empty_storage._history_store
    before_history = history.get_recent(limit=100)
    run = _run(runs)
    other_run = _run(runs, "other-member")
    proposal = _proposal(proposals)
    other_proposal = _proposal(proposals, "other-member")
    sessions_backend = FileSessionPersistenceBackend(tmp_path / "sessions")
    sessions = SessionStore(sessions_backend)
    session = sessions.create()
    session.activity_log = [
        {"id": "activity-a", "actor": ACTOR, "inverse_op": {"client_id": ACTOR}},
        {"id": "activity-b", "actor": "other-member"},
    ]
    sessions.persist(session)
    original_session = sessions_backend.load(session.id)
    service = ActorErasureService(
        mutation_history=history,
        run_history=runs,
        proposals=proposals,
        session_activity=sessions,
    )
    report = service.erase_actor(ACTOR, TOKEN)
    assert report == {
        "counts": {
            "mutation_history": 3,
            "run_history": 1,
            "proposals": 1,
            "session_activity": 1,
        },
        "failed_stores": [],
    }
    assert service.erase_actor(ACTOR, TOKEN) == {
        "counts": dict.fromkeys(report["counts"], 0),
        "failed_stores": [],
    }
    assert ACTOR not in json.dumps(history.get_recent(limit=100))
    assert ACTOR not in json.dumps(runs.get(run.id).payload)
    assert runs.get(run.id).origin == f"agent:{TOKEN}"
    assert ACTOR not in json.dumps(runs.get(run.id).result)
    assert proposals.get(proposal.id).decided_by == TOKEN
    assert proposals.get(proposal.id).updated_at == proposal.updated_at
    assert runs.get(other_run.id).to_dict() == other_run.to_dict()
    assert proposals.get(other_proposal.id).to_dict() == other_proposal.to_dict()
    assert ACTOR not in json.dumps(session.activity_log)
    reloaded = SessionStore(sessions_backend).get(session.id)
    assert reloaded.activity_log == session.activity_log
    after_session = sessions_backend.load(session.id)
    assert {k: v for k, v in after_session.items() if k != "activity_log"} == {
        k: v for k, v in original_session.items() if k != "activity_log"
    }
    for old, new in zip(before_history, history.get_recent(limit=100)):
        assert old["before"] == new["before"] and old["after"] == new["after"]
    empty_storage.flush()
    assert empty_storage.json_path.read_bytes() == graph_bytes
    assert empty_storage.nodes == before_nodes
    assert empty_storage.edges == before_edges
    assert ACTOR not in json.dumps(report) + caplog.text
    assert TOKEN not in json.dumps(report) + caplog.text


def test_erasure_handles_absent_stores_and_unloaded_sessions(tmp_path):
    """Missing stores return zero, and cold persisted sessions are included."""
    backend = InMemorySessionPersistenceBackend()
    sessions = SessionStore(backend)
    session = sessions.create()
    session.activity_log = [{"id": "a", "actor": ACTOR}]
    sessions.persist(session)
    cold = SessionStore(backend)
    report = ActorErasureService(session_activity=cold).erase_actor(ACTOR, TOKEN)
    assert report["counts"] == {
        "mutation_history": 0,
        "run_history": 0,
        "proposals": 0,
        "session_activity": 1,
    }
    assert cold.get(session.id).activity_log[0]["actor"] == TOKEN
    missing = GraphHistoryStore(tmp_path / "absent" / "history.ndjson")
    assert missing.erase_actor(ACTOR, TOKEN) == 0
    assert not missing.history_path.parent.exists()


def test_erasure_rejects_invalid_inputs_before_writing():
    """Validation never echoes personal inputs and runs before any store call."""
    store = Mock()
    service = ActorErasureService(run_history=store)
    for actor, token in [("", TOKEN), (ACTOR, ""), (ACTOR, ACTOR), (None, TOKEN)]:
        with pytest.raises(ValueError) as exc:
            service.erase_actor(actor, token)
        assert ACTOR not in str(exc.value)
    store.erase_actor.assert_not_called()


def test_erasure_reports_failures_without_exception_content(erasure_stores, caplog):
    """One failed store does not block the others or disclose its exception."""
    runs, proposals = erasure_stores
    _proposal(proposals)
    broken = Mock()
    broken.erase_actor.side_effect = RuntimeError(ACTOR)
    report = ActorErasureService(run_history=broken, proposals=proposals).erase_actor(
        ACTOR, TOKEN
    )
    assert report["counts"]["run_history"] is None
    assert report["counts"]["proposals"] == 1
    assert report["failed_stores"] == ["run_history"]
    assert ACTOR not in json.dumps(report) + caplog.text


def test_history_erasure_is_atomic_on_malformed_records(tmp_path):
    """An unreadable record fails the store without committing an incomplete rewrite."""
    path = tmp_path / "history.ndjson"
    history = GraphHistoryStore(path)
    history.append_record({"actor": ACTOR})
    history.close()
    with path.open("ab") as out:
        out.write(b'{"invalid":')
    before = path.read_bytes()
    report = ActorErasureService(mutation_history=history).erase_actor(ACTOR, TOKEN)
    assert report["failed_stores"] == ["mutation_history"]
    assert path.read_bytes() == before
    assert not list(tmp_path.glob("graph_history_*"))


def test_session_erasure_does_not_skip_corrupt_persisted_files(tmp_path):
    """Erasure must report corrupt sessions even when metadata listing omits them."""
    (tmp_path / "1234-5678.json").write_text('{"invalid":')
    sessions = SessionStore(FileSessionPersistenceBackend(tmp_path))
    assert sessions.list_meta() == []
    report = ActorErasureService(session_activity=sessions).erase_actor(ACTOR, TOKEN)
    assert report["failed_stores"] == ["session_activity"]


def test_history_append_after_erasure_uses_replaced_sidecar(tmp_path):
    """A cached append handle cannot restore or append to the replaced file."""
    history = GraphHistoryStore(tmp_path / "history.ndjson")
    history.append_record({"actor": ACTOR})
    assert history.erase_actor(ACTOR, TOKEN) == 1
    history.append_record({"actor": "other"})
    assert history.get_recent() == [{"actor": "other"}, {"actor": TOKEN}]
    history.close()


def test_cli_erases_durable_stores_and_outputs_only_counts(
    tmp_path, monkeypatch, capsys
):
    """CLI changes survive reopening and repeated invocations return zero counts."""
    monkeypatch.delenv("AGENTS_RUN_HISTORY_DB", raising=False)
    monkeypatch.delenv("AGENTS_GOVERNANCE_DB", raising=False)
    runs = SqliteExecutionStore(tmp_path / "runs.db")
    proposals = SqliteProposalStore(tmp_path / "proposals.db")
    run, proposal = _run(runs), _proposal(proposals)
    runs.close()
    proposals.close()
    args = [
        "--actor-id",
        ACTOR,
        "--replacement-token",
        TOKEN,
        "--run-history-db",
        str(tmp_path / "runs.db"),
        "--governance-db",
        str(tmp_path / "proposals.db"),
        "--history-path",
        str(tmp_path / "absent.ndjson"),
        "--sessions-dir",
        str(tmp_path / "absent-sessions"),
    ]
    assert main(args) == 0
    captured = capsys.readouterr()
    assert ACTOR not in captured.out + captured.err
    assert TOKEN not in captured.out + captured.err
    assert json.loads(captured.out)["counts"] == {
        "mutation_history": 0,
        "run_history": 1,
        "proposals": 1,
        "session_activity": 0,
    }
    assert main(args) == 0
    assert set(json.loads(capsys.readouterr().out)["counts"].values()) == {0}
    runs = SqliteExecutionStore(tmp_path / "runs.db")
    proposals = SqliteProposalStore(tmp_path / "proposals.db")
    assert runs.get(run.id).payload["event"]["origin"]["staged_by"] == TOKEN
    assert proposals.get(proposal.id).decided_by == TOKEN
    runs.close()
    proposals.close()
    assert not (tmp_path / "absent.ndjson").exists()
    assert not (tmp_path / "absent-sessions").exists()


def test_cli_failure_and_argument_errors_do_not_echo_inputs(
    tmp_path, monkeypatch, capsys
):
    """Failure exits are nonzero and neither decoder errors nor argv are echoed."""
    monkeypatch.delenv("AGENTS_RUN_HISTORY_DB", raising=False)
    monkeypatch.delenv("AGENTS_GOVERNANCE_DB", raising=False)
    damaged = tmp_path / "history.ndjson"
    damaged.write_text(ACTOR)
    assert (
        main(
            [
                "--actor-id",
                ACTOR,
                "--replacement-token",
                TOKEN,
                "--history-path",
                str(damaged),
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert ACTOR not in captured.out + captured.err
    assert json.loads(captured.out)["failed_stores"] == ["mutation_history"]
    with pytest.raises(SystemExit) as exc:
        main(["--unexpected", ACTOR])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert ACTOR not in captured.out + captured.err


def test_sqlite_erasure_rolls_back_on_invalid_run_payload(tmp_path):
    """A later invalid run cannot leave earlier updates committed in the same store."""
    store = SqliteExecutionStore(tmp_path / "runs.db")
    try:
        run = _run(store)
        invalid = _run(store, "other-member")
        store._conn.execute(
            "UPDATE execution_jobs SET payload = ? WHERE id = ?", ("{", invalid.id)
        )
        store._conn.commit()
        report = ActorErasureService(run_history=store).erase_actor(ACTOR, TOKEN)
        assert report["failed_stores"] == ["run_history"]
        assert store.get(run.id).payload == run.payload
    finally:
        store.close()


def test_session_erasure_updates_different_loaded_and_persisted_records():
    """Both copies are erased without persisting unrelated unsaved session changes."""
    backend = InMemorySessionPersistenceBackend()
    store = SessionStore(backend)
    session = store.create()
    session.activity_log = [{"id": "disk-record", "actor": ACTOR}]
    store.persist(session)
    session.activity_log = [{"id": "live-record", "actor": ACTOR}]
    session.name = "unsaved name"
    assert store.erase_actor(ACTOR, TOKEN) == 2
    assert session.activity_log[0]["actor"] == TOKEN
    assert backend.load(session.id)["activity_log"][0]["actor"] == TOKEN
    assert backend.load(session.id)["name"] != session.name
    store.persist(session)
    assert ACTOR not in json.dumps(backend.load(session.id))
    assert store.erase_actor(ACTOR, TOKEN) == 0


def test_cli_rejects_wrong_store_path_type_without_disclosure(
    tmp_path, monkeypatch, capsys
):
    """An existing file passed as a sessions directory must not look like an empty store."""
    monkeypatch.delenv("AGENTS_RUN_HISTORY_DB", raising=False)
    monkeypatch.delenv("AGENTS_GOVERNANCE_DB", raising=False)
    path = tmp_path / ACTOR
    path.write_text("{}")
    assert (
        main(
            [
                "--actor-id",
                ACTOR,
                "--replacement-token",
                TOKEN,
                "--sessions-dir",
                str(path),
            ]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert ACTOR not in captured.out + captured.err
    assert json.loads(captured.out)["failed_stores"] == ["session_activity"]


def test_erasure_preserves_entity_and_execution_content(erasure_stores, tmp_path):
    """Attribution-like keys in user content must survive erasure unchanged."""
    runs, _ = erasure_stores
    content = {
        "actor": ACTOR,
        "actor_id": ACTOR,
        "client_id": ACTOR,
        "origin": ACTOR,
        "event_origin": f"agent:{ACTOR}",
        "staged_by": ACTOR,
        "decided_by": ACTOR,
        "on_behalf_of": {"id": ACTOR},
        "attribution": {"actor": {"actor_id": ACTOR}},
        "metadata": {"actor": ACTOR},
    }
    history = GraphHistoryStore(tmp_path / "history.ndjson")
    record = {
        "before": copy.deepcopy(content),
        "after": copy.deepcopy(content),
        "patch": copy.deepcopy(content),
        "attribution": {"actor": {"actor_id": ACTOR}},
    }
    history.append_record(record)
    run = _run(runs)
    # Store copies on enqueue, so add content before creating this job.
    job = ExecutionJob(
        agent_id="agent-1",
        kind=ExecutionKind.EVENT,
        idempotency_key="content",
        payload={
            "event": {
                "entity": copy.deepcopy(content),
                "origin": {"attribution": {"actor": {"actor_id": ACTOR}}},
            },
            "data": copy.deepcopy(content),
        },
        result={"data": copy.deepcopy(content), "attribution": record["attribution"]},
    )
    content_run = runs.enqueue(job)
    backend = InMemorySessionPersistenceBackend()
    sessions = SessionStore(backend)
    session = sessions.create()
    session.activity_log = [
        {
            "id": "content",
            "actor": ACTOR,
            "before": copy.deepcopy(content),
            "after": copy.deepcopy(content),
            "inverse_op": {"client_id": ACTOR, "data": copy.deepcopy(content)},
        }
    ]
    sessions.persist(session)
    try:
        service = ActorErasureService(
            mutation_history=history, run_history=runs, session_activity=sessions
        )
        assert service.erase_actor(ACTOR, TOKEN)["counts"] == {
            "mutation_history": 1,
            "run_history": 2,
            "proposals": 0,
            "session_activity": 1,
        }
        cleaned = history.get_recent()[0]
        for key in ("before", "after", "patch"):
            assert cleaned[key] == content
        assert cleaned["attribution"]["actor"]["actor_id"] == TOKEN
        cleaned_run = runs.get(content_run.id)
        assert cleaned_run.payload["event"]["entity"] == content
        assert cleaned_run.payload["data"] == content
        assert cleaned_run.result["data"] == content
        assert cleaned_run.result["attribution"]["actor"]["actor_id"] == TOKEN
        assert (
            cleaned_run.payload["event"]["origin"]["attribution"]["actor"]["actor_id"]
            == TOKEN
        )
        assert runs.get(run.id).origin == f"agent:{TOKEN}"
        for activity in (
            session.activity_log,
            backend.load(session.id)["activity_log"],
        ):
            assert activity[0]["actor"] == TOKEN
            assert activity[0]["inverse_op"]["client_id"] == TOKEN
            assert activity[0]["inverse_op"]["data"] == content
            assert activity[0]["before"] == activity[0]["after"] == content
        assert set(service.erase_actor(ACTOR, TOKEN)["counts"].values()) == {0}
    finally:
        history.close()


@pytest.mark.parametrize("cold", [False, True])
def test_erasure_scrubs_annotation_history_and_delete_undo(tmp_path, cold):
    """Another actor's delete must not preserve or resurrect creator attribution."""
    from backend.core.session_manager import SessionManager

    backend = FileSessionPersistenceBackend(tmp_path / "sessions")
    store = SessionStore(backend)
    manager = SessionManager(store)
    session = manager.create_session()
    manager.upsert_annotation(
        session.id, ACTOR, {"id": "note", "type": "note", "text": "original"}
    )
    manager.update_annotation(
        session.id, "other", {"id": "note", "type": "note", "text": "edited"}
    )
    manager.delete_annotation(session.id, "other", "note")
    store.persist(session)
    original = copy.deepcopy(session.activity_log)
    if cold:
        store = SessionStore(backend)
        manager = SessionManager(store)
    assert store.erase_actor(ACTOR, TOKEN) == 3
    session = store.get(session.id)
    expected = copy.deepcopy(original)
    expected[0]["actor"] = TOKEN
    for record in expected:
        for key in ("before", "after"):
            if isinstance(record[key], dict):
                record[key]["created_by"] = TOKEN
        if "annotation" in record["inverse_op"]:
            record["inverse_op"]["annotation"]["created_by"] = TOKEN
    assert session.activity_log == expected
    assert backend.load(session.id)["activity_log"] == expected
    assert ACTOR not in json.dumps(expected)
    assert store.erase_actor(ACTOR, TOKEN) == 0
    manager.undo_last_action(session.id, "other")
    assert session.state["annotations"][0]["created_by"] == TOKEN
    assert session.state["annotations"][0]["text"] == "edited"
    store.persist(session)
    assert ACTOR not in json.dumps(backend.load(session.id))


def test_erasure_limits_annotation_traversal_to_defined_fields():
    from backend.core.actor_attribution import replace_attribution

    content = {"created_by": ACTOR, "text": ACTOR, "data": {"created_by": ACTOR}}
    record = {
        "affected": {"kind": "annotation"},
        "before": copy.deepcopy(content),
        "after": copy.deepcopy(content),
        "inverse_op": {
            "op": "annotation_updated",
            "annotation": copy.deepcopy(content),
        },
    }
    cleaned = replace_attribution(record, ACTOR, TOKEN, envelope="activity")
    for snapshot in (
        cleaned["before"],
        cleaned["after"],
        cleaned["inverse_op"]["annotation"],
    ):
        assert snapshot == {**content, "created_by": TOKEN}
    # Identical keys in graph snapshots and non-annotation activity are content.
    assert replace_attribution(record, ACTOR, TOKEN) == record
    record["affected"]["kind"] = "node"
    record["inverse_op"]["op"] = "node_moved"
    assert replace_attribution(record, ACTOR, TOKEN, envelope="activity") == record
