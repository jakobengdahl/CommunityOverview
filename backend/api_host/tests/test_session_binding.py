"""Acting in a session over HTTP and MCP (docs/SESSION_OVERLAY_CONTRACT.md
§16.1): the header is bound for every request, never ignored, and a staged
session's writes never reach the graph.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from backend.api_host.persistence import (
    PersistenceConfigurationError,
    build_session_settings,
)
from backend.api_host.server import create_app
from backend.runtime.session_context import SESSION_ID_HEADER

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


def test_a_composing_read_in_a_staged_session_is_refused(client):
    sid = _staged(client)
    response = client.post(
        "/api/search", headers={SESSION_ID_HEADER: sid}, json={"query": "x"}
    )
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "session_not_supported"


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
