"""Composed reads (docs/SESSION_OVERLAY_CONTRACT.md §8).

A read made in a staged session answers from the graph with the session's
layer applied; without a session, or in a direct one, it answers as before.
Run over the file backend and, when a server is configured, PostgreSQL.
"""

from __future__ import annotations

import uuid

import pytest

from backend.core import GraphStorage, Node, layer_vectors
from backend.core.storage_backends import FileGraphPersistenceBackend
from backend.core.tests.test_persistence_contract_postgres import (
    DSN,
    _server_reachable,
    psycopg,
)
from backend.runtime.authorization import (
    GraphAccessNarrowing,
    GraphAuthorizationDecision,
)
from backend.runtime.session_context import bind_session
from backend.service.tests.test_staged_writes import World

REMOTE = {"origin_graph_id": "remote"}


class SwitchableHook:
    """Allows everything; narrows to the local graph while ``narrow`` is set,
    and denies while ``deny`` is set. Keeps every context it was asked."""

    def __init__(self):
        self.narrow = False
        self.deny = False
        self.contexts = []

    def evaluate(self, context):
        self.contexts.append(context)
        if self.deny:
            return GraphAuthorizationDecision(allowed=False, reason="no")
        return GraphAuthorizationDecision(
            allowed=True,
            graph_access=GraphAccessNarrowing(enabled=self.narrow),
        )


def _seed(storage: GraphStorage) -> None:
    from backend.core import Edge

    storage.add_nodes(
        [
            Node(id="a", type="Actor", name="Alpha", subtypes=["Agency"]),
            Node(id="b", type="Actor", name="Beta"),
            Node(id="c", type="Actor", name="Gamma"),
            Node(id="r", type="Actor", name="Remote Rho", metadata=dict(REMOTE)),
        ],
        [
            Edge(id="ab", source="a", target="b", type="RELATES_TO"),
            Edge(id="bc", source="b", target="c", type="RELATES_TO"),
            Edge(id="ar", source="a", target="r", type="RELATES_TO"),
        ],
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
        w = World(storage)
        w.hook = SwitchableHook()
        w.service._authorization_hook = w.hook
        yield w
        storage.shutdown_events()
        return
    if not _server_reachable():
        pytest.skip("no PostgreSQL server configured")
    from backend.core.postgres_backend import PostgresGraphPersistenceBackend

    schema = f"co_composed_{uuid.uuid4().hex[:16]}"
    backend = PostgresGraphPersistenceBackend(DSN, schema=schema, staged_sessions=True)
    storage = GraphStorage(persistence_backend=backend)
    storage.WRITE_RESOLVE_DELAY_SECONDS = 0.0
    _seed(storage)
    try:
        w = World(storage)
        w.hook = SwitchableHook()
        w.service._authorization_hook = w.hook
        yield w
    finally:
        storage.shutdown_events()
        backend.close()
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def _ids(entities):
    return sorted(e["id"] for e in entities)


def _states(entities):
    return {e["id"]: e["staged_state"] for e in entities}


# -- §8.1 the rule, and staged_state -------------------------------------------------


def test_each_entry_kind_composes_by_the_rule_and_carries_its_staged_state(world):
    svc = world.service
    sid = world.staged_session()
    with bind_session(sid):
        svc.add_nodes([{"id": "n", "type": "Actor", "name": "New"}], [])
        svc.update_node("a", {"description": "staged"})
        svc.update_node("c", {"description": "orphan"})
        svc.delete_nodes(["b"], confirmed=True)
    revision_of_a = world.storage.get_node("a").revision
    # c leaves the main graph behind the session's back: its update orphans.
    world.storage.delete_nodes(["c"], confirmed=True)
    with bind_session(sid):
        created = svc.get_node_details("n")
        updated = svc.get_node_details("a")
        orphaned = svc.get_node_details("c")
        deleted = svc.get_node_details("b")
        untouched = svc.get_node_details("r")
    assert created["node"]["staged_state"] == "created"
    assert created["node"]["revision"] == 0
    assert updated["node"]["description"] == "staged"
    assert updated["node"]["staged_state"] == "updated"
    assert updated["node"]["revision"] == revision_of_a
    assert orphaned["node"]["description"] == "orphan"
    assert (orphaned["node"]["staged_state"], orphaned["node"]["revision"]) == (
        "orphaned",
        0,
    )
    assert deleted["success"] is False
    assert untouched["node"]["staged_state"] == "none"


def test_an_update_composes_over_the_main_graph_as_it_is_now(world):
    sid = world.staged_session()
    with bind_session(sid):
        world.service.update_node("a", {"description": "staged"})
    world.storage.update_node("a", {"summary": "later on main"})
    with bind_session(sid):
        node = world.service.get_node_details("a")["node"]
    assert (node["description"], node["summary"]) == ("staged", "later on main")


def test_without_a_session_and_in_a_direct_one_reads_answer_as_before(world):
    sid = world.staged_session()
    with bind_session(sid):
        world.service.add_nodes([{"id": "n", "type": "Actor", "name": "New"}], [])
        world.service.delete_nodes(["b"], confirmed=True)
    direct = world.manager.create_session("plain").id
    for session in (None, direct):
        with bind_session(session):
            details = world.service.get_node_details("b")
            search = world.service.search_graph("")
        assert details["success"] is True and "staged_state" not in details["node"]
        assert "n" not in _ids(search["nodes"]) and "b" in _ids(search["nodes"])
        assert all("staged_state" not in n for n in search["nodes"])


def test_another_staged_session_never_sees_this_ones_layer(world):
    one, other = world.staged_session(), world.staged_session()
    with bind_session(one):
        world.service.add_nodes([{"id": "n", "type": "Actor", "name": "New"}], [])
        world.service.update_node("a", {"description": "one's"})
    with bind_session(other):
        assert world.service.get_node_details("n")["success"] is False
        assert world.service.get_node_details("a")["node"]["description"] == ""
        assert "n" not in _ids(world.service.search_graph("")["nodes"])


def test_a_composing_read_writes_nothing(world):
    sid = world.staged_session()
    with bind_session(sid):
        world.service.update_node("a", {"description": "staged"})
    before_main, before_layer = world.main(), world.layer(sid)
    world.events.clear()
    with bind_session(sid):
        world.service.search_graph("")
        world.service.get_related_nodes("a", depth=2)
        world.service.get_graph_stats()
        world.service.export_graph()
    assert world.main() == before_main
    assert world.layer(sid) == before_layer
    assert world.events == []


def test_a_session_that_does_not_resolve_is_refused_not_read_from_main(world):
    with bind_session("1111-2222-3333-4444"):
        result = world.service.search_graph("Alpha")
    assert result["error"] == "session_not_found" and result["nodes"] == []


def test_the_hook_is_asked_about_a_composing_read_with_the_session(world):
    sid = world.staged_session()
    world.hook.deny = True
    with bind_session(sid):
        result = world.service.get_node_details("a")
    assert result["error_code"] == "access_denied"
    assert {c.session_id for c in world.hook.contexts} == {sid}


# -- I4: tombstones and edges ---------------------------------------------------------


def test_a_tombstoned_node_and_its_edges_leave_every_composing_read(world):
    svc = world.service
    sid = world.staged_session()
    with bind_session(sid):
        svc.delete_nodes(["b"], confirmed=True)
        search = svc.search_graph("")
        related = svc.get_related_nodes("a", depth=3)
        details = svc.get_node_details("a")
        stats = svc.get_graph_stats()
        exported = svc.export_graph()
        similar = svc.find_similar_nodes("Beta", threshold=0.9)
    assert "b" not in _ids(search["nodes"])
    assert {"ab", "bc"}.isdisjoint(_ids(search["edges"]))
    # c was reachable only through b.
    assert _ids(related["nodes"]) == ["a", "r"]
    assert _ids(details["edges"]) == ["ar"]
    assert (stats["total_nodes"], stats["total_edges"]) == (3, 1)
    assert "b" not in _ids(exported["nodes"]) and _ids(exported["edges"]) == ["ar"]
    assert "b" not in [s["node"]["id"] for s in similar["similar_nodes"]]


def test_an_edge_to_a_node_main_deleted_since_is_not_composed(world):
    sid = world.staged_session()
    with bind_session(sid):
        world.service.add_edge("c", "a", type="RELATES_TO")
    world.storage.delete_nodes(["c"], confirmed=True)
    with bind_session(sid):
        details = world.service.get_node_details("a")
        exported = world.service.export_graph()
    assert all("c" not in (e["source"], e["target"]) for e in details["edges"])
    assert all("c" not in (e["source"], e["target"]) for e in exported["edges"])


def test_traversal_walks_staged_edges_and_not_tombstoned_ones(world):
    svc = world.service
    sid = world.staged_session()
    with bind_session(sid):
        svc.add_nodes(
            [{"id": "n", "type": "Actor", "name": "New"}],
            [{"id": "cn", "source": "c", "target": "n", "type": "RELATES_TO"}],
        )
        svc.delete_edges(["ab"], confirmed=True)
        from_c = svc.get_related_nodes("c", depth=1)
        from_a = svc.get_related_nodes("a", depth=3)
    assert _ids(from_c["nodes"]) == ["b", "c", "n"]
    assert _states(from_c["edges"]) == {"bc": "none", "cn": "created"}
    assert _ids(from_a["nodes"]) == ["a", "r"]


def test_traversal_follows_a_staged_archive_change(world):
    sid = world.staged_session()
    with bind_session(sid):
        world.service.archive_nodes(["b"])
        hidden = world.service.get_related_nodes("a", depth=2)
        shown = world.service.get_related_nodes("a", depth=2, include_archived=True)
    assert _ids(hidden["nodes"]) == ["a", "r"]
    assert _ids(shown["nodes"]) == ["a", "b", "c", "r"]


def test_a_recreated_edge_runs_between_its_new_endpoints(world):
    svc = world.service
    sid = world.staged_session()
    with bind_session(sid):
        svc.delete_edges(["ab"], confirmed=True)
        svc.add_nodes(
            [],
            [{"id": "ab", "source": "c", "target": "a", "type": "RELATES_TO"}],
        )
        from_b = svc.get_related_nodes("b", depth=1)
        from_c = svc.get_related_nodes("c", depth=1)
    assert "ab" not in _ids(from_b["edges"])
    assert "ab" in _ids(from_c["edges"]) and "a" in _ids(from_c["nodes"])


# -- lexical search -------------------------------------------------------------------


def test_lexical_search_sees_staged_content_and_not_the_main_content_it_replaced(
    world,
):
    svc = world.service
    sid = world.staged_session()
    with bind_session(sid):
        svc.update_node("a", {"name": "Omega"})
        svc.add_nodes([{"id": "n", "type": "Actor", "name": "Alphabet"}], [])
        old_name = svc.search_graph("Alpha")
        new_name = svc.search_graph("Omega")
    assert _states(old_name["nodes"]) == {"n": "created"}
    assert _states(new_name["nodes"]) == {"a": "updated"}


def test_a_staged_name_match_outranks_a_main_description_match(world):
    world.storage.add_nodes(
        [Node(id="d", type="Actor", name="Delta", description="about zebra")], []
    )
    sid = world.staged_session()
    with bind_session(sid):
        world.service.add_nodes([{"id": "z", "type": "Actor", "name": "Zebra"}], [])
        result = world.service.search_graph("zebra")
    assert [n["id"] for n in result["nodes"]] == ["z", "d"]


def test_a_search_limit_counts_composed_results(world):
    sid = world.staged_session()
    with bind_session(sid):
        world.service.delete_nodes(["a"], confirmed=True)
        result = world.service.search_graph("", limit=3)
    assert len(result["nodes"]) == 3 and "a" not in _ids(result["nodes"])


# -- §8.3 semantic search and layer vectors -------------------------------------------


class FakeEmbedder:
    """Embeds text as a fixed vector per keyword, so scores are predictable."""

    KEYWORDS = ("ocean", "forest", "desert")

    def __init__(self):
        self.embedded = []

    def vector(self, text):
        text = text.lower()
        return [1.0 if k in text else 0.0 for k in self.KEYWORDS]

    def install(self, store, main_hits):
        def compute(nodes):
            self.embedded.extend(n.id for n in nodes)
            return {n.id: self.vector(store._get_text_representation(n)) for n in nodes}

        def search(query_text=None, query_node=None, limit=5, threshold=0.0):
            return [h for h in main_hits if h[1] >= threshold][:limit]

        store.compute_node_embeddings = compute
        store.embed_query = self.vector
        store.search = search


def test_semantic_search_drops_superseded_and_tombstoned_main_vectors(world):
    embedder = FakeEmbedder()
    # The main index still scores a and b on their main content.
    embedder.install(world.storage.vector_store, [("a", 0.95), ("b", 0.9)])
    sid = world.staged_session()
    with bind_session(sid):
        world.service.update_node("a", {"description": "a forest"})
        world.service.delete_nodes(["b"], confirmed=True)
        world.service.add_nodes(
            [{"id": "n", "type": "Actor", "name": "Ocean watch"}], []
        )
        result = world.service.search_graph("ocean", semantic=True)
    assert result["semantic"] is True
    assert _states(result["nodes"]) == {"n": "created"}


def test_without_a_session_semantic_search_reads_the_main_index_only(world):
    embedder = FakeEmbedder()
    embedder.install(world.storage.vector_store, [("a", 0.95)])
    sid = world.staged_session()
    with bind_session(sid):
        world.service.add_nodes([{"id": "n", "type": "Actor", "name": "Ocean"}], [])
    result = world.service.search_graph("ocean", semantic=True)
    assert _ids(result["nodes"]) == ["a"]
    assert embedder.embedded == []


def test_staged_entries_are_embedded_in_the_background_when_staged(world, monkeypatch):
    monkeypatch.setattr(layer_vectors, "embeddings_installed", lambda: True)
    embedder = FakeEmbedder()
    embedder.install(world.storage.vector_store, [])
    vectors = world.service.session_gate.layer_vectors
    sid = world.staged_session()
    with bind_session(sid):
        world.service.add_nodes([{"id": "n", "type": "Actor", "name": "Ocean"}], [])
    vectors._executor.submit(lambda: None).result(timeout=10)
    assert set(vectors.held(sid)) == {"n"}
    # A changed entry is embedded again; an unchanged one is not.
    with bind_session(sid):
        world.service.update_node("n", {"name": "Forest"})
    vectors._executor.submit(lambda: None).result(timeout=10)
    assert embedder.embedded == ["n", "n"]
    with bind_session(sid):
        result = world.service.search_graph("forest", semantic=True)
    assert _ids(result["nodes"]) == ["n"]
    assert embedder.embedded == ["n", "n"]


def test_a_composing_semantic_read_embeds_missing_entries_on_demand(world):
    embedder = FakeEmbedder()
    embedder.install(world.storage.vector_store, [])
    sid = world.staged_session()
    with bind_session(sid):
        world.service.add_nodes([{"id": "n", "type": "Actor", "name": "Desert"}], [])
        result = world.service.search_graph("desert", semantic=True)
    assert _ids(result["nodes"]) == ["n"] and embedder.embedded == ["n"]


def test_a_discarded_entrys_vector_is_dropped(world):
    embedder = FakeEmbedder()
    embedder.install(world.storage.vector_store, [])
    sid = world.staged_session()
    with bind_session(sid):
        world.service.add_nodes([{"id": "n", "type": "Actor", "name": "Desert"}], [])
        world.service.search_graph("desert", semantic=True)
    assert set(world.service.session_gate.layer_vectors.held(sid)) == {"n"}
    world.storage.layer_store.discard(sid, None)
    with bind_session(sid):
        result = world.service.search_graph("desert", semantic=True)
    assert result["nodes"] == []
    assert world.service.session_gate.layer_vectors.held(sid) == {}


def test_without_the_ml_stack_a_composing_semantic_read_still_answers(
    world, monkeypatch
):
    def no_model():
        raise ImportError("no sentence-transformers")

    monkeypatch.setattr(world.storage.vector_store, "_load_model", no_model)
    sid = world.staged_session()
    with bind_session(sid):
        world.service.add_nodes([{"id": "n", "type": "Actor", "name": "Ocean"}], [])
        semantic = world.service.search_graph("ocean", semantic=True)
        lexical = world.service.search_graph("ocean")
    # Neither index can embed: no semantic answer, and no failure.
    assert semantic["nodes"] == [] and "error" not in semantic
    assert _ids(lexical["nodes"]) == ["n"]


# -- similarity, stats, subtypes, audit, saved views, export -------------------------


def test_similarity_sees_staged_names(world):
    sid = world.staged_session()
    with bind_session(sid):
        world.service.add_nodes([{"id": "n", "type": "Actor", "name": "Alphb"}], [])
        single = world.service.find_similar_nodes("Alpha", threshold=0.7)
        batch = world.service.find_similar_nodes_batch(["Alpha"], threshold=0.7)
    for similar in (
        single["similar_nodes"],
        batch["results"]["Alpha"]["similar_nodes"],
    ):
        states = _states([s["node"] for s in similar])
        assert (states["a"], states["n"]) == ("none", "created")


def test_stats_subtypes_and_audit_compose(world):
    svc = world.service
    sid = world.staged_session()
    with bind_session(sid):
        svc.add_nodes(
            [{"id": "n", "type": "Initiative", "name": "Init", "subtypes": ["Pilot"]}],
            [],
        )
        svc.delete_nodes(["a"], confirmed=True)
        stats = svc.get_graph_stats()
        subtypes = svc.get_subtypes()
        audit = svc.audit_relationship_applicability()
    assert stats["total_nodes"] == 4 and stats["nodes_by_type"]["Initiative"] == 1
    assert stats["total_edges"] == 1
    assert subtypes["subtypes"] == {"Initiative": ["Pilot"]}
    assert audit["success"] is True and audit["violation_count"] == 0


def test_a_staged_saved_view_resolves_in_the_session(world):
    sid = world.staged_session()
    metadata = {"node_ids": ["a", "n"], "positions": {}}
    with bind_session(sid):
        world.service.add_nodes(
            [
                {"id": "n", "type": "Actor", "name": "New"},
                {"id": "v", "type": "SavedView", "name": "Plan", "metadata": metadata},
            ],
            [{"id": "an", "source": "a", "target": "n", "type": "RELATES_TO"}],
        )
        view = world.service.get_saved_view("Plan")
        listed = world.service.list_saved_views()
    assert _states(view["nodes"]) == {"a": "none", "n": "created"}
    assert _states(view["edges"]) == {"an": "created"}
    assert [v["name"] for v in listed["views"]] == ["Plan"]
    assert world.service.get_saved_view("Plan")["success"] is False


def test_session_canvas_resolution_reads_the_sessions_own_composed_view(world):
    sid = world.staged_session()
    with bind_session(sid):
        world.service.add_nodes([{"id": "n", "type": "Actor", "name": "New"}], [])
    with world.service.reading_in(sid):
        resolved = world.service.resolve_session_nodes(["a", "n"])
        semantics = world.service.resolve_session_node_semantics(["n"])
    assert _states(resolved["nodes"]) == {"a": "none", "n": "created"}
    assert semantics["nodes"] == {"n": {"type": "Actor", "status": None}}
    direct = world.manager.create_session("plain").id
    with world.service.reading_in(direct):
        assert _ids(world.service.resolve_session_nodes(["a", "n"])["nodes"]) == ["a"]


# -- narrowing: nothing outside the reader's view, main or layer ---------------------


def test_a_narrowed_reader_never_sees_a_hidden_node_staged_into_view(world):
    """An unnarrowed participant stages the hidden node r into the local
    graph; the composed node would carry r's main content, so a narrowed
    reader in the same session must not see it anywhere."""
    svc = world.service
    sid = world.staged_session()
    with bind_session(sid):
        svc.update_node(
            "r", {"metadata": {"origin_graph_id": None}}, metadata_merge=True
        )
    world.hook.narrow = True
    with bind_session(sid):
        details = svc.get_node_details("r")
        search = svc.search_graph("Rho")
        related = svc.get_related_nodes("a")
        stats = svc.get_graph_stats()
        similar = svc.find_similar_nodes("Remote Rho")
        batch = svc.find_similar_nodes_batch(["Remote Rho"])
        exported = svc.export_graph()
        with svc.reading_in(sid):
            resolved = svc.resolve_session_nodes(["r"])
    assert details["success"] is False
    assert "r" not in _ids(search["nodes"])
    assert "r" not in _ids(related["nodes"])
    assert stats["total_nodes"] == 3
    assert "r" not in [s["node"]["id"] for s in similar["similar_nodes"]]
    assert "r" not in [
        s["node"]["id"] for s in batch["results"]["Remote Rho"]["similar_nodes"]
    ]
    assert "r" not in _ids(exported["nodes"])
    assert resolved["nodes"] == []


def test_a_narrowed_reader_never_sees_a_visible_node_whose_base_was_hidden(world):
    """r was hidden when first staged; main has since made it local. The
    composed node keeps the base's metadata keys, so it stays hidden."""
    svc = world.service
    sid = world.staged_session()
    with bind_session(sid):
        svc.update_node("r", {"description": "staged"})
    world.storage.update_node(
        "r", {"metadata": {"origin_graph_id": None}}, metadata_merge=True
    )
    world.hook.narrow = True
    with bind_session(sid):
        assert svc.get_node_details("r")["success"] is False
    # Without the session the node is local now, and shown.
    assert world.service.get_node_details("r")["success"] is True


def test_a_narrowed_reader_never_sees_an_edge_recreated_over_a_hidden_one(world):
    svc = world.service
    sid = world.staged_session()
    with bind_session(sid):
        svc.delete_edges(["ar"], confirmed=True)
        svc.add_nodes(
            [],
            [{"id": "ar", "source": "a", "target": "b", "type": "RELATES_TO"}],
        )
    world.hook.narrow = True
    with bind_session(sid):
        details = svc.get_node_details("a")
        exported = svc.export_graph()
    assert "ar" not in _ids(details["edges"])
    assert "ar" not in _ids(exported["edges"])


def test_a_narrowed_reader_sees_staged_local_work(world):
    sid = world.staged_session()
    world.hook.narrow = True
    with bind_session(sid):
        world.service.add_nodes([{"id": "n", "type": "Actor", "name": "New"}], [])
        result = world.service.search_graph("New")
    assert _states(result["nodes"]) == {"n": "created"}


def test_a_hidden_nodes_semantic_score_never_reaches_a_narrowed_reader(world):
    embedder = FakeEmbedder()
    embedder.install(world.storage.vector_store, [("r", 0.99), ("a", 0.5)])
    sid = world.staged_session()
    world.hook.narrow = True
    with bind_session(sid):
        result = world.service.search_graph("ocean", semantic=True)
        similar = world.service.find_similar_nodes("ocean", limit=1)
    assert _ids(result["nodes"]) == ["a"]
    assert [s["node"]["id"] for s in similar["similar_nodes"]] == ["a"]


# -- the chat-authorization residue: similarity and subtypes ask the hook -----------


@pytest.mark.parametrize("session", ["none", "staged"])
def test_similarity_and_subtypes_ask_the_hook_and_narrow(world, session):
    svc = world.service
    world.storage.update_node("r", {"subtypes": ["Hidden"]})
    sid = world.staged_session() if session == "staged" else None
    world.hook.narrow = True
    with bind_session(sid):
        similar = svc.find_similar_nodes("Remote Rho", limit=1)
        batch = svc.find_similar_nodes_batch(["Remote Rho"], limit=1)
        subtypes = svc.get_subtypes()
        audit = svc.audit_relationship_applicability()
    assert all(s["node"]["id"] != "r" for s in similar["similar_nodes"])
    assert all(
        s["node"]["id"] != "r" for s in batch["results"]["Remote Rho"]["similar_nodes"]
    )
    assert subtypes["subtypes"] == {"Actor": ["Agency"]}
    assert audit["success"] is True
    targets = {c.target for c in world.hook.contexts}
    assert {
        "find_similar_nodes",
        "find_similar_nodes_batch",
        "get_subtypes",
        "audit_relationship_applicability",
    } <= targets
    world.hook.deny = True
    with bind_session(sid):
        for result in (
            svc.find_similar_nodes("Alpha"),
            svc.find_similar_nodes_batch(["Alpha"]),
            svc.get_subtypes(),
            svc.audit_relationship_applicability(),
        ):
            assert result["error_code"] == "access_denied"


def test_a_narrowed_audit_reports_no_edge_with_a_hidden_endpoint(world):
    world.storage.add_nodes(
        [Node(id="i", type="Initiative", name="Init", metadata=dict(REMOTE))], []
    )
    world.storage.edges["ar"].type = "IMPLEMENTS"
    world.hook.narrow = True
    assert world.service.audit_relationship_applicability()["violations"] == []
    world.hook.narrow = False
    violations = world.service.audit_relationship_applicability()["violations"]
    assert [v["edge_id"] for v in violations] == ["ar"]
