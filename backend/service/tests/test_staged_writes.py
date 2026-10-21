"""Staged writes (docs/SESSION_OVERLAY_CONTRACT.md §5, §6, §9, §13, §14).

Run over the file backend and, when a server is configured, PostgreSQL: the
graph service decides whether a write stages, and the layer store holds it.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from backend.core import Edge, GraphStorage, Node, layer_store
from backend.core.session_manager import (
    GRAPH_STAGING_CLIENT_ID,
    SessionManager,
    SessionNotFound,
)
from backend.core.session_store import (
    InMemorySessionPersistenceBackend,
    OpError,
    SessionStore,
)
from backend.core.storage_backends import FileGraphPersistenceBackend
from backend.core.tests.test_persistence_contract_postgres import (
    DSN,
    _server_reachable,
    psycopg,
)
from backend.core.write_modes import WriteModeRefused, WriteModes
from backend.runtime.authorization import (
    DefaultGraphAuthorizationHook,
    GraphAccessNarrowing,
    GraphAuthorizationDecision,
)
from backend.runtime.session_context import bind_session
from backend.service import GraphService


class RecordingHook:
    """Allows everything, and keeps every context it was asked about."""

    def __init__(self):
        self.contexts = []

    def evaluate(self, context):
        self.contexts.append(context)
        return GraphAuthorizationDecision(allowed=True)


class World:
    def __init__(self, storage: GraphStorage, *, max_entries: int = 1000):
        self.storage = storage
        self.events = []
        storage.add_system_listener(self.events.append)
        self.write_modes = WriteModes(storage.layer_store, max_entries=max_entries)
        self.store = SessionStore(
            InMemorySessionPersistenceBackend(),
            staged_status=self.write_modes.staged_status,
        )
        self.manager = SessionManager(self.store, write_modes=self.write_modes)
        self.hook = RecordingHook()
        self.service = GraphService(storage, authorization_hook=self.hook)
        self.service.attach_sessions(self.manager)
        self.published = []
        original = self.manager.bus.publish

        def publish(session_id, event):
            self.published.append((session_id, event))
            return original(session_id, event)

        self.manager.bus.publish = publish

    def staged_session(self) -> str:
        return self.manager.create_session("workshop", "staged").id

    def layer(self, session_id: str):
        return {
            (e["kind"], e["entity_id"]): e
            for e in self.storage.layer_store.load_layer(session_id)
        }

    def main(self):
        return (
            {nid: n.to_dict() for nid, n in self.storage.nodes.items()},
            {eid: e.to_dict() for eid, e in self.storage.edges.items()},
        )


def _seed(storage: GraphStorage) -> None:
    from backend.core import Edge, Node

    storage.add_nodes(
        [
            Node(id="a", type="Actor", name="Alpha", metadata={"keep": 1, "x": 2}),
            Node(id="b", type="Actor", name="Beta"),
        ],
        [Edge(id="ab", source="a", target="b", type="RELATES_TO")],
    )


@pytest.fixture(params=["file", "postgres"])
def world(request, tmp_path):
    if request.param == "file":
        backend = FileGraphPersistenceBackend(
            tmp_path / "graph.json", staged_sessions=True
        )
        storage = GraphStorage(
            str(tmp_path / "graph.json"), persistence_backend=backend
        )
        _seed(storage)
        yield World(storage)
        storage.shutdown_events()
        return
    if not _server_reachable():
        pytest.skip("no PostgreSQL server configured")
    from backend.core.postgres_backend import PostgresGraphPersistenceBackend

    schema = f"co_staged_{uuid.uuid4().hex[:16]}"
    backend = PostgresGraphPersistenceBackend(DSN, schema=schema, staged_sessions=True)
    storage = GraphStorage(persistence_backend=backend)
    storage.WRITE_RESOLVE_DELAY_SECONDS = 0.0
    _seed(storage)
    try:
        yield World(storage)
    finally:
        storage.shutdown_events()
        backend.close()
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


# -- I1: a staged write changes nothing in the graph -----------------------------


def test_every_staged_write_leaves_the_graph_and_its_events_untouched(world):
    sid = world.staged_session()
    before = world.main()
    world.events.clear()
    svc = world.service
    with bind_session(sid):
        results = [
            svc.add_nodes(
                [{"id": "c", "type": "Actor", "name": "Gamma"}],
                [{"id": "ca", "source": "c", "target": "a", "type": "RELATES_TO"}],
            ),
            svc.update_node("a", {"description": "staged"}),
            svc.add_edge("b", "c", type="RELATES_TO"),
            svc.update_edge("ab", {"label": "staged"}),
            svc.archive_nodes(["b"]),
            svc.archive_edges(["ab"]),
            svc.delete_edge("ca"),
            svc.delete_edges(["ab"], confirmed=True),
            svc.delete_nodes(["b"], confirmed=True),
        ]
    for result in results:
        assert result["success"] is True, result
        assert result["staged"] is True
    assert world.main() == before
    assert world.events == []
    assert world.layer(sid)


def test_a_staged_delete_leaves_the_nodes_attachments_alone(world, monkeypatch):
    from backend.service import attachments

    deleted = []
    monkeypatch.setattr(
        attachments,
        "delete_attachments_of_nodes",
        lambda settings, ids: deleted.extend(ids),
    )
    sid = world.staged_session()
    with bind_session(sid):
        result = world.service.delete_nodes(["a"], confirmed=True)
    assert result["staged"] is True and result["deleted_node_ids"] == ["a"]
    assert deleted == []
    assert world.storage.get_node("a") is not None


# -- I5: no session, and a direct session, behave as before -----------------------


def test_a_direct_session_writes_to_the_graph_like_no_session(world):
    session = world.manager.create_session("plain")
    with bind_session(session.id):
        result = world.service.update_node("a", {"description": "direct"})
    assert result["success"] is True and "staged" not in result
    assert world.storage.get_node("a").description == "direct"
    assert world.layer(session.id) == {}


def test_without_a_session_the_hook_context_carries_no_session(world):
    world.service.update_node("a", {"description": "plain"})
    context = world.hook.contexts[-1]
    assert (context.action, context.graph_operation, context.session_id) == (
        "mutate",
        "",
        "",
    )


# -- I2: a session that does not resolve is refused, never written directly -------


@pytest.mark.parametrize("how", ["unknown", "retired", "unattached"])
def test_a_session_that_does_not_resolve_is_refused_and_nothing_lands(world, how):
    service = world.service
    if how == "unknown":
        sid = "1111-2222-3333-4444"
    elif how == "retired":
        sid = world.staged_session()
        world.manager.write_modes.retire(sid, require_empty=False)
    else:
        sid = world.staged_session()
        service = GraphService(world.storage)
    before = world.main()
    with bind_session(sid):
        result = service.add_nodes([{"id": "z", "type": "Actor", "name": "Z"}], [])
    assert result["success"] is False
    assert result["error"] == "session_not_found"
    assert result["added_node_ids"] == []
    assert world.main() == before


# -- §6.2 collapsing ---------------------------------------------------------------


def test_create_then_update_stays_a_create_with_the_new_values(world):
    sid = world.staged_session()
    with bind_session(sid):
        world.service.add_nodes([{"id": "c", "type": "Actor", "name": "C"}], [])
        world.service.update_node("c", {"name": "C2"})
    entry = world.layer(sid)[("node", "c")]
    assert entry["action"] == "create" and entry["base"] is None
    assert entry["staged"]["name"] == "C2"


def test_create_then_delete_removes_the_entry(world):
    sid = world.staged_session()
    with bind_session(sid):
        world.service.add_nodes([{"id": "c", "type": "Actor", "name": "C"}], [])
        result = world.service.delete_nodes(["c"], confirmed=True)
    assert result["removed_entries"] == [["node", "c"]]
    assert world.layer(sid) == {}


def test_repeated_updates_keep_the_first_base_and_the_latest_values(world):
    sid = world.staged_session()
    base = world.storage.get_node("a").to_dict()
    with bind_session(sid):
        world.service.update_node(
            "a", {"name": "A1", "metadata": {"x": 3}}, metadata_merge=True
        )
        world.service.update_node(
            "a", {"description": "d", "metadata": {"x": None}}, metadata_merge=True
        )
    entry = world.layer(sid)[("node", "a")]
    assert entry["action"] == "update"
    assert entry["base"]["name"] == base["name"]
    assert entry["base_revision"] == base["revision"]
    assert entry["staged"] == {
        "name": "A1",
        "description": "d",
        "metadata": {"x": None},
    }


def test_update_then_delete_is_a_delete_with_the_first_base(world):
    sid = world.staged_session()
    with bind_session(sid):
        world.service.update_node("a", {"name": "A1"})
        world.service.delete_nodes(["a"], confirmed=True)
    entry = world.layer(sid)[("node", "a")]
    assert entry["action"] == "delete" and entry["staged"] is None
    assert entry["base"]["name"] == "Alpha"


def test_delete_then_create_is_an_update_nulling_the_base_metadata_it_lacks(world):
    sid = world.staged_session()
    with bind_session(sid):
        world.service.delete_nodes(["a"], confirmed=True)
        world.service.add_nodes(
            [{"id": "a", "type": "Actor", "name": "New", "metadata": {"keep": 1}}],
            [],
        )
    entry = world.layer(sid)[("node", "a")]
    assert entry["action"] == "update"
    assert entry["base"]["name"] == "Alpha"
    assert entry["staged"]["name"] == "New"
    assert entry["staged"]["metadata"] == {"x": None}


# -- §6.3 validation against the composed view ---------------------------------------


def test_a_node_delete_tombstones_every_incident_edge_in_the_same_op(world):
    sid = world.staged_session()
    with bind_session(sid):
        world.service.add_nodes(
            [{"id": "c", "type": "Actor", "name": "C"}],
            [{"id": "cb", "source": "c", "target": "b", "type": "RELATES_TO"}],
        )
        result = world.service.delete_nodes(["b"], confirmed=True)
    assert sorted(result["affected_edge_ids"]) == ["ab", "cb"]
    layer = world.layer(sid)
    assert layer[("edge", "ab")]["action"] == "delete"
    assert ("edge", "cb") not in layer  # a staged create, so simply gone
    assert len({e["session_seq"] for e in result["entries"]}) == 1


def test_an_edge_to_a_tombstoned_node_is_refused(world):
    sid = world.staged_session()
    with bind_session(sid):
        world.service.delete_nodes(["b"], confirmed=True)
        result = world.service.add_edge("a", "b")
    assert result["success"] is False
    assert set(world.layer(sid)) == {("node", "b"), ("edge", "ab")}


def test_an_edge_to_a_staged_node_is_accepted(world):
    sid = world.staged_session()
    with bind_session(sid):
        world.service.add_nodes([{"id": "c", "type": "Actor", "name": "C"}], [])
        result = world.service.add_edge("a", "c")
    assert result["success"] is True and result["staged"] is True
    assert result["edge"]["target"] == "c"


def test_a_staged_update_is_validated_like_a_direct_one(world):
    sid = world.staged_session()
    with bind_session(sid):
        result = world.service.update_node("a", {"description": "x" * 2001})
    assert result["success"] is False
    assert world.layer(sid) == {}


def test_an_expected_revision_is_checked_against_the_main_graph_revision(world):
    sid = world.staged_session()
    current = world.storage.get_node("a").revision
    with bind_session(sid):
        refused = world.service.update_node(
            "a", {"name": "N"}, expected_revision=current + 7
        )
        staged = world.service.update_node(
            "a", {"name": "N"}, expected_revision=current
        )
        again = world.service.update_node("a", {"name": "M"}, expected_revision=current)
    assert refused["error"] == "entity_revision_conflict"
    assert refused["current_revision"] == current
    assert staged["staged"] is True and again["staged"] is True


# -- §6.4 the cap -----------------------------------------------------------------------


def test_an_op_past_the_cap_is_refused_whole_with_layer_full(tmp_path):
    backend = FileGraphPersistenceBackend(tmp_path / "g.json", staged_sessions=True)
    storage = GraphStorage(str(tmp_path / "g.json"), persistence_backend=backend)
    _seed(storage)
    world = World(storage, max_entries=2)
    sid = world.staged_session()
    with bind_session(sid):
        world.service.update_node("a", {"name": "A1"})
        result = world.service.add_nodes(
            [
                {"id": "c", "type": "Actor", "name": "C"},
                {"id": "d", "type": "Actor", "name": "D"},
            ],
            [],
        )
    assert result["success"] is False and result["error"] == "layer_full"
    assert result["limit"] == 2
    assert list(world.layer(sid)) == [("node", "a")]


# -- §9.2 and §15: the session sequence --------------------------------------------------


def test_each_staging_op_is_one_sequenced_session_op(world):
    sid = world.staged_session()
    world.published.clear()
    with bind_session(sid):
        first = world.service.update_node("a", {"name": "A1"})
        second = world.service.update_node("b", {"name": "B1"})
    assert second["session_seq"] == first["session_seq"] + 1
    ops = [event for _, event in world.published if event.get("type") == "op"]
    assert [op["op"]["op"] for op in ops] == ["graph_change_staged"] * 2
    assert {op["client_id"] for op in ops} == {GRAPH_STAGING_CLIENT_ID}
    assert ops[0]["op"]["entries"][0]["session_seq"] == first["session_seq"]


def test_the_fanned_out_op_carries_what_changed_not_the_main_graph_base(world):
    sid = world.staged_session()
    world.published.clear()
    with bind_session(sid):
        result = world.service.update_node("a", {"description": "staged"})
    op = next(e["op"] for _, e in world.published if e.get("type") == "op")
    assert op["entries"][0]["staged"] == {"description": "staged"}
    assert "base" not in op["entries"][0]
    assert "staged_by" not in op["entries"][0]
    assert all(
        "base" not in entry and "staged_by" not in entry
        for entry in world.store.ops_since(sid, 0)[0]["entries"]
    )
    # Nor does the writer's own answer carry the base; only the layer does.
    assert "base" not in result["entries"][0]
    assert result["entries"][0]["staged"] == {"description": "staged"}
    assert world.layer(sid)[("node", "a")]["base"]["name"] == "Alpha"


def test_a_session_materialised_under_a_staged_default_stays_direct(tmp_path):
    backend = FileGraphPersistenceBackend(tmp_path / "g.json", staged_sessions=True)
    storage = GraphStorage(str(tmp_path / "g.json"), persistence_backend=backend)
    _seed(storage)
    world = World(storage)
    world.manager.write_modes._default = "staged"
    session, created = world.manager.get_or_create("1111-2222-3333-4444")
    assert created
    assert storage.layer_store.staged_status(session.id) == "none"
    assert world.manager.write_mode_of(session)["write_mode"] == "direct"


def test_a_stale_expected_session_seq_stages_nothing(world):
    sid = world.staged_session()
    with bind_session(sid):
        first = world.service.update_node("a", {"name": "A1"})
        refused = world.service.update_node(
            "b", {"name": "B1"}, expected_session_seq=first["session_seq"] - 1
        )
    assert refused["error"] == "session_seq_conflict"
    assert refused["session_seq"] == first["session_seq"]
    assert ("node", "b") not in world.layer(sid)


def test_expected_session_seq_without_a_session_is_refused(tmp_path):
    storage = GraphStorage(str(tmp_path / "g.json"))
    result = GraphService(storage).update_node("x", {}, expected_session_seq=1)
    assert result["error"] == "session_seq_without_session"


def test_clients_cannot_post_server_only_session_ops(world):
    import asyncio

    sid = world.staged_session()
    for op in ("graph_change_staged", "write_mode_changed"):
        with pytest.raises(OpError):
            asyncio.run(
                world.manager.apply_ops(
                    sid, "browser", None, [{"op": op, "entries": [], "removals": []}]
                )
            )


# -- §14: the hook ---------------------------------------------------------------


def test_a_staged_write_asks_the_hook_as_mutate_with_stage_and_the_session(world):
    sid = world.staged_session()
    with bind_session(sid):
        world.service.update_node("a", {"name": "A1"})
    context = world.hook.contexts[-1]
    assert (context.action, context.target) == ("mutate", "update_node")
    assert (context.graph_operation, context.session_id) == ("stage", sid)


def test_a_read_only_hook_refuses_staging_like_a_direct_write(world, monkeypatch):
    monkeypatch.setenv("COMMUNITYOVERVIEW_AUTHORIZATION_MODE", "read-only")
    sid = world.staged_session()
    service = GraphService(
        world.storage, authorization_hook=DefaultGraphAuthorizationHook()
    )
    service.attach_sessions(world.manager)
    with bind_session(sid):
        result = service.add_nodes([{"id": "z", "type": "Actor", "name": "Z"}], [])
    assert result["error_code"] == "access_denied"
    assert world.layer(sid) == {}


# -- §5: write modes ------------------------------------------------------------------


def test_staged_mode_is_refused_where_the_backend_holds_no_layers():
    manager = SessionManager(SessionStore(InMemorySessionPersistenceBackend()))
    with pytest.raises(WriteModeRefused) as raised:
        manager.create_session("x", "staged")
    assert raised.value.code == "staged_unsupported"
    assert manager.list_sessions() == []


def test_a_staged_session_never_becomes_direct(world):
    import asyncio

    sid = world.staged_session()
    with pytest.raises(WriteModeRefused) as raised:
        asyncio.run(world.manager.set_write_mode(sid, "direct"))
    assert raised.value.code == "write_mode_irreversible"
    assert world.write_modes.mode_of(sid) == "staged"


def test_switching_to_staged_records_first_then_fans_out(world):
    import asyncio

    session = world.manager.create_session("plain")
    world.published.clear()
    asyncio.run(world.manager.set_write_mode(session.id, "staged"))
    assert world.storage.layer_store.staged_status(session.id) == "active"
    described = world.manager.describe(world.manager.get_session(session.id))
    assert (described["write_mode"], described["write_mode_source"]) == (
        "staged",
        "explicit",
    )
    ops = [e["op"]["op"] for _, e in world.published if e.get("type") == "op"]
    assert ops == ["write_mode_changed"]
    with bind_session(session.id):
        assert world.service.update_node("a", {"name": "A1"})["staged"] is True


def test_the_document_cannot_make_a_staged_session_direct(world):
    sid = world.staged_session()
    world.manager.get_session(sid).write_mode = "direct"
    with bind_session(sid):
        result = world.service.update_node("a", {"name": "A1"})
    assert result["staged"] is True
    assert world.storage.get_node("a").name == "Alpha"


# -- §5.3 and §13: retirement and deletion --------------------------------------------


def test_deleting_a_session_with_staged_work_needs_discard_staged(world):
    import asyncio

    sid = world.staged_session()
    with bind_session(sid):
        world.service.update_node("a", {"name": "A1"})
    with pytest.raises(layer_store.LayerNotEmpty):
        asyncio.run(world.manager.delete_session(sid))
    assert world.manager.get_session(sid) is not None
    assert world.layer(sid)
    world.published.clear()
    assert asyncio.run(world.manager.delete_session(sid, discard_staged=True))
    assert world.storage.layer_store.staged_status(sid) == "retired"
    assert world.layer(sid) == {}
    assert world.published[-1][1] == {
        "type": "session_deleted",
        "deleted_by": None,
        "write_mode": "staged",
    }


def test_a_retired_id_is_never_created_again(world):
    import asyncio

    sid = world.staged_session()
    assert asyncio.run(world.manager.delete_session(sid))
    with pytest.raises(SessionNotFound):
        world.manager.get_or_create(sid)
    with pytest.raises(SessionNotFound):
        asyncio.run(world.manager.rename_session(sid, "again"))
    assert world.manager.get_session(sid) is None


def test_new_session_ids_skip_every_id_with_a_staged_record(world, monkeypatch):
    import asyncio

    import backend.core.session_store as session_store

    retired = world.staged_session()
    assert asyncio.run(world.manager.delete_session(retired))
    digits = iter([int(p) for p in retired.split("-")] + [5555, 6666, 7777, 8888])
    monkeypatch.setattr(session_store.secrets, "randbelow", lambda n: next(digits))
    assert world.manager.create_session("next").id == "5555-6666-7777-8888"


# -- §9.1: an approved agent proposal stages in its session ----------------------------


def test_an_agent_proposal_made_in_a_staged_session_stages_when_approved(world):
    from backend.agents.governance.gate import AutonomyGate
    from backend.agents.governance.manager import GovernanceManager
    from backend.agents.governance.models import AutonomyLevel
    from backend.agents.governance.store import InMemoryProposalStore

    class Loader:
        def get_tool(self, name):
            return type("T", (), {"original_name": name})()

    store = InMemoryProposalStore()
    gate = AutonomyGate(
        autonomy_level=AutonomyLevel.ACT_AFTER_APPROVAL,
        tool_allowlist=None,
        mcp_loader=Loader(),
        proposal_store=store,
        agent_id="agent-1",
    )
    sid = world.staged_session()
    with bind_session(sid):
        proposed = gate.wrap(lambda *_: None)(
            "update_node", {"node_id": "a", "updates": {"name": "A1"}}
        )
    manager = GovernanceManager(
        store,
        executor_factory=lambda _agent: (
            lambda tool, args: getattr(world.service, tool)(**args)
        ),
    )
    approved = manager.approve(proposed["proposal_id"])
    assert approved["status"] == "applied"
    assert approved["session_id"] == sid
    assert approved["apply_result"]["staged"] is True
    assert world.storage.get_node("a").name == "Alpha"


def test_the_layer_file_holds_only_what_changed(tmp_path):
    backend = FileGraphPersistenceBackend(tmp_path / "g.json", staged_sessions=True)
    storage = GraphStorage(str(tmp_path / "g.json"), persistence_backend=backend)
    _seed(storage)
    world = World(storage)
    sid = world.staged_session()
    with bind_session(sid):
        world.service.update_node("a", {"name": "A1"})
    held = json.loads(Path(backend.layers_path).read_text())
    assert json.dumps(held).count('"entity_id"') == 1


def test_a_staged_session_whose_record_cannot_be_read_is_refused_not_direct(
    tmp_path,
):
    store = SessionStore(InMemorySessionPersistenceBackend())
    manager = SessionManager(store)
    session = manager.create_session("was staged")
    session.write_mode = "staged"
    storage = GraphStorage(str(tmp_path / "g.json"))
    _seed(storage)
    service = GraphService(storage)
    service.attach_sessions(manager)
    with bind_session(session.id):
        result = service.update_node("a", {"name": "A1"})
    assert result["error"] == "staged_unsupported"
    assert storage.get_node("a").name == "Alpha"


@pytest.mark.parametrize("held", [False, True])
def test_an_instance_without_staging_refuses_sessions_once_the_store_holds_records(
    tmp_path, held
):
    storage = GraphStorage(str(tmp_path / "g.json"))
    _seed(storage)
    asked = []

    def records():
        asked.append(1)
        return held

    write_modes = WriteModes(None, unreadable_records=records)
    manager = SessionManager(
        SessionStore(InMemorySessionPersistenceBackend()), write_modes=write_modes
    )
    sid = manager.create_session("joined").id
    service = GraphService(storage)
    service.attach_sessions(manager)
    with bind_session(sid):
        result = service.update_node("a", {"name": "A1"})
        again = service.update_node("a", {"name": "A2"})
    if held:
        assert result["error"] == again["error"] == "staged_unsupported"
        assert storage.get_node("a").name == "Alpha"
        assert len(asked) == 1  # sticky once seen
    else:
        assert storage.get_node("a").name == "A2"
        assert len(asked) == 1  # trusted for the re-check interval
    # Without a session the instance writes as before, and asks nothing.
    asked.clear()
    assert service.update_node("b", {"name": "B1"})["success"] is True
    assert asked == []


def test_the_store_is_asked_again_after_the_interval_and_a_failed_check_keeps_the_answer(
    monkeypatch,
):
    import backend.core.write_modes as wm

    answers = iter([False, RuntimeError("store down"), True])
    asked = []

    def records():
        asked.append(1)
        answer = next(answers)
        if isinstance(answer, Exception):
            raise answer
        return answer

    clock = [1000.0]
    monkeypatch.setattr(wm.time, "monotonic", lambda: clock[0])
    write_modes = WriteModes(None, unreadable_records=records)
    assert write_modes.store_holds_unreadable_records() is False
    clock[0] += wm.UNREADABLE_RECORDS_RECHECK_SECONDS - 1
    assert write_modes.store_holds_unreadable_records() is False
    assert len(asked) == 1
    clock[0] += 2
    assert write_modes.store_holds_unreadable_records() is False  # failed check
    clock[0] += wm.UNREADABLE_RECORDS_RECHECK_SECONDS
    assert write_modes.store_holds_unreadable_records() is True
    clock[0] += wm.UNREADABLE_RECORDS_RECHECK_SECONDS
    assert write_modes.store_holds_unreadable_records() is True
    assert len(asked) == 3


def test_a_staged_session_whose_record_was_lost_is_never_written_directly(world):
    session = world.manager.create_session("restored")
    session.write_mode = "staged"  # the document kept, the record gone
    with bind_session(session.id):
        result = world.service.update_node("a", {"name": "A1"})
    assert result["error"] == "session_not_found"
    assert world.storage.get_node("a").name == "Alpha"


def test_an_edge_recreated_elsewhere_is_not_incident_to_its_old_endpoint(world):
    sid = world.staged_session()
    with bind_session(sid):
        world.service.add_nodes([{"id": "c", "type": "Actor", "name": "C"}], [])
        world.service.delete_edges(["ab"], confirmed=True)
        world.service.add_nodes(
            [], [{"id": "ab", "source": "b", "target": "c", "type": "RELATES_TO"}]
        )
        result = world.service.delete_nodes(["a"], confirmed=True)
    assert result["affected_edge_ids"] == []
    assert world.layer(sid)[("edge", "ab")]["action"] == "update"


def test_a_stale_expected_session_seq_is_refused_even_when_nothing_changes(world):
    sid = world.staged_session()
    with bind_session(sid):
        world.service.update_node("a", {"name": "A1"})
        refused = world.service.delete_nodes(
            ["absent"], confirmed=True, expected_session_seq=0
        )
    assert refused["error"] == "session_seq_conflict"


def test_postgres_refuses_to_boot_with_staging_off_over_staged_sessions():
    if not _server_reachable():
        pytest.skip("no PostgreSQL server configured")
    from backend.core.postgres_backend import PostgresGraphPersistenceBackend

    schema = f"co_staged_{uuid.uuid4().hex[:16]}"
    try:
        on = PostgresGraphPersistenceBackend(DSN, schema=schema, staged_sessions=True)
        off = PostgresGraphPersistenceBackend(DSN, schema=schema)
        assert off.holds_staged_sessions() is False
        on.record_staged("1234-5678-1234-5678")
        assert off.holds_staged_sessions() is True
        on.close()
        off.close()
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


# -- entry contents, expectations and refusals on every write kind ---------------


def test_archive_stages_the_flag_and_names_resolve_through_the_layer(world):
    sid = world.staged_session()
    with bind_session(sid):
        world.service.archive_nodes(["b"])
        world.service.update_node("a", {"name": "Alpha2"})
        stale = world.service.add_edge("Alpha", "b", type="RELATES_TO")
        fresh = world.service.add_edge("Alpha2", "b", type="RELATES_TO")
    assert world.layer(sid)[("node", "b")]["staged"] == {"archived": True}
    assert stale["success"] is False
    assert fresh["success"] is True


def test_update_over_create_keeps_every_field_and_merges_metadata(world):
    sid = world.staged_session()
    with bind_session(sid):
        world.service.add_nodes(
            [{"id": "c", "type": "Actor", "name": "C", "description": "d"}], []
        )
        world.service.update_node("c", {"name": "C2", "description": "d2"})
        world.service.update_node("a", {"metadata": {"p": 1}}, metadata_merge=True)
        world.service.update_node("a", {"metadata": {"q": 2}}, metadata_merge=True)
    assert world.layer(sid)[("node", "c")]["staged"]["description"] == "d2"
    assert world.layer(sid)[("node", "a")]["staged"]["metadata"] == {"p": 1, "q": 2}


def test_expectations_on_delete_and_edge_writes(world):
    sid = world.staged_session()
    rev = world.storage.get_node("a").revision
    erev = world.storage.edges["ab"].revision
    with bind_session(sid):
        r1 = world.service.delete_nodes(
            ["a"], confirmed=True, expected_revisions={"a": rev + 7}
        )
        r2 = world.service.update_edge("ab", {"label": "x"}, expected_revision=erev + 7)
        r3 = world.service.delete_edge("ab", expected_revision=erev + 7)
        r4 = world.service.add_nodes(
            [{"id": "a", "type": "Actor", "name": "Z"}], [], expect_absent=True
        )
        r5 = world.service.add_nodes([{"id": "a", "type": "Actor", "name": "Z"}], [])
        r6 = world.service.add_nodes(
            [],
            [{"id": "ab", "source": "a", "target": "b", "type": "RELATES_TO"}],
            expect_absent=True,
        )
    assert r1["error"] == "entity_revision_conflict"
    assert r2["error"] == "entity_revision_conflict"
    assert r3["error"] == "entity_revision_conflict"
    assert r4["error"] == "entity_revision_conflict"
    assert r5["success"] is False and "already exists" in r5["message"]
    assert r6["error"] == "entity_revision_conflict"
    assert world.layer(sid) == {}


def test_inapplicable_edges_are_refused(world, monkeypatch):
    monkeypatch.setattr(
        "backend.service.staging.edge_applicability_error", lambda *a: "inapplicable"
    )
    sid = world.staged_session()
    with bind_session(sid):
        r1 = world.service.add_nodes(
            [{"id": "c", "type": "Actor", "name": "C"}],
            [{"id": "ca", "source": "c", "target": "a", "type": "RELATES_TO"}],
        )
        r2 = world.service.add_edge("b", "a", type="RELATES_TO")
    assert r1["success"] is False and r2["success"] is False
    with bind_session(sid):
        r3 = world.service.update_edge("ab", {"label": "y"})
    assert r3["success"] is False
    assert world.layer(sid) == {}


def test_noop_update_stages_nothing_and_takes_no_seq(world):
    sid = world.staged_session()
    seq = world.manager.get_session(sid).seq
    world.published.clear()
    with bind_session(sid):
        result = world.service.update_node("a", {"name": "Alpha"})
    assert result["session_seq"] == seq
    assert result["entries"] == []
    assert world.manager.get_session(sid).seq == seq
    assert world.published == []


def test_stale_expected_updated_at_conflicts(world):
    sid = world.staged_session()
    with bind_session(sid):
        result = world.service.update_node(
            "a", {"name": "N"}, expected_updated_at="2001-01-01T00:00:00+00:00"
        )
    assert result.get("conflict") is True
    assert world.layer(sid) == {}


def test_actor_is_recorded(world):
    sid = world.staged_session()
    with bind_session(sid):
        world.service.update_node("a", {"name": "N"})
    assert world.layer(sid)[("node", "a")]["staged_by"] == "anonymous"


def test_commit_into_a_retired_layer_is_session_not_found(world, monkeypatch):
    sid = world.staged_session()

    def boom(*a, **k):
        raise layer_store.StagedSessionNotActive(sid, "retired")

    monkeypatch.setattr(world.storage.layer_store, "stage", boom)
    with bind_session(sid):
        result = world.service.update_node("a", {"name": "N"})
    assert result["error"] == "session_not_found"


def test_direct_named_session_seq_conflict(world):
    sid = world.manager.create_session("d", "direct").id
    with bind_session(sid):
        result = world.service.update_node("a", {"name": "N"}, expected_session_seq=99)
    assert result["error"] == "session_seq_conflict"
    assert world.storage.get_node("a").name == "Alpha"


def test_delete_nodes_limits_when_staged(world):
    sid = world.staged_session()
    with bind_session(sid):
        many = world.service.delete_nodes([f"n{i}" for i in range(11)], confirmed=True)
        unconfirmed = world.service.delete_nodes(["a"])
    assert many["success"] is False and unconfirmed["success"] is False
    assert world.layer(sid) == {}


def test_staging_while_a_batch_holds_the_session_lock_is_busy(world):
    import asyncio

    sid = world.staged_session()
    loop = asyncio.new_event_loop()
    loop.run_until_complete(world.manager._lock(sid).acquire())
    try:
        with bind_session(sid):
            result = world.service.update_node("a", {"name": "N"})
    finally:
        world.manager._lock(sid).release()
        loop.close()
    assert result["error"] == "busy"
    assert world.layer(sid) == {}


# -- narrowing, saved views, adoption and the guards ------------------------


class Narrow:
    """Only nodes whose origin_graph_id is g1 are visible; local ones are not."""

    def evaluate(self, context):
        return GraphAuthorizationDecision(
            allowed=True,
            graph_access=GraphAccessNarrowing(
                enabled=True, allow_local_graph=False, include_graph_ids=("g1",)
            ),
        )


def _narrowed(world):
    world.storage.add_nodes(
        [
            Node(id="v", type="Actor", name="Vis", metadata={"origin_graph_id": "g1"}),
            Node(id="w", type="Actor", name="Vis2", metadata={"origin_graph_id": "g1"}),
        ],
        [Edge(id="vw", source="v", target="w", type="RELATES_TO")],
    )
    sid = world.staged_session()
    world.service._authorization_hook = Narrow()
    return sid


def test_staged_update_of_a_node_outside_the_narrowing_is_not_found(world):
    sid = _narrowed(world)
    with bind_session(sid):
        result = world.service.update_node("a", {"name": "X"})
    assert result["success"] is False
    assert world.layer(sid) == {}


def test_staged_delete_of_a_node_outside_the_narrowing_stages_nothing(world):
    sid = _narrowed(world)
    with bind_session(sid):
        world.service.delete_nodes(["a"], confirmed=True)
    assert world.layer(sid) == {}


def test_a_staged_cascade_answers_nothing_of_an_edge_outside_the_narrowing(world):
    sid = _narrowed(world)
    world.storage.add_edge(
        Edge(
            id="va",
            source="v",
            target="a",
            type="RELATES_TO",
            label="SECRET-LABEL",
            metadata={"note": "SECRET-META"},
        )
    )
    with bind_session(sid):
        result = world.service.delete_nodes(["v"], confirmed=True)
    assert result["success"] is True
    assert "SECRET" not in json.dumps(result)
    assert world.layer(sid)[("edge", "va")]["base"]["label"] == "SECRET-LABEL"


def _hidden_edge_tombstoned(world):
    sid = _narrowed(world)
    world.storage.add_edge(
        Edge(
            id="va",
            source="v",
            target="a",
            type="RELATES_TO",
            metadata={"SECRET_KEY_NAME": 1},
        )
    )
    with bind_session(sid):
        world.service.delete_nodes(["v"], confirmed=True)
    return sid


def test_re_creating_a_tombstoned_edge_outside_the_narrowing_is_refused(world):
    sid = _hidden_edge_tombstoned(world)
    before = world.layer(sid)
    with bind_session(sid):
        batch = world.service.add_nodes(
            [],
            [{"id": "va", "source": "w", "target": "w", "type": "RELATES_TO"}],
        )
        single = world.service.add_edge("w", "w", type="RELATES_TO")
    assert batch["success"] is False
    assert "SECRET" not in json.dumps(batch)
    assert world.layer(sid).keys() == before.keys() | (
        {("edge", single["edge"]["id"])} if single.get("success") else set()
    )


def test_re_creating_a_tombstoned_node_outside_the_narrowing_is_refused(world):
    sid = world.staged_session()
    with bind_session(sid):
        world.service.delete_nodes(["a"], confirmed=True)
    world.service._authorization_hook = Narrow()
    with bind_session(sid):
        result = world.service.add_nodes(
            [
                {
                    "id": "a",
                    "type": "Actor",
                    "name": "A",
                    "metadata": {"origin_graph_id": "g1"},
                }
            ],
            [],
        )
    assert result["success"] is False
    assert "keep" not in json.dumps(result)
    assert world.layer(sid)[("node", "a")]["action"] == "delete"


def test_re_creating_a_hidden_edge_after_main_deleted_its_hidden_endpoint_is_refused(
    world,
):
    sid = _hidden_edge_tombstoned(world)
    world.storage.delete_nodes(["a"], confirmed=True)
    with bind_session(sid):
        result = world.service.add_nodes(
            [], [{"id": "va", "source": "w", "target": "w", "type": "RELATES_TO"}]
        )
    assert result["success"] is False
    assert "SECRET" not in json.dumps(result)


def test_a_re_created_edge_cannot_keep_a_main_edge_that_left_the_view_in_it(world):
    sid = _narrowed(world)
    world.service._authorization_hook = world.hook
    with bind_session(sid):
        world.service.delete_edges(["ab"], confirmed=True)
        world.service.add_nodes(
            [], [{"id": "ab", "source": "v", "target": "w", "type": "RELATES_TO"}]
        )
    world.storage.update_node("a", {"metadata": {"origin_graph_id": "g2"}})
    world.storage.update_edge("ab", {"metadata": {"secret_key": "s3cr3t"}})

    class LocalAndG1:
        def evaluate(self, context):
            return GraphAuthorizationDecision(
                allowed=True,
                graph_access=GraphAccessNarrowing(
                    enabled=True, allow_local_graph=True, include_graph_ids=("g1",)
                ),
            )

    world.service._authorization_hook = LocalAndG1()
    before = world.layer(sid)
    with bind_session(sid):
        answers = [
            world.service.archive_edges(["ab"]),
            world.service.update_edge("ab", {"label": "x"}),
            world.service.update_edge("ab", {"label": "x"}, expected_revision=999),
            world.service.delete_edges(["ab"], confirmed=True),
        ]
    for answer in answers:
        assert "s3cr3t" not in json.dumps(answer)
        assert answer.get("current_revision") is None
    assert world.layer(sid) == before


def test_a_staged_change_cannot_keep_a_main_node_that_left_the_view_in_it(world):
    sid = world.staged_session()
    world.storage.add_nodes(
        [Node(id="v", type="Actor", name="V", metadata={"origin_graph_id": "g1"})], []
    )
    with bind_session(sid):
        world.service.update_node("v", {"metadata": {}})  # stages origin -> null
    world.storage.update_node(
        "v",
        {
            "description": "SECRET-DESC",
            "metadata": {"origin_graph_id": "g2", "secret_key": "s3cr3t"},
        },
    )

    class LocalAndG1:
        def evaluate(self, context):
            return GraphAuthorizationDecision(
                allowed=True,
                graph_access=GraphAccessNarrowing(
                    enabled=True, allow_local_graph=True, include_graph_ids=("g1",)
                ),
            )

    world.service._authorization_hook = LocalAndG1()
    before = world.layer(sid)
    with bind_session(sid):
        answers = [
            world.service.archive_nodes(["v"]),
            world.service.update_node("v", {"tags": ["t"]}),
            world.service.add_edge("v", "b", type="RELATES_TO"),
            world.service.delete_nodes(["v"], confirmed=True),
        ]
    for answer in answers:
        assert "SECRET" not in json.dumps(answer) and "s3cr3t" not in json.dumps(answer)
    assert world.layer(sid) == before


def test_re_creating_a_node_that_has_since_left_the_narrowing_is_refused(world):
    sid = _narrowed(world)
    with bind_session(sid):
        world.service.delete_nodes(["v"], confirmed=True)
    world.storage.update_node(
        "v", {"metadata": {"origin_graph_id": "g2", "secret_key": "s3cr3t"}}
    )
    with bind_session(sid):
        result = world.service.add_nodes(
            [
                {
                    "id": "v",
                    "type": "Actor",
                    "name": "V",
                    "metadata": {"origin_graph_id": "g1"},
                }
            ],
            [],
        )
    assert result["success"] is False
    assert "s3cr3t" not in json.dumps(result)
    assert world.layer(sid)[("node", "v")]["action"] == "delete"


def test_staged_archive_of_a_node_outside_the_narrowing_stages_nothing(world):
    sid = _narrowed(world)
    with bind_session(sid):
        world.service.archive_nodes(["a"])
    assert world.layer(sid) == {}


def test_staged_edge_to_a_node_outside_the_narrowing_is_refused(world):
    sid = _narrowed(world)
    with bind_session(sid):
        result = world.service.add_edge("v", "a", type="RELATES_TO")
    assert result["success"] is False
    assert world.layer(sid) == {}


def test_staged_edge_update_or_delete_of_an_invisible_edge_stages_nothing(world):
    sid = _narrowed(world)
    with bind_session(sid):
        world.service.update_edge("ab", {"label": "x"})
        world.service.delete_edges(["ab"], confirmed=True)
        world.service.archive_edges(["ab"])
    assert world.layer(sid) == {}


def test_staged_add_nodes_with_an_edge_to_an_invisible_node_is_refused(world):
    sid = _narrowed(world)
    with bind_session(sid):
        result = world.service.add_nodes(
            [
                {
                    "id": "n",
                    "type": "Actor",
                    "name": "N",
                    "metadata": {"origin_graph_id": "g1"},
                }
            ],
            [{"source": "n", "target": "a", "type": "RELATES_TO"}],
        )
    assert result["success"] is False
    assert world.layer(sid) == {}


def test_saved_view_annotation_validation_applies_to_a_staged_update(world):
    world.storage.add_nodes(
        [Node(id="sv", type="SavedView", name="View", metadata={})], []
    )
    sid = world.staged_session()
    with bind_session(sid):
        result = world.service.update_node(
            "sv",
            {
                "metadata": {
                    "annotations": [
                        {
                            "id": "i1",
                            "type": "image",
                            "image": {"url": "http://example.invalid/x.png"},
                        }
                    ]
                }
            },
        )
    assert result["success"] is False
    assert world.layer(sid) == {}


def test_adopting_twice_in_a_staged_session_finds_the_layer_copy(world, tmp_path):
    from backend.service.tests.test_federated_adoption import (
        _service_with_cached_federated_node,
    )

    (tmp_path / "fed").mkdir()
    fed = _service_with_cached_federated_node(tmp_path / "fed")
    world.service._federation_manager = fed._federation_manager
    sid = world.staged_session()
    with bind_session(sid):
        first = world.service.adopt_federated_node("federated::esam-main::remote-1")
        second = world.service.adopt_federated_node("federated::esam-main::remote-1")
    assert first["success"] is True
    assert "already adopted" in second["message"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"default_mode": "bogus"},
        {"default_mode": "staged"},
        {"max_entries": True},
        {"max_entries": 0},
        {"max_entries": "5"},
    ],
)
def test_write_modes_constructor_guards(kwargs):
    with pytest.raises(ValueError):
        WriteModes(None, **kwargs)


@pytest.mark.parametrize(
    "op",
    [
        {"op": "write_mode_changed", "write_mode": "direct"},
        {"op": "graph_change_staged", "entries": [], "removals": "x"},
        {"op": "graph_change_staged", "entries": "x", "removals": []},
        {"op": "graph_change_staged", "entries": []},
    ],
)
def test_session_store_validates_server_only_ops(op):
    from backend.core.session_store import (
        InMemorySessionPersistenceBackend,
        OpError,
        SessionStore,
    )

    store = SessionStore(InMemorySessionPersistenceBackend())
    session = store.create("s", write_mode="staged")
    with pytest.raises(OpError):
        store.apply_state_op(session, op)


# -- net metadata changes, composed endpoints, record ordering, PG locking ---


def test_update_over_a_staged_create_removes_a_metadata_key_set_to_none(world):
    """G7: one entry per entity holding the NET change; a merge-patch None on a
    key of a staged create removes it from the staged doc."""
    sid = world.staged_session()
    with bind_session(sid):
        world.service.add_nodes(
            [{"id": "c", "type": "Actor", "name": "C", "metadata": {"k": 1, "j": 2}}],
            [],
        )
        world.service.update_node("c", {"metadata": {"k": None}}, metadata_merge=True)
    entry = world.layer(sid)[("node", "c")]
    assert entry["action"] == "create"
    assert entry["staged"]["metadata"] == {"j": 2}


def test_an_edge_whose_endpoint_vanished_from_main_is_not_in_the_composed_view(world):
    """G7 (§6.3): a composed edge needs both endpoints in the composed view."""
    sid = world.staged_session()
    with bind_session(sid):
        world.service.update_edge("ab", {"label": "l"})
    # b disappears from the main graph behind the session's back (cascading ab).
    world.storage.delete_nodes(["b"], confirmed=True)
    with bind_session(sid):
        result = world.service.delete_edges(["ab"], confirmed=True)
    assert result["deleted_edge_ids"] == []


def test_choose_staged_without_a_layer_store_is_staged_unsupported():
    """G2/G10: a deployment with no layer store refuses `staged` at the choice
    itself, not only when a record is later written."""
    with pytest.raises(WriteModeRefused) as exc:
        WriteModes().choose("staged")
    assert exc.value.code == "staged_unsupported"


def test_a_failing_record_leaves_no_session_document_behind():
    """G4: the staged-session record is written before the document; when it
    fails nothing is saved - not in memory, not in the backend."""
    backend = InMemorySessionPersistenceBackend()
    store = SessionStore(backend)

    def boom(_sid):
        raise RuntimeError("record failed")

    with pytest.raises(RuntimeError):
        store.create("w", write_mode="staged", on_allocate=boom)
    assert store.session_count() == 0
    assert store.list_meta() == []


def test_a_record_is_written_before_its_document_is_visible():
    """G4: at the moment the record is written, no document names the session."""
    store = SessionStore(InMemorySessionPersistenceBackend())
    seen = []
    store.create(
        "w", write_mode="staged", on_allocate=lambda sid: seen.append(store.get(sid))
    )
    assert seen == [None]


def test_postgres_retire_requiring_empty_takes_the_record_row_lock(world, monkeypatch):
    """G5: the emptiness check is made under the record row's lock, so a stage on
    another instance either committed (and is counted) or waits."""
    backend = world.storage.layer_store
    if type(backend).__name__ != "PostgresGraphPersistenceBackend":
        pytest.skip("postgres only")
    sid = world.staged_session()
    calls = []
    original = backend._session_status

    def spy(conn, session_id, *, lock):
        calls.append(lock)
        return original(conn, session_id, lock=lock)

    monkeypatch.setattr(backend, "_session_status", spy)
    backend.retire(sid, require_empty=True)
    assert calls and calls[0] is True
    assert backend.staged_status(sid) == layer_store.STATUS_RETIRED


def test_a_staged_record_exists_before_the_session_document_is_saved(world):
    world.write_modes._default = "staged"
    seen = []
    backend = world.store._backend
    original = backend.save

    def save(doc):
        seen.append(world.storage.layer_store.staged_status(doc["id"]))
        return original(doc)

    backend.save = save
    world.manager.create_session("x")
    assert seen == ["active"]


def test_expected_revision_on_an_absent_entity_conflicts_like_a_direct_write(world):
    sid = world.staged_session()
    direct = world.service.update_node("ghost", {"name": "N"}, expected_revision=3)
    with bind_session(sid):
        staged = world.service.update_node("ghost", {"name": "N"}, expected_revision=3)
        staged_edge = world.service.update_edge(
            "ghost-edge", {"label": "x"}, expected_revision=3
        )
    assert direct["error"] == "entity_revision_conflict"
    assert staged["error"] == "entity_revision_conflict"
    assert staged["current_revision"] is None
    assert staged_edge["error"] == "entity_revision_conflict"
    assert world.layer(sid) == {}


def test_an_unnarrowed_re_create_over_a_tombstone_whose_main_endpoint_is_gone(world):
    sid = world.staged_session()
    with bind_session(sid):
        assert world.service.delete_edges(["ab"], confirmed=True)["success"] is True
    world.storage.delete_nodes(["b"], confirmed=True)
    with bind_session(sid):
        result = world.service.add_nodes(
            [], [{"id": "ab", "source": "a", "target": "a", "type": "RELATES_TO"}]
        )
    assert result["success"] is True, result


def test_a_single_edge_add_refuses_an_id_tombstoned_outside_the_narrowing(world):
    from backend.service.staging import StagedWrite

    # The service mints edge ids, so only the unit can be handed a colliding one.
    sid = _hidden_edge_tombstoned(world)
    staged = StagedWrite(
        world.storage,
        world.manager,
        sid,
        expected_session_seq=None,
        max_entries=1000,
        actor="t",
    )
    before = world.layer(sid)
    result = staged.add_edge(
        Edge(id="va", source="w", target="w", type="RELATES_TO"),
        graph_access=GraphAccessNarrowing(
            enabled=True, allow_local_graph=False, include_graph_ids=("g1",)
        ),
    )
    assert result["success"] is False
    assert world.layer(sid).keys() == before.keys()


def test_a_staging_instance_never_asks_the_unreadable_records_check(tmp_path):
    backend = FileGraphPersistenceBackend(tmp_path / "g.json", staged_sessions=True)
    storage = GraphStorage(str(tmp_path / "g.json"), persistence_backend=backend)
    asked = []
    modes = WriteModes(
        storage.layer_store, unreadable_records=lambda: asked.append(1) or True
    )
    assert modes.store_holds_unreadable_records() is False
    assert asked == []


def test_staged_records_check_is_the_backends_own_check(tmp_path):
    backend = FileGraphPersistenceBackend(tmp_path / "g.json")
    storage = GraphStorage(str(tmp_path / "g.json"), persistence_backend=backend)
    assert storage.staged_records_check is None
    backend.holds_staged_sessions = lambda: True
    assert storage.staged_records_check() is True


def test_staged_status_of_a_malformed_id_is_none_not_an_error(tmp_path):
    backend = FileGraphPersistenceBackend(tmp_path / "g.json", staged_sessions=True)
    storage = GraphStorage(str(tmp_path / "g.json"), persistence_backend=backend)
    modes = WriteModes(storage.layer_store)
    assert modes.staged_status("not a session id/../x") == "none"
    assert modes.is_retired("") is False


def test_a_retired_id_stays_retired_once_seen(tmp_path):
    backend = FileGraphPersistenceBackend(tmp_path / "g.json", staged_sessions=True)
    storage = GraphStorage(str(tmp_path / "g.json"), persistence_backend=backend)
    sid = "1111-2222-3333-4444"
    WriteModes(storage.layer_store).record_staged(sid)
    WriteModes(storage.layer_store).retire(sid, require_empty=False)
    modes = WriteModes(storage.layer_store)
    assert modes.staged_status(sid) == "retired"
    storage.layer_store.staged_status = lambda _sid: "none"
    assert modes.staged_status(sid) == "retired"


def test_a_node_main_since_deleted_is_judged_by_its_base(world):
    sid = world.staged_session()
    with bind_session(sid):
        world.service.update_node("a", {"metadata": {"origin_graph_id": "g1"}})
    world.service._authorization_hook = Narrow()
    world.storage.delete_nodes(["a"], confirmed=True)
    before = world.layer(sid)
    with bind_session(sid):
        answers = [
            world.service.update_node("a", {"tags": ["t"]}),
            world.service.archive_nodes(["a"]),
            world.service.add_edge("b", "a", type="RELATES_TO"),
        ]
    for answer in answers:
        assert "Alpha" not in json.dumps(answer) and "keep" not in json.dumps(answer)
    assert world.layer(sid) == before


def test_a_staged_adopt_under_a_federated_only_view_stages_like_a_direct_one(
    world, tmp_path
):
    from backend.service.tests.test_federated_adoption import (
        _service_with_cached_federated_node,
    )

    class FederatedOnly:
        def evaluate(self, context):
            return GraphAuthorizationDecision(
                allowed=True,
                graph_access=GraphAccessNarrowing(
                    enabled=True,
                    allow_local_graph=False,
                    include_graph_ids=("esam-main",),
                ),
            )

    (tmp_path / "fed").mkdir()
    fed = _service_with_cached_federated_node(tmp_path / "fed")
    world.service._federation_manager = fed._federation_manager
    world.service._authorization_hook = FederatedOnly()
    sid = world.staged_session()
    with bind_session(sid):
        staged = world.service.adopt_federated_node("federated::esam-main::remote-1")
    assert staged["success"] is True, staged
    assert staged["staged"] is True


def test_an_edge_main_since_deleted_is_judged_by_its_base_endpoints(world):
    world.storage.add_nodes(
        [Node(id="h", type="Actor", name="H", metadata={"origin_graph_id": "g2"})],
        [
            Edge(
                id="hb",
                source="h",
                target="b",
                type="RELATES_TO",
                label="SECRET-LABEL",
                metadata={"secret_key": "s3cr3t"},
            )
        ],
    )
    sid = world.staged_session()
    with bind_session(sid):
        world.service.archive_edges(["hb"])
    world.storage.delete_nodes(["h"], confirmed=True)

    class LocalAndG1:
        def evaluate(self, context):
            return GraphAuthorizationDecision(
                allowed=True,
                graph_access=GraphAccessNarrowing(
                    enabled=True, allow_local_graph=True, include_graph_ids=("g1",)
                ),
            )

    world.service._authorization_hook = LocalAndG1()
    with bind_session(sid):
        world.service.add_nodes(
            [
                {
                    "id": "h",
                    "type": "Actor",
                    "name": "mine",
                    "metadata": {"origin_graph_id": "g1"},
                }
            ],
            [],
        )
        answers = [
            world.service.unarchive_edges(["hb"]),
            world.service.update_edge("hb", {"label": "x"}),
        ]
    for answer in answers:
        assert "SECRET" not in json.dumps(answer) and "s3cr3t" not in json.dumps(answer)


def _tombstone_then_recreate(world, sid, edge_id, source, target):
    with bind_session(sid):
        assert world.service.delete_edges([edge_id], confirmed=True)["success"]
        r = world.service.add_nodes(
            [],
            [{"id": edge_id, "source": source, "target": target, "type": "RELATES_TO"}],
        )
        assert r["success"] is True, r


def test_a_node_a_staged_update_moved_out_of_view_is_no_longer_shown(world):
    sid = _narrowed(world)
    with bind_session(sid):
        moved = world.service.update_node("v", {"metadata": {"origin_graph_id": "g2"}})
    assert moved["success"] is True, moved
    before = world.layer(sid)
    with bind_session(sid):
        again = world.service.update_node("v", {"name": "X"})
    assert again["success"] is False
    assert world.layer(sid) == before


# endpoints; an endpoint main moved out of view hides it.
@pytest.mark.parametrize("moved", ["v", "w"])
def test_a_staged_edge_with_an_endpoint_main_moved_out_of_view_is_hidden(world, moved):
    sid = _narrowed(world)
    with bind_session(sid):
        r = world.service.add_nodes(
            [], [{"id": "new", "source": "v", "target": "w", "type": "RELATES_TO"}]
        )
        assert r["success"] is True, r
    world.storage.update_node(moved, {"metadata": {"origin_graph_id": "g2"}})
    before = world.layer(sid)
    with bind_session(sid):
        answers = [
            world.service.update_edge("new", {"label": "x"}),
            world.service.delete_edges(["new"], confirmed=True),
            world.service.archive_edges(["new"]),
        ]
    assert world.layer(sid) == before
    assert answers[0].get("success") is not True, answers[0]


def test_a_staged_created_edge_in_view_can_be_updated(world):
    sid = _narrowed(world)
    with bind_session(sid):
        assert world.service.add_nodes(
            [], [{"id": "new", "source": "v", "target": "w", "type": "RELATES_TO"}]
        )["success"]
        result = world.service.update_edge("new", {"label": "x"})
    assert result["success"] is True, result


# endpoints, either of them, even when the entry's base was in view.
@pytest.mark.parametrize("hidden_end", ["source", "target"])
def test_a_re_created_edge_over_a_main_edge_now_ending_outside_the_view_is_hidden(
    world, hidden_end
):
    sid = _narrowed(world)
    _tombstone_then_recreate(world, sid, "vw", "v", "w")  # base: v-w, all in view
    world.storage.delete_edges(["vw"])
    world.storage.add_edge(
        Edge(
            id="vw",
            source="a" if hidden_end == "source" else "v",
            target="a" if hidden_end == "target" else "w",
            type="RELATES_TO",
            metadata={"secret_key": "s3cr3t"},
        )
    )
    before = world.layer(sid)
    with bind_session(sid):
        answers = [
            world.service.update_edge("vw", {"label": "x"}),
            world.service.delete_edges(["vw"], confirmed=True),
            world.service.archive_edges(["vw"]),
        ]
    assert world.layer(sid) == before
    for a in answers:
        assert "s3cr3t" not in json.dumps(a)
    assert answers[0].get("success") is not True, answers[0]


def test_an_unknown_edge_under_narrowing_is_not_found(world):
    sid = _narrowed(world)
    with bind_session(sid):
        r = world.service.update_edge("nope", {"label": "x"})
        d = world.service.delete_edges(["nope"], confirmed=True)
    assert r["success"] is False
    assert world.layer(sid) == {}
    assert d is not None


def test_an_unnarrowed_edge_over_a_base_whose_endpoint_left_main_stays_editable(world):
    sid = world.staged_session()
    with bind_session(sid):
        assert world.service.delete_edges(["ab"], confirmed=True)["success"]
    world.storage.delete_nodes(["b"], confirmed=True)
    with bind_session(sid):
        r = world.service.add_nodes(
            [], [{"id": "ab", "source": "a", "target": "a", "type": "RELATES_TO"}]
        )
        assert r["success"] is True, r
        result = world.service.update_edge("ab", {"label": "x"})
    assert result["success"] is True, result
