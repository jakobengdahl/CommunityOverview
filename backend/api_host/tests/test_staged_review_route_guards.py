from __future__ import annotations

import pytest

from backend.api_host.tests import test_staged_review_routes as _routes
from backend.api_host.tests.test_staged_review_routes import (
    _staged,
    _stage_node,
    _storage,
)
from backend.core import Node
from backend.runtime.session_context import SESSION_ID_HEADER

client = _routes.client


# RA06
def test_discard_over_rest_removes_only_the_named_entries(client):
    sid = _staged(client)
    _stage_node(client, sid, "s1")
    _stage_node(client, sid, "s2")
    r = client.post(
        f"/api/sessions/{sid}/staged/discard",
        json={"entries": [{"kind": "node", "entity_id": "s1"}]},
    )
    assert r.json()["removed_entries"] == [["node", "s1"]]
    assert client.get(f"/api/sessions/{sid}/staged").json()["count"] == 1


# RA08
def test_merge_over_rest_merges_only_the_named_entries(client):
    sid = _staged(client)
    _stage_node(client, sid, "s1")
    _stage_node(client, sid, "s2")
    r = client.post(
        f"/api/sessions/{sid}/staged/merge",
        json={"merge_id": "m", "entries": [{"kind": "node", "entity_id": "s1"}]},
    )
    assert r.status_code == 200, r.text
    assert _storage(client).get_node("s2") is None


# RA07
def test_merge_over_rest_carries_the_resolutions(client):
    sid = _staged(client)
    _stage_node(client, sid, "t", "Mine")
    _storage(client).add_nodes([Node(id="t", type="Actor", name="Theirs")], [])
    r = client.post(
        f"/api/sessions/{sid}/staged/merge",
        json={
            "merge_id": "m",
            "resolutions": [
                {"kind": "node", "entity_id": "t", "choice": "take_session"}
            ],
        },
    )
    assert r.status_code == 200, r.text
    assert _storage(client).get_node("t").name == "Mine"


# RA05 / RA12 / RA13 / RA14
def test_refusal_details_and_statuses(client):
    sid, other = _staged(client), _staged(client)
    _stage_node(client, sid, "s1")
    raced = client.post(
        f"/api/sessions/{sid}/staged/merge",
        json={"merge_id": "m1", "entries": [{"kind": "node", "entity_id": "zz"}]},
    )
    assert raced.status_code == 409
    assert raced.json()["detail"]["error"] == "merge_raced"
    assert "success" not in raced.json()["detail"]
    client.post(f"/api/sessions/{sid}/staged/merge", json={"merge_id": "m2"})
    taken = client.post(f"/api/sessions/{other}/staged/merge", json={"merge_id": "m2"})
    assert taken.status_code == 409
    assert taken.json()["detail"]["error"] == "merge_id_taken"
    inc = client.post(
        "/api/nodes",
        headers={SESSION_ID_HEADER: other},
        json={
            "nodes": [{"id": "n", "type": "Actor", "name": "N"}],
            "edges": [{"id": "e", "source": "n", "target": "n", "type": "RELATES_TO"}],
        },
    )
    assert inc.status_code == 200, inc.text
    incomplete = client.post(
        f"/api/sessions/{other}/staged/merge",
        json={"merge_id": "m3", "entries": [{"kind": "edge", "entity_id": "e"}]},
    )
    assert incomplete.status_code == 422, incomplete.text
    assert incomplete.json()["detail"]["error"] == "merge_incomplete"


# MT02 / MT03
def test_mcp_discard_honours_entries_and_merge_honours_resolutions(client):
    tools = client.app.state.tools_map
    sid = _staged(client)
    _stage_node(client, sid, "s1")
    _stage_node(client, sid, "s2")
    d = tools["discard_staged_changes"](
        session_id=sid, entries=[{"kind": "node", "entity_id": "s1"}]
    )
    assert d["removed_entries"] == [["node", "s1"]]
    _stage_node(client, sid, "t", "Mine")
    _storage(client).add_nodes([Node(id="t", type="Actor", name="Theirs")], [])
    m = tools["merge_staged_changes"](
        session_id=sid,
        merge_id="mm",
        resolutions=[{"kind": "node", "entity_id": "t", "choice": "take_session"}],
    )
    assert m["success"] is True, m
    assert _storage(client).get_node("t").name == "Mine"


@pytest.mark.parametrize(
    "method,suffix,body,action,target,operation",
    [
        ("GET", "staged", None, "read", "get_staged_changes", ""),
        (
            "POST",
            "staged/merge",
            {"merge_id": "m"},
            "mutate",
            "merge_staged_changes",
            "merge",
        ),
        ("POST", "staged/discard", {}, "mutate", "discard_staged_changes", "discard"),
    ],
)
def test_review_routes_authorize_using_each_requests_headers(
    client, method, suffix, body, action, target, operation
):
    from backend.runtime.authorization import GraphAuthorizationDecision

    sid = _staged(client)
    _stage_node(client, sid)
    seen = []

    class HeaderHook:
        def evaluate(self, context):
            seen.append(context)
            return GraphAuthorizationDecision(
                allowed=context.actor["actor_id"] == "reader"
            )

    client.app.state.graph_service._authorization_hook = HeaderHook()
    for session in (sid, "9999-9999-9999-9999"):
        refused = client.request(
            method,
            f"/api/sessions/{session}/{suffix}",
            json=body,
            headers={"x-communityoverview-actor-id": "denied"},
        )
        assert refused.status_code == 403, refused.text
        context = seen[-1]
        assert (
            context.action,
            context.target,
            context.graph_operation,
            context.session_id,
        ) == (action, target, operation, session)
    assert _storage(client).get_node("s1") is None
    allowed = client.request(
        method,
        f"/api/sessions/{sid}/{suffix}",
        json=body,
        headers={"x-communityoverview-actor-id": "reader"},
    )
    assert allowed.status_code == 200, allowed.text
