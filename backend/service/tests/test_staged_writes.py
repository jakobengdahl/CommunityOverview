"""Staged writes (docs/SESSION_OVERLAY_CONTRACT.md §5, §6, §9, §13, §14).

Run over the file backend and, when a server is configured, PostgreSQL: the
graph service decides whether a write stages, and the layer store holds it.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from backend.core import GraphStorage, layer_store
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


def test_a_composing_read_in_a_staged_session_is_refused_not_answered_from_main(
    world,
):
    sid = world.staged_session()
    with bind_session(sid):
        result = world.service.get_node_details("a")
    assert result["success"] is False and result["error"] == "session_not_supported"


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
