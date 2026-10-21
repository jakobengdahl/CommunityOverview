"""Acting in a session over HTTP and MCP (docs/SESSION_OVERLAY_CONTRACT.md
§16.1): the header is bound for every request, never ignored, and a staged
session's writes never reach the graph.
"""

from __future__ import annotations

import dataclasses
import json
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from backend.agents.governance.models import AutonomyLevel, Proposal
from backend.agents.governance.store import SqliteProposalStore
from backend.api_host.persistence import (
    PersistenceConfigurationError,
    build_session_settings,
)
from backend.api_host.server import create_app
from backend.runtime.session_context import (
    SESSION_ID_HEADER,
    SessionRefused,
    bind_session,
)
from backend.service import access, mcp_tools

UNKNOWN = "1111-2222-3333-4444"


@pytest.fixture
def staged_config(app_config):
    return dataclasses.replace(app_config, graph_staged_sessions="true")


@pytest.fixture
def client(staged_config, mock_llm_provider):
    with patch("backend.ui.chat_logic.create_provider", return_value=mock_llm_provider):
        yield TestClient(create_app(staged_config))


def _node(client, node_id):
    return client.app.state.graph_storage.get_node(node_id)


def _staged(client) -> str:
    response = client.post("/api/sessions", json={"name": "w", "write_mode": "staged"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["write_mode"], body["write_mode_source"]) == ("staged", "explicit")
    return body["id"]


def _add(client, headers, node_id="s1"):
    return client.post(
        "/api/nodes",
        headers=headers,
        json={
            "nodes": [{"id": node_id, "type": "Actor", "name": node_id}],
            "edges": [],
        },
    )


def test_a_rest_write_in_a_staged_session_stages_and_the_graph_is_untouched(client):
    sid = _staged(client)
    response = _add(client, {SESSION_ID_HEADER: sid})
    assert response.status_code == 200
    assert response.json()["staged"] is True
    assert _node(client, "s1") is None
    assert client.get("/api/nodes/s1").status_code == 404


def test_without_the_header_the_same_write_lands_as_before(client):
    _staged(client)
    response = _add(client, {})
    assert response.status_code == 200 and "staged" not in response.json()
    assert _node(client, "s1") is not None


@pytest.mark.parametrize(
    "headers, status, code",
    [
        ({SESSION_ID_HEADER: UNKNOWN}, 404, "session_not_found"),
        ({SESSION_ID_HEADER: ""}, 404, "session_not_found"),
    ],
)
def test_a_header_that_does_not_resolve_is_refused_and_nothing_lands(
    client, headers, status, code
):
    response = _add(client, headers)
    assert response.status_code == status
    assert response.json()["detail"]["error"] == code
    assert _node(client, "s1") is None


def test_two_different_session_headers_are_a_mismatch(client):
    sid = _staged(client)
    response = client.post(
        "/api/nodes",
        headers=[(SESSION_ID_HEADER, sid), (SESSION_ID_HEADER, UNKNOWN)],
        json={"nodes": [{"id": "s1", "type": "Actor", "name": "s1"}], "edges": []},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "session_mismatch"
    assert _node(client, "s1") is None


@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/api/schema"),
        ("POST", "/api/import"),
        ("GET", "/api/history"),
        ("GET", "/api/sessions"),
        ("GET", "/health"),
    ],
)
def test_a_route_that_neither_stages_nor_composes_refuses_the_header(
    client, method, path
):
    sid = _staged(client)
    response = client.request(method, path, headers={SESSION_ID_HEADER: sid})
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "session_not_supported"


def test_a_session_route_takes_only_its_own_session_in_the_header(client):
    sid = _staged(client)
    other = client.post("/api/sessions", json={"name": "o"}).json()["id"]
    response = client.get(f"/api/sessions/{other}", headers={SESSION_ID_HEADER: sid})
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "session_mismatch"
    same = client.get(f"/api/sessions/{sid}", headers={SESSION_ID_HEADER: sid})
    assert same.status_code == 200


def test_a_composing_read_in_a_staged_session_answers_from_the_composed_view(
    client,
):
    sid = _staged(client)
    _add(client, {SESSION_ID_HEADER: sid}, node_id="staged-only")
    composed = client.post(
        "/api/search", headers={SESSION_ID_HEADER: sid}, json={"query": "staged-only"}
    )
    plain = client.post("/api/search", json={"query": "staged-only"})
    assert composed.status_code == 200
    assert [(n["id"], n["staged_state"]) for n in composed.json()["nodes"]] == [
        ("staged-only", "created")
    ]
    assert plain.json()["nodes"] == []


def test_a_direct_session_header_changes_nothing(client):
    sid = client.post("/api/sessions", json={"name": "plain"}).json()["id"]
    response = _add(client, {SESSION_ID_HEADER: sid})
    assert response.status_code == 200 and "staged" not in response.json()
    assert _node(client, "s1") is not None
    assert (
        client.get("/api/nodes/s1", headers={SESSION_ID_HEADER: sid}).status_code == 200
    )


def test_staged_to_direct_is_irreversible_over_rest(client):
    sid = _staged(client)
    response = client.patch(f"/api/sessions/{sid}", json={"write_mode": "direct"})
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "write_mode_irreversible"


def test_direct_to_staged_over_rest_then_writes_stage(client):
    sid = client.post("/api/sessions", json={"name": "plain"}).json()["id"]
    response = client.patch(f"/api/sessions/{sid}", json={"write_mode": "staged"})
    assert response.status_code == 200
    assert response.json()["write_mode"] == "staged"
    assert _add(client, {SESSION_ID_HEADER: sid}).json()["staged"] is True
    assert _node(client, "s1") is None


def test_deleting_a_staged_session_with_work_needs_discard_staged_and_retires_it(
    client,
):
    sid = _staged(client)
    _add(client, {SESSION_ID_HEADER: sid})
    refused = client.delete(f"/api/sessions/{sid}")
    assert refused.status_code == 409
    assert refused.json()["detail"]["error"] == "layer_not_empty"
    assert client.delete(f"/api/sessions/{sid}?discard_staged=true").status_code == 200
    # Retired: never recreated, by a write, a rename or a stream reconnect.
    assert _add(client, {SESSION_ID_HEADER: sid}).status_code == 404
    assert client.patch(f"/api/sessions/{sid}", json={"name": "x"}).status_code == 404
    assert client.get(f"/api/sessions/{sid}/stream?client_id=c").status_code == 404
    assert client.app.state.session_manager.get_session(sid) is None


def test_staged_is_refused_where_staged_sessions_are_off(app_config, mock_llm_provider):
    with patch("backend.ui.chat_logic.create_provider", return_value=mock_llm_provider):
        client = TestClient(create_app(app_config))
    response = client.post("/api/sessions", json={"write_mode": "staged"})
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "staged_unsupported"


def test_a_read_only_hook_refuses_creating_a_staged_session(client, monkeypatch):
    monkeypatch.setenv("COMMUNITYOVERVIEW_AUTHORIZATION_MODE", "read-only")
    response = client.post("/api/sessions", json={"write_mode": "staged"})
    assert response.status_code == 403
    plain = client.post("/api/sessions", json={"name": "still allowed"})
    assert plain.status_code == 200


# -- MCP ----------------------------------------------------------------------


def test_an_mcp_write_naming_a_staged_session_stages(client):
    sid = _staged(client)
    tools = client.app.state.tools_map
    result = tools["add_nodes"](
        nodes=[{"id": "m1", "type": "Actor", "name": "m1"}],
        edges=[],
        visualization_session_id=sid,
    )
    assert result["staged"] is True
    assert _node(client, "m1") is None
    unknown = tools["add_nodes"](
        nodes=[{"id": "m2", "type": "Actor", "name": "m2"}],
        edges=[],
        visualization_session_id=UNKNOWN,
    )
    assert unknown["error"] == "session_not_found"
    assert _node(client, "m2") is None


def test_an_mcp_header_and_argument_naming_two_sessions_are_a_mismatch(client):
    from backend.runtime.session_context import bind_session

    sid = _staged(client)
    tools = client.app.state.tools_map
    with bind_session(sid):
        result = tools["update_node"](
            node_id="x", updates={"name": "y"}, visualization_session_id=UNKNOWN
        )
        refused = tools["get_schema"]()
        session_tool = tools["get_visualization_session"](session_id=UNKNOWN)
    assert result["error"] == "session_mismatch"
    assert refused["error"] == "session_not_supported"
    assert session_tool["error"] == "session_mismatch"


def test_the_push_argument_of_a_read_tool_still_takes_an_unstored_session(client):
    result = client.app.state.tools_map["search_graph"](
        query="x", visualization_session_id=UNKNOWN
    )
    assert "error" not in result
    assert result["visualization_delivery"]["delivered"] is False


def test_mcp_switches_a_session_to_staged_and_never_back(client):
    tools = client.app.state.tools_map
    created = tools["create_visualization_session"](name="m")
    sid = created["session"]["session_id"]
    assert created["session"]["write_mode"] == "direct"
    switched = tools["set_session_write_mode"](session_id=sid, write_mode="staged")
    assert switched["session"]["write_mode"] == "staged"
    back = tools["set_session_write_mode"](session_id=sid, write_mode="direct")
    assert back["error"] == "write_mode_irreversible"


# -- configuration ------------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"session_write_mode_default": "staged"},
        {"session_write_mode_default": "sometimes"},
        {"graph_staged_sessions": "maybe"},
        {"graph_staged_sessions": "true", "session_layer_max_entries": "0"},
    ],
)
def test_unusable_staged_session_settings_fail_at_boot(app_config, changes):
    with pytest.raises(PersistenceConfigurationError):
        build_session_settings(dataclasses.replace(app_config, **changes))


def test_a_staged_default_makes_new_sessions_staged(staged_config, mock_llm_provider):
    config = dataclasses.replace(staged_config, session_write_mode_default="staged")
    with patch("backend.ui.chat_logic.create_provider", return_value=mock_llm_provider):
        client = TestClient(create_app(config))
    body = client.post("/api/sessions", json={"name": "n"}).json()
    assert (body["write_mode"], body["write_mode_source"]) == ("staged", "inherited")


def test_staged_sessions_cannot_be_turned_off_once_used(
    staged_config, mock_llm_provider
):
    from backend.api_host.persistence import build_persistence_backend

    with patch("backend.ui.chat_logic.create_provider", return_value=mock_llm_provider):
        client = TestClient(create_app(staged_config))
    _staged(client)
    off = dataclasses.replace(staged_config, graph_staged_sessions="false")
    with pytest.raises(PersistenceConfigurationError, match="GRAPH_STAGED_SESSIONS"):
        build_persistence_backend(off)


def test_an_invalid_staged_patch_is_422_like_a_direct_one(client):
    sid = _staged(client)
    _add(client, {})
    response = client.patch(
        "/api/nodes/s1",
        headers={SESSION_ID_HEADER: sid},
        json={"updates": {"description": "x" * 2001}},
    )
    assert response.status_code == 422
    assert client.app.state.graph_storage.layer_store.load_layer(sid) == []


# -- the real MCP transport -----------------------------------------------------

_MCP_ACCEPT = {"Accept": "application/json, text/event-stream"}


def _rpc(client, mcp_session, method, params, rpc_id, headers=None):
    sent = dict(_MCP_ACCEPT, **(headers or {}))
    if mcp_session:
        sent["mcp-session-id"] = mcp_session
    return client.post(
        "/mcp/",
        json={"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params},
        headers=sent,
    )


def _open_mcp(client, headers=None):
    opened = _rpc(
        client,
        None,
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"},
        },
        1,
        headers,
    )
    mcp_session = opened.headers.get("mcp-session-id")
    client.post(
        "/mcp/",
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers=dict(_MCP_ACCEPT, **{"mcp-session-id": mcp_session}),
    )
    return mcp_session


def _call_add(client, mcp_session, node_id, rpc_id, headers=None):
    return _rpc(
        client,
        mcp_session,
        "tools/call",
        {
            "name": "add_nodes",
            "arguments": {
                "nodes": [{"id": node_id, "type": "Actor", "name": node_id}],
                "edges": [],
            },
        },
        rpc_id,
        headers,
    )


@pytest.fixture
def transport(staged_config, mock_llm_provider):
    with patch("backend.ui.chat_logic.create_provider", return_value=mock_llm_provider):
        with TestClient(create_app(staged_config)) as client:
            yield client


def test_over_the_mcp_transport_a_call_acts_in_its_own_header_session(transport):
    staged = _staged(transport)
    mcp_session = _open_mcp(transport)
    _call_add(transport, mcp_session, "p1", 2, {SESSION_ID_HEADER: staged})
    store = transport.app.state.write_modes.layer_store
    assert _node(transport, "p1") is None
    assert [e["entity_id"] for e in store.load_layer(staged)] == ["p1"]
    # A later call without the header, on a connection opened in no session,
    # is in no session at all.
    _call_add(transport, mcp_session, "p2", 3)
    assert _node(transport, "p2") is not None


def test_an_mcp_connection_opened_in_a_session_stays_in_it(transport):
    staged = _staged(transport)
    other = _staged(transport)
    mcp_session = _open_mcp(transport, {SESSION_ID_HEADER: staged})
    _call_add(transport, mcp_session, "p1", 2, {SESSION_ID_HEADER: staged})
    for rpc_id, headers in ((3, {SESSION_ID_HEADER: other}), (4, None)):
        refused = _call_add(transport, mcp_session, f"x{rpc_id}", rpc_id, headers)
        assert "session_mismatch" in refused.text
        assert staged not in refused.text
        assert _node(transport, f"x{rpc_id}") is None
    store = transport.app.state.write_modes.layer_store
    assert [e["entity_id"] for e in store.load_layer(staged)] == ["p1"]
    assert store.load_layer(other) == []


def test_a_retired_session_path_answers_byte_for_byte_like_an_unknown_one(client):
    import asyncio

    sid = _staged(client)
    asyncio.run(client.app.state.session_manager.delete_session(sid))
    retired = client.get(f"/api/sessions/{sid}")
    unknown = client.get(f"/api/sessions/{UNKNOWN}")
    assert (retired.status_code, retired.content) == (
        unknown.status_code,
        unknown.content,
    )


# -- session routes and tools ask the hook; more refusals ---------------------


@pytest.fixture
def asked(monkeypatch):
    calls = []
    original = access.authorize_graph_access

    def spy(hook, **kwargs):
        calls.append(kwargs)
        return original(hook, **kwargs)

    monkeypatch.setattr(access, "authorize_graph_access", spy)
    return calls


def test_the_archive_route_stages_in_a_staged_session(client):
    sid = _staged(client)
    _add(client, {})
    response = client.post(
        "/api/nodes/archive",
        headers={SESSION_ID_HEADER: sid},
        json={"node_ids": ["s1"], "archived": True},
    )
    assert response.status_code == 200, response.text
    assert response.json()["staged"] is True
    assert _node(client, "s1").archived is False


def test_layer_full_is_422_over_rest(staged_config, mock_llm_provider):
    config = dataclasses.replace(staged_config, session_layer_max_entries="1")
    with patch("backend.ui.chat_logic.create_provider", return_value=mock_llm_provider):
        c = TestClient(create_app(config))
    sid = _staged(c)
    _add(c, {SESSION_ID_HEADER: sid}, "s1")
    response = _add(c, {SESSION_ID_HEADER: sid}, "s2")
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "layer_full"


def test_rest_staged_delete_and_mode_switch_ask_the_hook(client, asked):
    sid = _staged(client)
    plain = client.post("/api/sessions", json={"name": "p"}).json()["id"]
    asked.clear()
    client.patch(f"/api/sessions/{plain}", json={"write_mode": "staged"})
    assert any(c["target"] == "set_session_write_mode" for c in asked)
    asked.clear()
    client.delete(f"/api/sessions/{sid}?discard_staged=true")
    seen = [c for c in asked if c["target"] == "delete_visualization_session"]
    assert seen and seen[0]["graph_operation"] == "discard"
    assert seen[0]["session_id"] == sid


def test_mcp_session_tools_ask_the_hook_and_forward_discard(client, asked):
    tools = client.app.state.tools_map
    created = tools["create_visualization_session"](name="m", write_mode="staged")
    sid = created["session"]["session_id"]
    assert created["session"]["write_mode"] == "staged"
    other = tools["create_visualization_session"](name="n")["session"]["session_id"]
    asked.clear()
    tools["set_session_write_mode"](session_id=other, write_mode="staged")
    assert any(c["target"] == "set_session_write_mode" for c in asked)
    tools["add_nodes"](
        nodes=[{"id": "m1", "type": "Actor", "name": "m1"}],
        edges=[],
        visualization_session_id=sid,
    )
    asked.clear()
    refused = tools["delete_visualization_session"](session_id=sid, confirm=True)
    assert refused["error"] == "layer_not_empty"
    done = tools["delete_visualization_session"](
        session_id=sid, confirm=True, discard_staged=True
    )
    assert done["success"] is True
    assert any(c.get("graph_operation") == "discard" for c in asked)
    gone = tools["get_visualization_session"](session_id=sid)
    assert gone["error"] == "session_not_found"


def test_proposal_session_id_survives_a_reopen(tmp_path):
    db = tmp_path / "gov.db"
    store = SqliteProposalStore(db)
    p = Proposal(
        agent_id="a", tool="t", input_args={}, autonomy_level=AutonomyLevel.PROPOSE
    )
    p.session_id = "1111-2222-3333-4444"
    store.create(p)
    store.close()
    assert SqliteProposalStore(db).get(p.id).session_id == "1111-2222-3333-4444"


def test_read_only_hook_refuses_mcp_mode_switch(client, monkeypatch):
    tools = client.app.state.tools_map
    sid = tools["create_visualization_session"](name="m")["session"]["session_id"]
    monkeypatch.setenv("COMMUNITYOVERVIEW_AUTHORIZATION_MODE", "read-only")
    result = tools["set_session_write_mode"](session_id=sid, write_mode="staged")
    assert result["success"] is False
    assert client.get(f"/api/sessions/{sid}").json()["write_mode"] == "direct"


def test_an_unknown_write_mode_is_refused_not_read_as_direct(client):
    response = client.post("/api/sessions", json={"write_mode": "bogus"})
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "invalid_write_mode"
    assert client.get("/api/sessions").json()["sessions"] == []


def test_a_stale_expected_session_seq_is_409_over_rest(client):
    sid = _staged(client)
    _add(client, {SESSION_ID_HEADER: sid}, "s1")
    response = client.post(
        "/api/nodes",
        headers={SESSION_ID_HEADER: sid},
        json={
            "nodes": [{"id": "s2", "type": "Actor", "name": "s2"}],
            "edges": [],
            "expected_session_seq": 0,
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "session_seq_conflict"


@pytest.mark.parametrize("recorded", ["other", None])
def test_approving_a_proposal_from_another_session_is_a_mismatch(client, recorded):
    sid = _staged(client)
    registry = client.app.state.agent_registry
    proposal = Proposal(
        agent_id="a",
        tool="update_node",
        input_args={},
        autonomy_level=AutonomyLevel.ACT_AFTER_APPROVAL,
    )
    proposal.session_id = _staged(client) if recorded else None
    registry._governance.store.create(proposal)
    response = client.post(
        f"/agents/proposals/{proposal.id}/approve", headers={SESSION_ID_HEADER: sid}
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "session_mismatch"
    assert registry.get_proposal(proposal.id)["status"] == "pending"


def test_a_session_known_only_by_its_push_channel_writes_directly(client):
    sid = "2222-3333-4444-5555"
    client.app.state.session_registry.get_or_create(sid)
    response = _add(client, {SESSION_ID_HEADER: sid})
    assert response.status_code == 200 and "staged" not in response.json()
    assert _node(client, "s1") is not None


def test_the_widget_tool_composes_in_a_session_header_per_message(transport):
    staged = _staged(transport)
    mcp_session = _open_mcp(transport)
    refused = _rpc(
        transport,
        mcp_session,
        "tools/call",
        {"name": "show_graph_widget", "arguments": {"query": "*"}},
        2,
        {SESSION_ID_HEADER: staged},
    )
    assert "session_not_supported" not in refused.text
    assert '"staged_state"' in refused.text
    answered = _rpc(
        transport,
        mcp_session,
        "tools/call",
        {"name": "show_graph_widget", "arguments": {"query": "*"}},
        3,
    )
    assert "session_not_supported" not in answered.text
    assert '"nodes"' in answered.text


# -- the per-message MCP helpers, authorization and boot -------------------


def _request(*values):
    raw = [(SESSION_ID_HEADER.encode(), v.encode()) for v in values]
    return Request({"type": "http", "headers": raw, "method": "POST", "path": "/"})


def test_message_session_two_distinct_headers_is_a_mismatch():
    with pytest.raises(SessionRefused) as exc:
        mcp_tools._message_session(
            _request("1111-2222-3333-4444", "5555-6666-7777-8888")
        )
    assert exc.value.code == "session_mismatch"


def test_message_session_empty_header_is_not_found_not_none():
    with pytest.raises(SessionRefused) as exc:
        mcp_tools._message_session(_request(""))
    assert exc.value.code == "session_not_found"


def test_message_session_strips_and_dedupes_equal_values():
    sid = "1111-2222-3333-4444"
    assert mcp_tools._message_session(_request(sid, f" {sid} ")) == sid


def test_message_request_outside_a_request_context_is_not_a_message():
    fake = SimpleNamespace(
        get_context=lambda: SimpleNamespace(request_context=SimpleNamespace())
    )
    assert mcp_tools._message_request(fake) == (False, None)


def test_a_read_tool_naming_a_staged_session_composes_in_it(client):
    sid = _staged(client)
    _add(client, {SESSION_ID_HEADER: sid}, node_id="staged-only")
    result = client.app.state.tools_map["search_graph"](
        query="staged-only", visualization_session_id=sid
    )
    assert [(n["id"], n["staged_state"]) for n in result["nodes"]] == [
        ("staged-only", "created")
    ]


def test_a_bound_header_naming_an_unknown_session_is_not_a_push(client):
    with bind_session(UNKNOWN):
        result = client.app.state.tools_map["search_graph"](
            query="x", visualization_session_id=UNKNOWN
        )
    assert result["error"] == "session_not_found"


@pytest.mark.parametrize("falsy", ["", 0, False, [], {}])
def test_a_falsy_non_string_session_argument_is_not_read_as_none(client, falsy):
    result = client.app.state.tools_map["add_nodes"](
        nodes=[{"id": "z", "type": "Actor", "name": "z"}],
        edges=[],
        visualization_session_id=falsy,
    )
    assert result["error"] == "session_not_found"
    assert client.app.state.graph_storage.get_node("z") is None


def test_an_empty_push_argument_still_means_no_push(client):
    result = client.app.state.tools_map["search_graph"](
        query="x", visualization_session_id=""
    )
    assert "error" not in result


def test_check_session_seq_on_an_unknown_session_raises():
    from backend.core.session_manager import SessionManager, SessionNotFound
    from backend.core.session_store import (
        InMemorySessionPersistenceBackend,
        SessionStore,
    )

    manager = SessionManager(SessionStore(InMemorySessionPersistenceBackend()))
    with pytest.raises(SessionNotFound):
        manager.check_session_seq("1111-2222-3333-4444", None)


def _call(client, mcp_session, name, arguments, rpc_id, headers=None):
    response = _rpc(
        client,
        mcp_session,
        "tools/call",
        {"name": name, "arguments": arguments},
        rpc_id,
        headers,
    )
    text = response.text
    for line in text.splitlines():
        if line.startswith("data:"):
            text = line[5:]
            break
    return json.loads(text)


def test_each_mcp_message_carries_its_own_authorization_headers(
    staged_config, mock_llm_provider
):
    with patch("backend.ui.chat_logic.create_provider", return_value=mock_llm_provider):
        with TestClient(create_app(staged_config)) as c:
            seen = []

            class Spy:
                def evaluate(self, context):
                    seen.append(context.scope.get("workspace_id", ""))
                    from backend.runtime.authorization import (
                        GraphAuthorizationDecision,
                    )

                    return GraphAuthorizationDecision(allowed=True)

            c.app.state.graph_service._authorization_hook = Spy()
            mcp_session = _open_mcp(
                c, {"x-communityoverview-workspace-id": "opened-with"}
            )
            args = {"nodes": [{"id": "q1", "type": "Actor", "name": "q"}], "edges": []}
            _call(
                c,
                mcp_session,
                "add_nodes",
                args,
                2,
                {"x-communityoverview-workspace-id": "ws-per-message"},
            )
            args2 = {"nodes": [{"id": "q2", "type": "Actor", "name": "q"}], "edges": []}
            _call(c, mcp_session, "add_nodes", args2, 3)
    assert seen[0] == "ws-per-message"
    assert seen[-1] == ""


def test_postgres_boot_refuses_staging_off_over_staged_records():
    from backend.api_host.config import AppConfig
    from backend.api_host.persistence import (
        PersistenceConfigurationError,
        build_persistence_backend,
    )
    from backend.core.postgres_backend import PostgresGraphPersistenceBackend
    from backend.core.tests.test_persistence_contract_postgres import (
        DSN,
        _server_reachable,
        psycopg,
    )

    if not _server_reachable():
        pytest.skip("no PostgreSQL server")
    schema = f"co_bootr2_{uuid.uuid4().hex[:12]}"
    try:
        on = PostgresGraphPersistenceBackend(DSN, schema=schema, staged_sessions=True)
        on.record_staged("1234-5678-1234-5678")
        on.close()
        config = AppConfig(
            graph_file="graph.json",
            graph_backend="postgres",
            graph_postgres_dsn=DSN,
            graph_postgres_schema=schema,
            graph_postgres_pool_size=1,
        )
        with pytest.raises(PersistenceConfigurationError) as exc:
            build_persistence_backend(config)
        assert "GRAPH_STAGED_SESSIONS" in str(exc.value)
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def test_a_retired_path_answers_exactly_like_an_unknown_one(client):
    sid = _staged(client)
    client.delete(f"/api/sessions/{sid}")
    retired = client.get(f"/api/sessions/{sid}")
    unknown = client.get(f"/api/sessions/{UNKNOWN}")
    assert (retired.status_code, retired.json()) == (
        unknown.status_code,
        unknown.json(),
    )


def test_a_staged_session_is_never_materialised_past_the_hook(
    staged_config, mock_llm_provider, monkeypatch
):
    config = dataclasses.replace(staged_config, session_write_mode_default="staged")
    with patch("backend.ui.chat_logic.create_provider", return_value=mock_llm_provider):
        client = TestClient(create_app(config))
    monkeypatch.setenv("COMMUNITYOVERVIEW_AUTHORIZATION_MODE", "read-only")
    assert client.post("/api/sessions", json={"name": "x"}).status_code == 403
    renamed = client.patch(f"/api/sessions/{UNKNOWN}", json={"name": "joined"})
    assert renamed.json()["write_mode"] == "direct"
    store = client.app.state.graph_storage.layer_store
    assert store.staged_status(UNKNOWN) == "none"


def test_rest_delete_nodes_in_a_staged_session_stages_a_tombstone(client):
    client.post(
        "/api/nodes",
        json={"nodes": [{"id": "m1", "type": "Actor", "name": "m1"}], "edges": []},
    )
    sid = _staged(client)
    response = client.request(
        "DELETE",
        "/api/nodes",
        headers={SESSION_ID_HEADER: sid},
        json={"node_ids": ["m1"], "confirmed": True},
    )
    assert response.status_code == 200, response.text
    assert response.json().get("staged") is True
    assert _node(client, "m1") is not None


def test_an_instance_that_cannot_read_the_stores_records_acts_in_no_session(
    app_config, mock_llm_provider, monkeypatch
):
    with patch("backend.ui.chat_logic.create_provider", return_value=mock_llm_provider):
        client = TestClient(create_app(app_config))
    tools = client.app.state.tools_map
    sid = client.post("/api/sessions", json={"name": "s"}).json()["id"]
    monkeypatch.setattr(
        client.app.state.write_modes, "store_holds_unreadable_records", lambda: True
    )
    deleted = client.delete(f"/api/sessions/{sid}")
    assert deleted.status_code == 422
    assert deleted.json()["detail"]["error"] == "staged_unsupported"
    assert client.app.state.session_manager.get_session(sid) is not None
    assert _add(client, {SESSION_ID_HEADER: sid}).status_code == 422
    for result in (
        tools["search_graph"](query="x", visualization_session_id=sid),
        tools["delete_visualization_session"](session_id=sid, confirm=True),
        tools["add_nodes"](
            nodes=[{"id": "u", "type": "Actor", "name": "u"}],
            edges=[],
            visualization_session_id=sid,
        ),
    ):
        assert result["error"] == "staged_unsupported"
    assert _node(client, "u") is None
    # A request naming no session is untouched.
    assert _add(client, {}, "plain").status_code == 200


def test_a_stream_join_racing_a_retirement_answers_404(client, monkeypatch):
    from backend.core.session_manager import SessionNotFound

    def retired(_session_id):
        raise SessionNotFound()

    monkeypatch.setattr(client.app.state.session_manager, "get_or_create", retired)
    response = client.get(f"/api/sessions/{UNKNOWN}/stream", params={"client_id": "c"})
    assert response.status_code == 404
    assert response.json()["detail"] == "session not found"


def test_create_app_hands_the_storage_check_to_the_write_modes(
    app_config, mock_llm_provider, monkeypatch
):
    from backend.core.storage import GraphStorage

    monkeypatch.setattr(
        GraphStorage, "staged_records_check", property(lambda self: lambda: True)
    )
    with patch("backend.ui.chat_logic.create_provider", return_value=mock_llm_provider):
        client = TestClient(create_app(app_config))
    assert client.app.state.write_modes.store_holds_unreadable_records() is True


# -- composed reads over HTTP and MCP (§8.2) -------------------------------


def test_a_staged_sessions_own_canvas_resolves_its_staged_nodes(client):
    sid = _staged(client)
    _add(client, {SESSION_ID_HEADER: sid}, node_id="staged-only")
    tools = client.app.state.tools_map
    added = tools["add_nodes_to_session"](session_id=sid, node_ids=["staged-only"])
    assert added["success"] is True, added
    layout = tools["get_visualization_layout"](session_id=sid)
    assert [(n["id"], n["type"]) for n in layout["nodes"]] == [("staged-only", "Actor")]
    resolved = client.get(f"/api/sessions/{sid}", params={"resolve": "true"}).json()
    assert [(n["id"], n["staged_state"]) for n in resolved["resolved"]["nodes"]] == [
        ("staged-only", "created")
    ]


def test_similarity_and_subtypes_compose_over_rest(client):
    sid = _staged(client)
    client.post(
        "/api/nodes",
        headers={SESSION_ID_HEADER: sid},
        json={
            "nodes": [
                {"id": "n1", "type": "Actor", "name": "Zyxwv", "subtypes": ["Zed"]}
            ],
            "edges": [],
        },
    )
    headers = {SESSION_ID_HEADER: sid}
    similar = client.post(
        "/api/similar", headers=headers, json={"name": "Zyxwv", "threshold": 0.9}
    )
    subtypes = client.get("/api/meta/subtypes", headers=headers)
    assert similar.status_code == 200
    assert [s["node"]["id"] for s in similar.json()["similar_nodes"]][:1] == ["n1"]
    assert "Zed" in subtypes.json()["subtypes"].get("Actor", [])
    plain = client.get("/api/meta/subtypes").json()["subtypes"]
    assert "Zed" not in plain.get("Actor", [])


def _typed_app(routes):
    from fastapi import FastAPI

    from backend.api_host.session_binding import add_session_binding_middleware
    from backend.runtime.session_context import current_session_id

    app = FastAPI()
    add_session_binding_middleware(app, api_prefix="/api", is_retired=lambda s: False)
    for path, name in routes:

        def endpoint():
            return {"session": current_session_id()}

        app.add_api_route(path, endpoint, methods=["GET"], name=name)
    return TestClient(app)


def test_a_typed_interface_route_takes_the_session_header():
    typed = _typed_app(
        [
            ("/api/schema", "get_schema"),
            ("/api/actors", "custom_interface_actors"),
            # Shadowed by the fixed route above, which does not compose.
            ("/api/schema", "custom_interface_schema"),
        ]
    )
    headers = {SESSION_ID_HEADER: UNKNOWN}
    assert typed.get("/api/actors", headers=headers).json() == {"session": UNKNOWN}
    shadowed = typed.get("/api/schema", headers=headers)
    assert shadowed.status_code == 400
    assert shadowed.json()["detail"]["error"] == "session_not_supported"


def test_session_resolve_asks_the_hook_as_the_requesting_caller(client):
    sid = _staged(client)
    _add(client, {SESSION_ID_HEADER: sid}, node_id="staged-only")
    client.app.state.tools_map["add_nodes_to_session"](
        session_id=sid, node_ids=["staged-only"]
    )
    seen = []
    hook = client.app.state.graph_service.authorization_hook
    original = hook.evaluate

    def evaluate(context):
        seen.append(context.actor)
        return original(context)

    with patch.object(hook, "evaluate", evaluate):
        client.get(
            f"/api/sessions/{sid}",
            params={"resolve": "true"},
            headers={"x-communityoverview-actor-id": "caller-7"},
        )
    assert seen and all(actor.get("actor_id") == "caller-7" for actor in seen)


def test_composing_tools_take_the_session_argument(client):
    sid = _staged(client)
    _add(client, {SESSION_ID_HEADER: sid}, node_id="staged-only")
    tools = client.app.state.tools_map
    listed = tools["list_saved_views"](visualization_session_id=sid)
    assert listed["success"] is True
    unknown = tools["list_saved_views"](visualization_session_id=UNKNOWN)
    assert unknown["error"] == "session_not_found"


def test_a_typed_interface_path_takes_the_header_only_on_get():
    typed = _typed_app([("/api/actors", "custom_interface_actors")])
    response = typed.post("/api/actors", headers={SESSION_ID_HEADER: UNKNOWN})
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "session_not_supported"
