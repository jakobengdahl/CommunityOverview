"""Guards of composed reads that the review loop's mutation rounds showed the
main suite let through (docs/SESSION_OVERLAY_CONTRACT.md §8)."""

from __future__ import annotations

from unittest.mock import MagicMock, Mock

import pytest
import httpx

from fastapi import FastAPI

from backend.core import Edge, Node, layer_vectors
from backend.runtime.authorization import (
    GraphAccessNarrowing,
    GraphAuthorizationDecision,
    get_current_request_authorization_inputs,
)
from backend.runtime.session_context import bind_session
from backend.service import create_rest_router, register_mcp_tools
from backend.service.tests.test_composed_reads import (
    REMOTE,
    FakeEmbedder,
    _ids,
    _states,
    make_world,
)


@pytest.fixture(params=["file", "postgres"])
def world(request, tmp_path):
    yield from make_world(request, tmp_path)


# -- narrowing: main, base and composed are each judged ---------------------------


def test_a_node_main_has_moved_out_of_view_stays_hidden(world):
    svc = world.service
    sid = world.staged_session()
    with bind_session(sid):
        svc.update_node("a", {"metadata": {"origin_graph_id": ""}}, metadata_merge=True)
    world.storage.update_node("a", {"metadata": dict(REMOTE)}, metadata_merge=True)
    world.hook.narrow = True
    with bind_session(sid):
        assert svc.get_node_details("a")["success"] is False


def test_an_edge_whose_main_copy_runs_to_a_hidden_node_stays_hidden(world):
    svc = world.service
    sid = world.staged_session()
    with bind_session(sid):
        svc.update_edge("ab", {"label": "x"})
    world.storage.delete_edges(["ab"])
    world.storage.add_nodes(
        [], [Edge(id="ab", source="a", target="r", type="RELATES_TO")]
    )
    world.hook.narrow = True
    with bind_session(sid):
        exported = svc.export_graph()
        stats = svc.get_graph_stats()
    assert "ab" not in _ids(exported["edges"])
    assert stats["total_edges"] == 1


def test_an_edge_whose_base_ran_to_a_hidden_node_stays_hidden(world):
    svc = world.service
    sid = world.staged_session()
    with bind_session(sid):
        svc.update_edge("ar", {"label": "x"})
    world.storage.delete_edges(["ar"])
    world.storage.add_nodes(
        [], [Edge(id="ar", source="a", target="b", type="RELATES_TO")]
    )
    world.hook.narrow = True
    with bind_session(sid):
        assert "ar" not in _ids(svc.export_graph()["edges"])


# -- I4: edges to nodes absent from the composed view --------------------------------


def test_a_staged_edge_to_a_node_main_deleted_counts_nowhere(world):
    sid = world.staged_session()
    with bind_session(sid):
        world.service.add_edge("c", "a", type="RELATES_TO")
    world.storage.delete_nodes(["c"], confirmed=True)
    with bind_session(sid):
        stats = world.service.get_graph_stats()
        audit = world.service.audit_relationship_applicability()
        search = world.service.search_graph("")
        exported = world.service.export_graph()
    assert stats["total_edges"] == 2
    assert audit["violation_count"] == 0
    assert len(search["edges"]) == 2 and len(exported["edges"]) == 2


def test_a_main_edge_added_to_a_tombstoned_node_counts_nowhere(world):
    sid = world.staged_session()
    with bind_session(sid):
        world.service.delete_nodes(["b"], confirmed=True)
    world.storage.add_nodes(
        [],
        [
            Edge(id="bx", source="b", target="c", type="RELATES_TO"),
            Edge(id="xb", source="c", target="b", type="RELATES_TO"),
        ],
    )
    with bind_session(sid):
        stats = world.service.get_graph_stats()
        exported = world.service.export_graph()
        related = world.service.get_related_nodes("c", depth=2)
        from_b = world.service.get_related_nodes("b", depth=1)
    assert stats["total_edges"] == 1
    assert _ids(exported["edges"]) == ["ar"]
    assert "b" not in _ids(related["nodes"])
    assert from_b["success"] is False


# -- §8.3 the composed vector index ------------------------------------------------------


def test_the_vector_index_overfetches_past_superseded_main_hits(world):
    FakeEmbedder().install(world.storage.vector_store, [("a", 0.95), ("c", 0.9)])
    sid = world.staged_session()
    with bind_session(sid):
        world.service.update_node("a", {"description": "forest"})
    composed = world.service.session_gate.compose(sid)
    hits = composed.vector_store.search(query_text="ocean", limit=1)
    assert [h[0] for h in hits] == ["c"]


def test_the_vector_index_ranks_and_limits_merged_hits(world):
    FakeEmbedder().install(world.storage.vector_store, [("c", 0.5)])
    sid = world.staged_session()
    with bind_session(sid):
        world.service.add_nodes([{"id": "n", "type": "Actor", "name": "Ocean"}], [])
    composed = world.service.session_gate.compose(sid)
    assert [
        h[0] for h in composed.vector_store.search(query_text="ocean", limit=1)
    ] == ["n"]
    assert [
        h[0] for h in composed.vector_store.search(query_text="ocean", limit=5)
    ] == [
        "n",
        "c",
    ]


def test_no_background_embedding_without_the_ml_stack(world, monkeypatch):
    monkeypatch.setattr(layer_vectors, "embeddings_installed", lambda: False)
    embedder = FakeEmbedder()
    embedder.install(world.storage.vector_store, [])
    vectors = world.service.session_gate.layer_vectors
    sid = world.staged_session()
    with bind_session(sid):
        world.service.add_nodes([{"id": "n", "type": "Actor", "name": "Ocean"}], [])
    assert vectors._executor is None and embedder.embedded == []


def test_layer_vectors_keep_a_bounded_number_of_sessions(world, monkeypatch):
    monkeypatch.setattr(layer_vectors, "MAX_SESSIONS", 1)
    FakeEmbedder().install(world.storage.vector_store, [])
    vectors = world.service.session_gate.layer_vectors
    one, two = world.staged_session(), world.staged_session()
    for sid, nid in ((one, "n1"), (two, "n2")):
        with bind_session(sid):
            world.service.add_nodes([{"id": nid, "type": "Actor", "name": "Ocean"}], [])
            world.service.search_graph("ocean", semantic=True)
    assert vectors.held(one) == {} and set(vectors.held(two)) == {"n2"}


def test_layer_vectors_are_held_per_session(world):
    embedder = FakeEmbedder()
    embedder.install(world.storage.vector_store, [])
    for sid in (world.staged_session(), world.staged_session()):
        with bind_session(sid):
            world.service.add_nodes([{"id": "n", "type": "Actor", "name": "Ocean"}], [])
            world.service.search_graph("ocean", semantic=True)
    assert embedder.embedded == ["n", "n"]


# -- the lexical index, subtypes and the audit ------------------------------------------


def test_lexical_search_walks_when_the_main_index_does_not_cover_main(world):
    class Short:
        records = {}

        def __len__(self):
            return 0

        def candidates(self, term):
            return []

    sid = world.staged_session()
    composed = world.service.session_gate.compose(sid)
    composed._searchable_text_cache._main = Short()
    assert [n.id for n in composed.search_nodes("Gamma")] == ["c"]


def test_subtypes_of_one_type_compose(world):
    sid = world.staged_session()
    with bind_session(sid):
        world.service.add_nodes(
            [{"id": "n", "type": "Initiative", "name": "I", "subtypes": ["Pilot"]}], []
        )
        assert world.service.get_subtypes("Actor")["subtypes"] == {"Actor": ["Agency"]}


def test_the_audit_does_not_report_a_tombstoned_edge(world):
    world.storage.edges["ar"].type = "IMPLEMENTS"
    sid = world.staged_session()
    with bind_session(sid):
        world.service.delete_edges(["ar"], confirmed=True)
        assert world.service.audit_relationship_applicability()["violation_count"] == 0


# -- the hook and the session a read composes in --------------------------------------


def test_a_denied_read_never_loads_the_layer(world):
    sid = world.staged_session()
    gate = world.service.session_gate
    calls = []
    original = gate.compose
    gate.compose = lambda *a, **k: (calls.append(1), original(*a, **k))[1]
    world.hook.deny = True
    with bind_session(sid):
        result = world.service.get_node_details("a")
    assert result["error_code"] == "access_denied" and calls == []


def test_reading_in_never_switches_an_already_bound_session(world):
    one, two = world.staged_session(), world.staged_session()
    with bind_session(two):
        world.service.add_nodes([{"id": "n", "type": "Actor", "name": "New"}], [])
    with bind_session(one):
        with world.service.reading_in(two):
            assert world.service.resolve_session_nodes(["n"])["nodes"] == []


def test_typed_reads_compose_and_ask_with_their_own_target(world):
    sid = world.staged_session()
    with bind_session(sid):
        world.service.add_nodes(
            [{"id": "n", "type": "Actor", "name": "New"}],
            [{"id": "an", "source": "a", "target": "n", "type": "RELATES_TO"}],
        )
        nodes = world.service.list_typed_nodes(node_type="Actor")
        edges = world.service.list_typed_edges(edge_type="RELATES_TO")
    assert _states(nodes["nodes"])["n"] == "created"
    assert _states(edges["edges"])["an"] == "created"
    targets = [c.target for c in world.hook.contexts]
    assert targets.count("list_typed_nodes:Actor") >= 2
    assert targets.count("list_typed_edges:RELATES_TO") >= 2


def test_canvas_resolution_asks_with_the_callers_action(world):
    sid = world.staged_session()
    with world.service.reading_in(sid):
        world.service.resolve_session_node_semantics(
            ["a"], action="mutate", target="add_nodes_to_session"
        )
    pairs = [(c.action, c.target) for c in world.hook.contexts]
    assert pairs.count(("mutate", "add_nodes_to_session")) >= 2


def test_mcp_subtypes_and_audit_compose(world):
    mcp = Mock()
    mcp.tool = MagicMock(return_value=lambda f: f)
    tools = register_mcp_tools(mcp, world.service, session_manager=world.manager)
    world.storage.edges["ar"].type = "IMPLEMENTS"
    sid = world.staged_session()
    with bind_session(sid):
        world.service.add_nodes(
            [{"id": "n", "type": "Initiative", "name": "I", "subtypes": ["Pilot"]}], []
        )
        world.service.delete_edges(["ar"], confirmed=True)
    subtypes = tools["get_subtypes"](visualization_session_id=sid)
    audit = tools["audit_relationship_applicability"](visualization_session_id=sid)
    assert subtypes["subtypes"].get("Initiative") == ["Pilot"]
    assert audit["violation_count"] == 0


@pytest.mark.asyncio
async def test_rest_similarity_subtypes_and_audit_ask_as_the_caller(world):
    class HeaderHook:
        def evaluate(self, context):
            headers = get_current_request_authorization_inputs().get("headers") or {}
            return GraphAuthorizationDecision(
                allowed=True,
                graph_access=GraphAccessNarrowing(
                    enabled=headers.get("x-narrow") == "1"
                ),
            )

    world.storage.update_node("r", {"subtypes": ["Hidden"]})
    world.storage.edges["ar"].type = "IMPLEMENTS"
    world.service._authorization_hook = HeaderHook()
    app = FastAPI()
    app.include_router(create_rest_router(world.service), prefix="/api")
    narrow = {"x-narrow": "1"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        single = (
            await client.post(
                "/api/similar",
                headers=narrow,
                json={"name": "Remote Rho", "threshold": 0.3},
            )
        ).json()
        batch = (
            await client.post(
                "/api/similar/batch",
                headers=narrow,
                json={"names": ["Remote Rho"], "threshold": 0.3},
            )
        ).json()
        subtypes = (await client.get("/api/meta/subtypes", headers=narrow)).json()
        audit = (
            await client.get(
                "/api/meta/relationship-applicability/audit", headers=narrow
            )
        ).json()
    assert all(s["node"]["id"] != "r" for s in single["similar_nodes"])
    assert all(
        s["node"]["id"] != "r" for s in batch["results"]["Remote Rho"]["similar_nodes"]
    )
    assert "Hidden" not in subtypes["subtypes"].get("Actor", [])
    assert audit["violations"] == []


@pytest.mark.parametrize("staged", [False, True])
@pytest.mark.parametrize("include_archived", [False, True])
def test_related_nodes_never_cross_a_hidden_bridge(world, staged, include_archived):
    world.storage.delete_edges(["ab", "bc"])
    world.storage.add_nodes(
        [], [Edge(id="rc", source="r", target="c", type="RELATES_TO")]
    )
    sid = world.staged_session() if staged else None
    if staged:
        with bind_session(sid):
            world.service.update_node("a", {"description": "staged"})
    # Exercise both outgoing and incoming traversal through the remote node.
    with bind_session(sid):
        assert _ids(world.service.get_related_nodes("a", depth=2)["nodes"]) == [
            "a",
            "c",
            "r",
        ]
        world.hook.narrow = True
        for anchor in ("a", "c"):
            result = world.service.get_related_nodes(
                anchor, depth=3, include_archived=include_archived
            )
            assert _ids(result["nodes"]) == [anchor]
            assert result["edges"] == []
        assert world.service.add_edge("a", "c", type="RELATES_TO")["success"]
        assert _ids(world.service.get_related_nodes("a", depth=2)["nodes"]) == [
            "a",
            "c",
        ]


@pytest.mark.parametrize(
    "tool,kwargs",
    [
        ("search_graph", {"query": "Alpha"}),
        ("get_related_nodes", {"node_id": "a"}),
        ("get_saved_view", {"name": "Plan"}),
    ],
)
@pytest.mark.parametrize("missing", ["record", "layer_store"])
def test_push_reads_refuse_a_staged_document_without_its_record(
    world, monkeypatch, tool, kwargs, missing
):
    world.storage.add_nodes([Node(id="v", type="SavedView", name="Plan")], [])
    mcp = Mock()
    mcp.tool = MagicMock(return_value=lambda f: f)
    tools = register_mcp_tools(mcp, world.service, session_manager=world.manager)
    sid = world.staged_session()
    if missing == "record":
        monkeypatch.setattr(world.write_modes, "staged_status", lambda _: "none")
        expected = "session_not_found"
    else:
        monkeypatch.setattr(world.write_modes, "_store", None)
        expected = "staged_unsupported"
    result = tools[tool](visualization_session_id=sid, **kwargs)
    assert result["success"] is False and result["error"] == expected


def test_reading_in_refuses_a_retired_record_without_a_session_document(world):
    sid = "1111-2222-3333-4444"
    world.write_modes.record_staged(sid)
    world.write_modes.retire(sid, require_empty=True)
    assert world.manager.get_session(sid) is None
    with world.service.reading_in(sid):
        result = world.service.resolve_session_nodes(["a"])
    assert result["success"] is False and result["error"] == "session_not_found"


@pytest.mark.asyncio
async def test_canvas_reads_pass_on_a_staging_refusal_after_the_session_fetch(
    world, monkeypatch
):
    from backend.service.staging import STAGED_UNSUPPORTED, StagingRefused

    sid = world.staged_session()
    mcp = Mock()
    mcp.tool = MagicMock(return_value=lambda f: f)
    tools = register_mcp_tools(mcp, world.service, session_manager=world.manager)
    assert tools["add_nodes_to_session"](session_id=sid, node_ids=["a"])["success"]
    app = FastAPI()
    app.include_router(
        create_rest_router(world.service, session_manager=world.manager), prefix="/api"
    )

    def refuse():
        raise StagingRefused(STAGED_UNSUPPORTED, "staging became unavailable")

    monkeypatch.setattr(world.service.session_gate, "read_session", refuse)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get(f"/api/sessions/{sid}?resolve=true")
    assert response.status_code == 422
    assert response.json()["detail"]["error"] == "staged_unsupported"
    layout = tools["get_visualization_layout"](session_id=sid)
    assert layout["success"] is False and layout["error"] == "staged_unsupported"


def test_legacy_revision_zero_endpoints_remain_visible_at_the_base_revision(world):
    # Legacy graph files assign revision zero to every entity on load.
    for entity in world.storage.get_all_nodes() + world.storage.get_all_edges():
        entity.revision = 0
    sid = world.staged_session()
    with bind_session(sid):
        assert world.service.update_edge("ab", {"label": "staged"})["success"]
    assert world.layer(sid)[("edge", "ab")]["base_revision"] == 0
    world.hook.narrow = True
    with bind_session(sid):
        edges = {e["id"]: e for e in world.service.get_node_details("a")["edges"]}
    assert edges["ab"]["label"] == "staged"
    assert edges["ab"]["staged_state"] == "updated"


@pytest.mark.parametrize("state", ["none", "updated", "orphaned"])
def test_saved_view_summaries_carry_their_id_and_staged_state(world, state):
    world.storage.add_nodes([Node(id="v", type="SavedView", name="Plan")], [])
    direct = world.service.list_saved_views()["views"][0]
    assert direct["id"] == "v" and "staged_state" not in direct
    sid = world.staged_session()
    if state != "none":
        with bind_session(sid):
            world.service.update_node("v", {"description": "staged"})
    if state == "orphaned":
        world.storage.delete_nodes(["v"], confirmed=True)
    with bind_session(sid):
        views = world.service.list_saved_views()["views"]
    assert _states(views) == {"v": state}
