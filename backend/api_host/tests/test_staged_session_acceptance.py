"""End-to-end acceptance of staged sessions as a workshop uses them
(docs/SESSION_OVERLAY_CONTRACT.md §21, slice S7).

Every test here drives the real app over HTTP (and MCP where a workshop agent
would use it), with the session bound by the header exactly as the frontend
sends it. The service-level suites prove each rule in isolation; this module
proves they hold together on the routes a workshop actually calls (the MCP
case calls the registered tool functions in-process, not the MCP transport):

1. isolation between two staged sessions and from the main graph;
2. composed reads - lexical search, semantic search and traversal - in the
   owning session only;
3. a main-graph edit made during a session surfaces as a conflict with all
   three values;
4. a selective merge of a dependency-closed subset, then discard;
5. direct mode unchanged.

The multi-instance criterion lives in
`test_staged_session_multi_instance_postgres.py`; the browser flows in
`frontend/web/tests/e2e/staged-session.spec.js`.
"""

from __future__ import annotations

import dataclasses
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from backend.api_host.server import create_app
from backend.runtime.session_context import SESSION_ID_HEADER

# node-1 / node-2 / node-3 and edge-1 (node-1 -> node-2) come from the
# conftest's sample graph.
ORIGINAL_DESCRIPTION = "A test organization for testing"


@pytest.fixture
def client(app_config, mock_llm_provider):
    config = dataclasses.replace(app_config, graph_staged_sessions="true")
    with patch("backend.ui.chat_logic.create_provider", return_value=mock_llm_provider):
        yield TestClient(create_app(config))


def _session(client, write_mode="staged") -> str:
    body = {"name": "workshop table"}
    if write_mode:
        body["write_mode"] = write_mode
    response = client.post("/api/sessions", json=body)
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _h(sid):
    return {SESSION_ID_HEADER: sid} if sid else {}


def _add(client, sid, nodes, edges=()):
    response = client.post(
        "/api/nodes", headers=_h(sid), json={"nodes": nodes, "edges": list(edges)}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _patch(client, sid, node_id, updates):
    response = client.patch(
        f"/api/nodes/{node_id}", headers=_h(sid), json={"updates": updates}
    )
    assert response.status_code == 200, response.text
    return response.json()


def _node(client, sid, node_id):
    """The node as a reader in ``sid`` sees it, or None when it is absent."""
    response = client.get(f"/api/nodes/{node_id}", headers=_h(sid))
    if response.status_code == 404:
        return None
    assert response.status_code == 200, response.text
    return response.json()["node"]


def _search(client, sid, query, **kwargs):
    response = client.post(
        "/api/search", headers=_h(sid), json={"query": query, **kwargs}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    # A lexical miss falls back to semantic ranking, which in tests embeds
    # with a random-vector stand-in: a lexical assertion must never read that.
    assert body["semantic"] is bool(kwargs.get("semantic")), body
    return {n["id"]: n.get("staged_state") for n in body["nodes"]}


def _related(client, sid, node_id):
    response = client.post(f"/api/nodes/{node_id}/related", headers=_h(sid), json={})
    assert response.status_code == 200, response.text
    body = response.json()
    return (
        {n["id"]: n.get("staged_state") for n in body["nodes"]},
        {e["id"]: e.get("staged_state") for e in body["edges"]},
    )


def _diff(client, sid):
    response = client.get(f"/api/sessions/{sid}/staged")
    assert response.status_code == 200, response.text
    return {(i["kind"], i["entity_id"]): i for i in response.json()["items"]}


def _main(client):
    return client.app.state.graph_storage


# -- 1. isolation -----------------------------------------------------------------


def test_two_staged_sessions_and_the_main_graph_never_see_each_others_work(client):
    a = _session(client)
    b = _session(client)
    _add(client, a, [{"id": "kelp", "type": "Actor", "name": "Kelp Forum"}])
    _add(client, b, [{"id": "reef", "type": "Actor", "name": "Reef Council"}])
    _patch(client, a, "node-1", {"description": "as table A sees it"})
    _patch(client, b, "node-1", {"description": "as table B sees it"})

    # Each session reads its own work composed over main, and nothing else.
    assert _node(client, a, "kelp")["staged_state"] == "created"
    assert _node(client, a, "reef") is None
    assert _node(client, b, "reef")["staged_state"] == "created"
    assert _node(client, b, "kelp") is None
    assert _node(client, a, "node-1")["description"] == "as table A sees it"
    assert _node(client, b, "node-1")["description"] == "as table B sees it"
    assert set(_diff(client, a)) == {("node", "kelp"), ("node", "node-1")}
    assert set(_diff(client, b)) == {("node", "reef"), ("node", "node-1")}

    # Main, through the route and in storage, holds none of it.
    for read in (lambda i: _node(client, None, i), _main(client).get_node):
        assert read("kelp") is None and read("reef") is None
    assert _node(client, None, "node-1")["description"] == ORIGINAL_DESCRIPTION
    assert _main(client).get_node("node-1").description == ORIGINAL_DESCRIPTION

    # A's merge reaches main and B's reads of it; B's own edit still wins in B,
    # and B's diff now reports what main did meanwhile.
    merged = client.post(f"/api/sessions/{a}/staged/merge", json={"merge_id": "a-1"})
    assert merged.status_code == 200, merged.text
    assert _main(client).get_node("kelp") is not None
    assert _main(client).get_node("node-1").description == "as table A sees it"
    assert _node(client, b, "kelp")["staged_state"] == "none"
    assert _node(client, b, "node-1")["description"] == "as table B sees it"
    assert _node(client, None, "reef") is None
    assert _diff(client, b)[("node", "node-1")]["status"] == "conflict"
    assert _diff(client, a) == {}


# -- 2. composed reads ------------------------------------------------------------


def test_lexical_search_finds_staged_content_in_the_owning_session_only(client):
    a = _session(client)
    b = _session(client)
    _add(client, a, [{"id": "kelp", "type": "Actor", "name": "Kelp Forum"}])
    _patch(
        client,
        a,
        "node-2",
        {"name": "Seagrass Programme", "description": "beds", "tags": []},
    )

    assert _search(client, a, "kelp") == {"kelp": "created"}
    assert _search(client, a, "seagrass") == {"node-2": "updated"}
    # The main content the session replaced no longer matches in the session...
    assert _search(client, a, "test") == {"node-1": "none", "node-3": "none"}
    # ...and does everywhere else, where none of A's work is found.
    assert _search(client, b, "test").keys() == {"node-1", "node-2", "node-3"}
    assert _search(client, None, "test").keys() == {"node-1", "node-2", "node-3"}
    for sid in (b, None):
        assert _search(client, sid, "kelp") == {}
        assert _search(client, sid, "seagrass") == {}


def test_mcp_search_and_traversal_compose_in_the_named_session_only(client):
    """A workshop agent reads through MCP with `visualization_session_id`."""
    tools = client.app.state.tools_map
    a = _session(client)
    b = _session(client)
    _add(
        client,
        a,
        [{"id": "kelp", "type": "Actor", "name": "Kelp Forum"}],
        [
            {
                "id": "kelp-link",
                "source": "kelp",
                "target": "node-1",
                "type": "RELATES_TO",
            }
        ],
    )
    found = tools["search_graph"](query="kelp", visualization_session_id=a)
    assert [n["id"] for n in found["nodes"]] == ["kelp"]
    assert (
        tools["search_graph"](query="kelp", visualization_session_id=b)["nodes"] == []
    )
    assert tools["search_graph"](query="kelp")["nodes"] == []
    related = tools["get_related_nodes"](node_id="node-1", visualization_session_id=a)
    assert "kelp" in {n["id"] for n in related["nodes"]}
    for sid in (b, None):
        kwargs = {"visualization_session_id": sid} if sid else {}
        related = tools["get_related_nodes"](node_id="node-1", **kwargs)
        assert "kelp" not in {n["id"] for n in related["nodes"]}


class KeywordModel:
    """Stands in for the sentence-transformer behind the app's real vector store.

    Only the encoder is replaced: text embeds as one dimension per keyword (a
    small constant keeps every vector non-zero), so scores are predictable.
    The store's own index, its search and the composed layer's vectors all run
    as they do in production, so staged content written into the main index
    would answer a main-graph semantic search and fail the test.
    """

    KEYWORDS = ("ocean", "forest", "desert")

    def encode(self, texts, **_):
        import numpy as np

        def vector(text):
            text = text.lower()
            return [1.0 if k in text else 0.0 for k in self.KEYWORDS] + [0.01]

        if isinstance(texts, str):
            return np.array(vector(texts), dtype=np.float32)
        return np.array([vector(t) for t in texts], dtype=np.float32)


def test_semantic_search_composes_in_the_owning_session_only(client):
    storage = _main(client)
    _patch(client, None, "node-3", {"description": "a guide to the ocean floor"})
    store = storage.vector_store
    store.model = KeywordModel()
    store.load_vectors({})
    store.update_nodes_embeddings(storage.get_all_nodes())
    indexed = store.get_embedding_count()

    def main_index_holds_main_content_only():
        # Checked after every step, so a staged write that reaches the index
        # and is later overwritten or removed still fails here.
        assert store.get_embedding_count() == indexed
        assert not store.has_embedding("watch")
        assert store.get_vector_list("node-3")[:3] == [1.0, 0.0, 0.0]

    main_index_holds_main_content_only()
    a = _session(client)
    b = _session(client)
    _add(client, a, [{"id": "watch", "type": "Actor", "name": "Ocean Watch"}])
    main_index_holds_main_content_only()
    # A also moves node-3 off the topic: its main vector must not answer in A.
    _patch(client, a, "node-3", {"description": "a guide to the forest floor"})
    main_index_holds_main_content_only()

    def semantic(sid, query):
        found = _search(client, sid, query, semantic=True)
        main_index_holds_main_content_only()
        return found

    assert semantic(a, "ocean") == {"watch": "created"}
    assert semantic(a, "forest") == {"node-3": "updated"}
    for sid in (b, None):
        assert set(semantic(sid, "ocean")) == {"node-3"}
        assert semantic(sid, "forest") == {}


def test_traversal_walks_staged_edges_in_the_owning_session_only(client):
    a = _session(client)
    b = _session(client)
    _add(
        client,
        a,
        [{"id": "kelp", "type": "Actor", "name": "Kelp Forum"}],
        [
            {
                "id": "kelp-link",
                "source": "kelp",
                "target": "node-1",
                "type": "RELATES_TO",
            }
        ],
    )
    # A also removes a main edge: traversal in A must stop following it.
    removed = client.delete("/api/edges/edge-1", headers=_h(a))
    assert removed.status_code == 200, removed.text

    nodes, edges = _related(client, a, "node-1")
    assert nodes.get("kelp") == "created" and edges.get("kelp-link") == "created"
    assert "edge-1" not in edges and "node-2" not in nodes
    for sid in (b, None):
        nodes, edges = _related(client, sid, "node-1")
        assert "kelp" not in nodes and "kelp-link" not in edges
        assert "edge-1" in edges and "node-2" in nodes
    assert _main(client).edges.get("edge-1") is not None


# -- 3. concurrent main-graph edits -----------------------------------------------


def test_a_main_edit_during_the_session_is_a_conflict_showing_all_three_values(
    client,
):
    a = _session(client)
    _patch(client, a, "node-1", {"description": "as the workshop wants it"})
    _patch(client, None, "node-1", {"description": "as someone changed it meanwhile"})
    # A main edit to a field the session never touched is no conflict.
    _patch(client, None, "node-2", {"description": "edited in main only"})
    _patch(client, a, "node-2", {"name": "Renamed in the workshop"})

    diff = _diff(client, a)
    item = diff[("node", "node-1")]
    assert item["status"] == "conflict"
    assert item["fields"]["description"] == {
        "base": ORIGINAL_DESCRIPTION,
        "staged": "as the workshop wants it",
        "main": "as someone changed it meanwhile",
        "conflict": True,
    }
    assert diff[("node", "node-2")]["status"] == "clean"

    # Unresolved, the conflict blocks the merge and nothing is written.
    blocked = client.post(f"/api/sessions/{a}/staged/merge", json={"merge_id": "m"})
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["error"] == "merge_blocked"
    assert _main(client).get_node("node-2").name == "Test Project"

    resolved = client.post(
        f"/api/sessions/{a}/staged/merge",
        json={
            "merge_id": "m",
            "resolutions": [
                {"kind": "node", "entity_id": "node-1", "choice": "take_session"}
            ],
        },
    )
    assert resolved.status_code == 200, resolved.text
    node_1, node_2 = _main(client).get_node("node-1"), _main(client).get_node("node-2")
    assert node_1.description == "as the workshop wants it"
    # The clean merge kept main's concurrent edit to the other field.
    assert node_2.name == "Renamed in the workshop"
    assert node_2.description == "edited in main only"


# -- 4. selective merge and discard -----------------------------------------------


def test_a_selective_merge_takes_a_dependency_closed_subset_and_leaves_the_rest(
    client,
):
    a = _session(client)
    _add(
        client,
        a,
        [
            {"id": "kelp", "type": "Actor", "name": "Kelp Forum"},
            {"id": "later", "type": "Actor", "name": "Decide later"},
        ],
        [
            {
                "id": "kelp-link",
                "source": "kelp",
                "target": "node-1",
                "type": "RELATES_TO",
            }
        ],
    )
    _patch(client, a, "node-2", {"name": "Not yet agreed"})
    diff = _diff(client, a)
    assert diff[("edge", "kelp-link")]["depends_on"] == [
        {"kind": "node", "entity_id": "kelp"}
    ]
    edge = {"kind": "edge", "entity_id": "kelp-link"}

    # The edge without the node it needs is refused, naming the missing entry.
    incomplete = client.post(
        f"/api/sessions/{a}/staged/merge", json={"merge_id": "s-1", "entries": [edge]}
    )
    assert incomplete.status_code == 422, incomplete.text
    detail = incomplete.json()["detail"]
    assert detail["error"] == "merge_incomplete"
    assert {"kind": "node", "entity_id": "kelp"} in detail["missing"]
    assert _main(client).edges.get("kelp-link") is None

    merged = client.post(
        f"/api/sessions/{a}/staged/merge",
        json={
            "merge_id": "s-1",
            "entries": [edge, {"kind": "node", "entity_id": "kelp"}],
        },
    )
    assert merged.status_code == 200, merged.text
    assert _main(client).get_node("kelp") is not None
    assert _main(client).edges.get("kelp-link").source == "kelp"
    assert _main(client).get_node("later") is None
    assert _main(client).get_node("node-2").name == "Test Project"
    assert set(_diff(client, a)) == {("node", "later"), ("node", "node-2")}

    # Discard one entry, then the rest: main never sees either.
    one = client.post(
        f"/api/sessions/{a}/staged/discard",
        json={"entries": [{"kind": "node", "entity_id": "later"}]},
    )
    assert one.status_code == 200, one.text
    assert set(_diff(client, a)) == {("node", "node-2")}
    assert _node(client, a, "later") is None
    rest = client.post(f"/api/sessions/{a}/staged/discard", json={})
    assert rest.status_code == 200, rest.text
    assert _diff(client, a) == {}
    assert _node(client, a, "node-2")["name"] == "Test Project"
    assert _main(client).get_node("later") is None
    assert _main(client).get_node("node-2").name == "Test Project"


# -- 5. direct mode ---------------------------------------------------------------


@pytest.mark.parametrize("session", ["none", "direct"])
def test_direct_mode_writes_land_in_the_main_graph_as_before(client, session):
    sid = _session(client, write_mode=None) if session == "direct" else None
    staged = _session(client)
    added = _add(client, sid, [{"id": "plain", "type": "Actor", "name": "Plain Write"}])
    assert not added.get("staged")
    updated = _patch(client, sid, "node-1", {"description": "written directly"})
    assert not updated.get("staged")

    assert _main(client).get_node("plain") is not None
    assert _main(client).get_node("node-1").description == "written directly"
    # Every reader sees it at once, a staged session included, and nothing was
    # held for review anywhere.
    for reader in (None, staged):
        assert _node(client, reader, "plain")["name"] == "Plain Write"
        assert _search(client, reader, "plain write").keys() == {"plain"}
    assert _diff(client, staged) == {}
    if sid:
        response = client.post(
            f"/api/sessions/{sid}/staged/merge", json={"merge_id": "d-1"}
        )
        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "session_not_staged"
