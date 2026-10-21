"""Staged-write narrowing, session resolution and the unreadable-records refusal,
through the graph service and the app."""

from __future__ import annotations

import asyncio
import dataclasses
import threading
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from backend.api_host.server import create_app
from backend.api_host.session_binding import SessionBindingMiddleware
from backend.core import Edge, Node
from backend.core import write_modes as wm
from backend.core.write_modes import WriteModes
from backend.runtime import session_context
from backend.runtime.authorization import (
    GraphAccessNarrowing,
    GraphAuthorizationDecision,
)
from backend.runtime.session_context import bind_session
from backend.service.staging import SessionGate
from backend.service.tests import test_staged_writes as staged_writes

# The staged-writes module's world (file and PostgreSQL) and narrowing helpers.
world = staged_writes.world
Narrow = staged_writes.Narrow
_narrowed = staged_writes._narrowed


class LocalAndG1:
    def evaluate(self, context):
        return GraphAuthorizationDecision(
            allowed=True,
            graph_access=GraphAccessNarrowing(
                enabled=True, allow_local_graph=True, include_graph_ids=("g1",)
            ),
        )


def _g1(node_id):
    return Node(
        id=node_id, type="Actor", name=node_id, metadata={"origin_graph_id": "g1"}
    )


# -- _shown ---------------------------------------------------------------------


def test_a_node_created_in_the_session_stays_in_the_writers_view(world):
    sid = world.staged_session()
    world.storage.add_nodes([_g1("v")], [])
    with bind_session(sid):
        created = world.service.add_nodes(
            [
                {
                    "id": "n",
                    "type": "Actor",
                    "name": "N",
                    "metadata": {"origin_graph_id": "g1"},
                }
            ],
            [],
        )
    assert created["success"] is True
    world.service._authorization_hook = Narrow()
    with bind_session(sid):
        updated = world.service.update_node("n", {"name": "N2"})
        edged = world.service.add_edge("n", "v", type="RELATES_TO")
    assert updated["success"] is True, updated
    assert edged["success"] is True, edged


def test_a_staged_change_that_took_a_node_out_of_the_view_is_not_found(world):
    sid = world.staged_session()
    world.storage.add_nodes([_g1("v")], [])
    with bind_session(sid):
        world.service.update_node("v", {"metadata": {}})  # stages origin -> null
    world.service._authorization_hook = Narrow()  # g1 only: the composed v is local
    before = world.layer(sid)
    with bind_session(sid):
        result = world.service.update_node("v", {"tags": ["t"]})
    assert result["success"] is False
    assert world.layer(sid) == before


def _node_left_main_view(world):
    """Staged: v's origin cleared (composed v local). Main: v moved to g2."""
    sid = world.staged_session()
    world.storage.add_nodes([_g1("v")], [])
    with bind_session(sid):
        world.service.update_node("v", {"metadata": {}})
    return sid


def test_add_nodes_cannot_attach_an_edge_to_a_main_node_that_left_the_view(world):
    sid = _node_left_main_view(world)
    world.storage.update_node("v", {"metadata": {"origin_graph_id": "g2"}})
    world.service._authorization_hook = LocalAndG1()
    before = world.layer(sid)
    with bind_session(sid):
        result = world.service.add_nodes(
            [], [{"id": "e1", "source": "v", "target": "b", "type": "RELATES_TO"}]
        )
    assert result["success"] is False
    assert world.layer(sid) == before


# -- _visible_edge --------------------------------------------------------------


def test_a_staged_edge_over_a_main_node_that_left_the_view_is_not_found(world):
    sid = _node_left_main_view(world)
    with bind_session(sid):
        added = world.service.add_edge("v", "b", type="RELATES_TO")
    edge_id = added["edge"]["id"]
    world.storage.update_node("v", {"metadata": {"origin_graph_id": "g2"}})
    world.service._authorization_hook = LocalAndG1()
    before = world.layer(sid)
    with bind_session(sid):
        result = world.service.update_edge(edge_id, {"label": "x"})
    assert result["success"] is False
    assert world.layer(sid) == before


def test_a_staged_edge_with_one_endpoint_out_of_the_view_is_not_found(world):
    sid = _narrowed(world)  # v, w (g1) and vw in main; Narrow installed
    world.service._authorization_hook = world.hook
    with bind_session(sid):
        added = world.service.add_edge("v", "w", type="RELATES_TO")
    edge_id = added["edge"]["id"]
    world.storage.update_node("w", {"metadata": {"origin_graph_id": "g2"}})
    world.service._authorization_hook = Narrow()
    before = world.layer(sid)
    with bind_session(sid):
        result = world.service.update_edge(edge_id, {"label": "x"})
    assert result["success"] is False
    assert world.layer(sid) == before


def _recreated_over_dangling_main_edge(world):
    sid = world.staged_session()
    with bind_session(sid):
        world.service.delete_edges(["ab"], confirmed=True)
        world.service.add_nodes(
            [], [{"id": "ab", "source": "b", "target": "b", "type": "RELATES_TO"}]
        )
    # Main keeps edge ab (a -> b) although a is gone: the only way a main
    # edge has an endpoint main no longer holds.
    del world.storage.nodes["a"]
    return sid


def test_an_unnarrowed_writer_is_not_judged_on_a_main_endpoint_that_is_gone(world):
    sid = _recreated_over_dangling_main_edge(world)
    with bind_session(sid):
        result = world.service.update_edge("ab", {"label": "x"})
    assert result["success"] is True, result


def test_a_narrowed_writer_takes_a_main_endpoint_that_is_gone_as_hidden(world):
    sid = _recreated_over_dangling_main_edge(world)
    world.service._authorization_hook = LocalAndG1()
    before = world.layer(sid)
    with bind_session(sid):
        result = world.service.update_edge("ab", {"label": "x"})
    assert result["success"] is False
    assert world.layer(sid) == before


# -- _hides_tombstone -----------------------------------------------------------


def test_re_creating_a_node_whose_base_was_outside_the_view_is_refused(world):
    sid = world.staged_session()
    with bind_session(sid):
        world.service.delete_nodes(["a"], confirmed=True)  # a: local, not in g1
    world.storage.update_node("a", {"metadata": {"origin_graph_id": "g1"}})
    world.service._authorization_hook = Narrow()
    before = world.layer(sid)
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
    assert world.layer(sid) == before


def test_re_creating_an_edge_main_has_since_moved_to_a_hidden_node_is_refused(world):
    sid = _narrowed(world)
    world.service._authorization_hook = world.hook
    with bind_session(sid):
        world.service.delete_edges(["vw"], confirmed=True)
    world.storage.delete_edges(["vw"])
    world.storage.add_edge(Edge(id="vw", source="v", target="a", type="RELATES_TO"))
    world.service._authorization_hook = Narrow()  # a is local: hidden
    before = world.layer(sid)
    with bind_session(sid):
        result = world.service.add_nodes(
            [], [{"id": "vw", "source": "v", "target": "v", "type": "RELATES_TO"}]
        )
    assert result["success"] is False
    assert world.layer(sid) == before


def test_re_creating_a_cascaded_edge_whose_endpoints_are_in_view_is_allowed(world):
    sid = _narrowed(world)
    with bind_session(sid):
        world.service.delete_nodes(["v"], confirmed=True)  # cascades vw
        result = world.service.add_nodes(
            [], [{"id": "vw", "source": "w", "target": "w", "type": "RELATES_TO"}]
        )
    assert result["success"] is True, result


# -- SessionGate._mode ----------------------------------------------------------


def test_a_retired_id_is_not_found_even_when_something_still_listens_on_it(world):
    sid = world.staged_session()
    assert world.manager.delete_session_sync(sid, discard_staged=True)
    assert world.write_modes.staged_status(sid) == "retired"
    gate = SessionGate(
        world.storage, world.manager, push_target_known=lambda _sid: True
    )
    with pytest.raises(session_context.SessionRefused) as refused:
        gate._mode(sid)
    assert refused.value.code == session_context.SESSION_NOT_FOUND


# -- write_modes.store_holds_unreadable_records ---------------------------------


def test_the_recheck_interval_is_short_enough_to_notice_new_records():
    assert 0 < wm.UNREADABLE_RECORDS_RECHECK_SECONDS <= 60


def test_an_instance_with_no_store_check_does_not_log_a_failed_check(caplog):
    modes = WriteModes(None)
    with caplog.at_level("WARNING"):
        assert modes.store_holds_unreadable_records() is False
    assert caplog.records == []


# -- session_binding ------------------------------------------------------------


def _drive(method, path, values, unreadable):
    seen = {}
    sent = []

    async def app(scope, receive, send):
        seen["reached"] = True
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = SessionBindingMiddleware(
        app,
        api_prefix="/api",
        is_retired=lambda s: False,
        sessions_unreadable=unreadable,
    )
    hdr = session_context.SESSION_ID_HEADER.lower().encode()
    scope = {
        "type": "http",
        "method": method,
        "path": path,
        "headers": [(hdr, v.encode()) for v in values],
    }

    async def send(m):
        sent.append(m)

    async def receive():
        return {"type": "http.request", "body": b""}

    asyncio.run(mw(scope, receive, send))
    return seen, sent


def test_a_preflight_naming_a_session_is_not_refused_where_records_are_unreadable():
    seen, sent = _drive("OPTIONS", "/api/schema", ["1111-2222-3333-4444"], lambda: True)
    assert seen.get("reached") is True
    assert sent[0]["status"] == 204
    seen, sent = _drive(
        "OPTIONS", "/api/sessions/1111-2222-3333-4444", [], lambda: True
    )
    assert seen.get("reached") is True


def test_the_unreadable_records_check_runs_off_the_event_loop():
    threads = {}

    async def run():
        threads["loop"] = threading.get_ident()

        def unreadable():
            threads["check"] = threading.get_ident()
            return False

        async def app(scope, receive, send):
            await send({"type": "http.response.start", "status": 204, "headers": []})
            await send({"type": "http.response.body", "body": b""})

        mw = SessionBindingMiddleware(
            app,
            api_prefix="/api",
            is_retired=lambda s: False,
            sessions_unreadable=unreadable,
        )
        hdr = session_context.SESSION_ID_HEADER.lower().encode()
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/search",
            "headers": [(hdr, b"1111-2222-3333-4444")],
        }

        async def send(_m):
            pass

        async def receive():
            return {"type": "http.request", "body": b""}

        await mw(scope, receive, send)

    asyncio.run(run())
    assert "check" in threads and threads["check"] != threads["loop"]


# -- mcp_tools._session_aware ---------------------------------------------------


@pytest.fixture
def unreadable_client(app_config, mock_llm_provider, monkeypatch):
    config = dataclasses.replace(app_config)
    with patch("backend.ui.chat_logic.create_provider", return_value=mock_llm_provider):
        client = TestClient(create_app(config))
    asked = []

    def unreadable():
        asked.append(1)
        return True

    monkeypatch.setattr(
        client.app.state.write_modes, "store_holds_unreadable_records", unreadable
    )
    client.asked = asked
    return client


def test_a_tool_call_naming_no_session_is_untouched_where_records_are_unreadable(
    unreadable_client,
):
    tools = unreadable_client.app.state.tools_map
    result = tools["search_graph"](query="*")
    assert result.get("error") != "staged_unsupported"
    assert unreadable_client.asked == []


def test_a_tool_call_bound_to_a_session_is_refused_where_records_are_unreadable(
    unreadable_client,
):
    tools = unreadable_client.app.state.tools_map
    sid = unreadable_client.post("/api/sessions", json={"name": "s"}).json()["id"]
    with bind_session(sid):
        result = tools["search_graph"](query="*")
        written = tools["add_nodes"](
            nodes=[{"id": "u", "type": "Actor", "name": "u"}], edges=[]
        )
    assert result["error"] == "staged_unsupported"
    assert written["error"] == "staged_unsupported"
    assert unreadable_client.app.state.graph_storage.get_node("u") is None
