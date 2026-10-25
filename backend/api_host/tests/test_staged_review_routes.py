"""The review, merge and discard routes and tools over HTTP and MCP
(docs/SESSION_OVERLAY_CONTRACT.md §16.2, §16.3)."""

from __future__ import annotations

import dataclasses
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from backend.api_host.server import create_app
from backend.runtime.session_context import SESSION_ID_HEADER, bind_session

UNKNOWN = "1111-2222-3333-4444"


@pytest.fixture
def client(app_config, mock_llm_provider):
    config = dataclasses.replace(app_config, graph_staged_sessions="true")
    with patch("backend.ui.chat_logic.create_provider", return_value=mock_llm_provider):
        yield TestClient(create_app(config))


def _staged(client) -> str:
    response = client.post("/api/sessions", json={"name": "w", "write_mode": "staged"})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _stage_node(client, sid, node_id="s1", name="Staged"):
    response = client.post(
        "/api/nodes",
        headers={SESSION_ID_HEADER: sid},
        json={"nodes": [{"id": node_id, "type": "Actor", "name": name}], "edges": []},
    )
    assert response.status_code == 200 and response.json()["staged"] is True


def _storage(client):
    return client.app.state.graph_storage


def test_diff_merge_over_rest_lands_the_staged_node(client):
    sid = _staged(client)
    _stage_node(client, sid)
    diff = client.get(f"/api/sessions/{sid}/staged")
    assert diff.status_code == 200
    body = diff.json()
    assert body["count"] == 1
    assert body["items"][0]["entity_id"] == "s1"
    assert body["items"][0]["status"] == "clean"
    merged = client.post(f"/api/sessions/{sid}/staged/merge", json={"merge_id": "m1"})
    assert merged.status_code == 200, merged.text
    assert merged.json()["merged"][0]["outcome"] == "applied"
    assert _storage(client).get_node("s1") is not None
    assert client.get(f"/api/sessions/{sid}/staged").json()["count"] == 0


def test_discard_over_rest_leaves_the_graph_alone(client):
    sid = _staged(client)
    _stage_node(client, sid)
    response = client.post(f"/api/sessions/{sid}/staged/discard", json={})
    assert response.status_code == 200
    assert response.json()["removed_entries"] == [["node", "s1"]]
    assert _storage(client).get_node("s1") is None


def test_a_merge_or_discard_naming_a_reviewed_version_restaged_since_is_refused(client):
    sid = _staged(client)
    _stage_node(client, sid, name="Reviewed")
    reviewed = client.get(f"/api/sessions/{sid}/staged").json()["items"][0]
    entry = {"kind": "node", "entity_id": "s1", "version": reviewed["version"]}
    restaged = client.patch(
        "/api/nodes/s1",
        headers={SESSION_ID_HEADER: sid},
        json={"updates": {"name": "Later"}},
    )
    assert restaged.status_code == 200, restaged.text
    for path, body in (
        ("merge", {"merge_id": "m1", "entries": [entry]}),
        ("discard", {"entries": [entry]}),
    ):
        response = client.post(f"/api/sessions/{sid}/staged/{path}", json=body)
        assert response.status_code == 409, response.text
        assert response.json()["detail"]["error"] == "merge_raced"
    assert _storage(client).get_node("s1") is None
    assert client.get(f"/api/sessions/{sid}/staged").json()["count"] == 1


def test_the_header_may_name_only_the_routes_own_session(client):
    sid = _staged(client)
    other = _staged(client)
    _stage_node(client, sid)
    for method, path, body in (
        ("GET", f"/api/sessions/{sid}/staged", None),
        ("POST", f"/api/sessions/{sid}/staged/merge", {"merge_id": "m"}),
        ("POST", f"/api/sessions/{sid}/staged/discard", {}),
    ):
        response = client.request(
            method, path, json=body, headers={SESSION_ID_HEADER: other}
        )
        assert response.status_code == 400, (path, response.text)
        assert response.json()["detail"]["error"] == "session_mismatch"
        same = client.request(method, path, json=body, headers={SESSION_ID_HEADER: sid})
        assert same.status_code == 200, (path, same.text)


@pytest.mark.parametrize(
    "method,suffix,body",
    [
        ("GET", "staged", None),
        ("POST", "staged/merge", {"merge_id": "m"}),
        ("POST", "staged/discard", {}),
    ],
)
def test_unknown_and_retired_sessions_answer_404(client, method, suffix, body):
    sid = _staged(client)
    assert client.delete(f"/api/sessions/{sid}").status_code == 200
    for name in (sid, UNKNOWN):
        response = client.request(method, f"/api/sessions/{name}/{suffix}", json=body)
        assert response.status_code == 404, response.text


def test_refusals_carry_their_code_and_status(client):
    storage = _storage(client)
    from backend.core import Node

    sid = _staged(client)
    _stage_node(client, sid, "t", "Mine")
    storage.add_nodes([Node(id="t", type="Actor", name="Theirs")], [])
    blocked = client.post(f"/api/sessions/{sid}/staged/merge", json={"merge_id": "m"})
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["error"] == "merge_blocked"
    assert blocked.json()["detail"]["items"][0]["status"] == "id_taken"
    bad = client.post(
        f"/api/sessions/{sid}/staged/merge",
        json={
            "merge_id": "m",
            "resolutions": [{"kind": "node", "entity_id": "t", "choice": "nope"}],
        },
    )
    assert bad.status_code == 422
    assert bad.json()["detail"]["error"] == "invalid_merge_request"
    direct = client.post("/api/sessions", json={"name": "d"}).json()["id"]
    response = client.post(
        f"/api/sessions/{direct}/staged/merge", json={"merge_id": "m"}
    )
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "session_not_staged"


def test_the_mcp_tools_review_merge_and_discard(client):
    tools = client.app.state.tools_map
    sid = _staged(client)
    _stage_node(client, sid, "s1")
    _stage_node(client, sid, "s2")
    diff = tools["get_staged_changes"](session_id=sid)
    assert diff["count"] == 2
    merged = tools["merge_staged_changes"](
        session_id=sid,
        merge_id="mcp-1",
        entries=[{"kind": "node", "entity_id": "s1"}],
    )
    assert merged["success"] is True, merged
    assert _storage(client).get_node("s1") is not None
    discarded = tools["discard_staged_changes"](session_id=sid)
    assert discarded["removed_entries"] == [["node", "s2"]]
    assert _storage(client).get_node("s2") is None
    with bind_session(UNKNOWN):
        mismatch = tools["get_staged_changes"](session_id=sid)
    assert mismatch["error"] == "session_mismatch"
