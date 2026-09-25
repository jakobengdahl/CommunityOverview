"""Local and federated search must agree on ``match_mode`` and on which edges
come back.

The same node set is searched once from local storage and once from the
federation cache: both must match and rank it identically in either mode. And an
edge whose other endpoint is not in the results must be dropped whether or not
the limit trim runs: the trim re-filters edges, but only when it cuts nodes, so
the untrimmed path relies on the access filter having done it first.
"""

import pytest

from backend.core import Edge, GraphStorage, Node, NodeType, RelationshipType
from backend.core.storage_search import (
    MATCH_MODE_ANY_TERM,
    MATCH_MODE_SUBSTRING,
    MAX_ANY_TERM_TERMS,
)
from backend.federation.config import FederationFileConfig
from backend.federation.manager import FederationManager
from backend.service import GraphService

# In this order on purpose: ``desc_one`` is scanned before ``desc_two``, so only
# the matched-term tie-break can put ``desc_two`` first.
NODES = [
    {"id": "exact", "type": "Initiative", "name": "Pricing"},
    {"id": "two_terms", "type": "Initiative", "name": "Pricing plan rollout"},
    {"id": "desc_one", "type": "Actor", "name": "Alpha", "description": "pricing"},
    {
        "id": "desc_two",
        "type": "Actor",
        "name": "Beta",
        "description": "catalogue of pricing",
    },
    {"id": "unrelated", "type": "Actor", "name": "Zeta"},
]

# No node holds the phrase; "two_terms" hits two name-tier terms (400 000 and
# 300 000), which a sum would rank above "exact" (500 000).
QUERY = "pricing rollout catalogue"


def _storage(tmp_path):
    storage = GraphStorage(json_path=str(tmp_path / "g.json"))
    # The ML-free install CI runs: keep the zero-result semantic fallback from
    # standing in for a lexical miss.
    storage.vector_store.search = lambda **kwargs: []
    return storage


def _manager(nodes, edges=()):
    config = FederationFileConfig.model_validate(
        {
            "federation": {
                "enabled": True,
                "graphs": [
                    {
                        "graph_id": "remote",
                        "display_name": "Remote",
                        "enabled": True,
                        "endpoints": {
                            "graph_json_url": "https://example.invalid/graph.json"
                        },
                    }
                ],
            }
        }
    )
    manager = FederationManager(config)
    cache_nodes, cache_edges = manager._build_cache(
        config.federation.graphs[0], list(nodes), list(edges)
    )
    manager._cache["remote"].nodes = cache_nodes
    manager._cache["remote"].edges = cache_edges
    return manager


def _local_service(tmp_path, nodes, edges=()):
    storage = _storage(tmp_path)
    storage.add_nodes(
        [
            Node(
                id=n["id"],
                type=NodeType(n["type"]),
                name=n["name"],
                description=n.get("description", ""),
                tags=n.get("tags", []),
                aliases=n.get("aliases", []),
            )
            for n in nodes
        ],
        [
            Edge(
                id=e["id"],
                source=e["source"],
                target=e["target"],
                type=RelationshipType.RELATES_TO,
            )
            for e in edges
        ],
    )
    return GraphService(storage)


def _federated_service(tmp_path, nodes, edges=()):
    return GraphService(_storage(tmp_path), federation_manager=_manager(nodes, edges))


def _origin_ids(result):
    return [n["id"].rsplit("::", 1)[-1] for n in result["nodes"]]


@pytest.mark.parametrize(
    "query, match_mode, expected",
    [
        (QUERY, MATCH_MODE_SUBSTRING, []),
        (QUERY, MATCH_MODE_ANY_TERM, ["exact", "two_terms", "desc_two", "desc_one"]),
        # One term, so every hit ties on matched terms and scan order decides
        # between the two description matches.
        (
            "pricing",
            MATCH_MODE_SUBSTRING,
            ["exact", "two_terms", "desc_one", "desc_two"],
        ),
        # Runs of whitespace and tabs separate terms; neither yields an empty
        # term, which would match every node including "unrelated".
        (
            "pricing  rollout",
            MATCH_MODE_ANY_TERM,
            ["exact", "two_terms", "desc_one", "desc_two"],
        ),
        (
            "pricing\trollout",
            MATCH_MODE_ANY_TERM,
            ["exact", "two_terms", "desc_one", "desc_two"],
        ),
    ],
)
def test_federated_search_matches_and_ranks_like_local_search(
    tmp_path, query, match_mode, expected
):
    local = _local_service(tmp_path / "local", NODES).search_graph(
        query=query, match_mode=match_mode
    )
    federated = _federated_service(tmp_path / "fed", NODES).search_graph(
        query=query, match_mode=match_mode
    )

    assert _origin_ids(local) == expected
    assert _origin_ids(federated) == expected
    assert federated["federation"]["federated_nodes"] == len(expected)


def test_federated_search_rejects_an_unknown_match_mode():
    with pytest.raises(ValueError):
        _manager(NODES).search_nodes(
            query=QUERY, node_types=None, limit=10, match_mode="fuzzy"
        )


EDGE_NODES = [
    {"id": "hub", "type": "Actor", "name": "Alpha hub"},
    {"id": "spoke", "type": "Actor", "name": "Alpha spoke"},
    {"id": "outside", "type": "Actor", "name": "Beta"},
]
EDGES = [
    {"id": "inside-edge", "source": "hub", "target": "spoke"},
    {"id": "outside-edge", "source": "hub", "target": "outside"},
]


@pytest.mark.parametrize("make_service", [_local_service, _federated_service])
def test_edge_to_a_node_outside_the_results_is_dropped_without_a_trim(
    tmp_path, make_service
):
    """Two matches under a limit of ten: nothing is trimmed, yet the edge to the
    unmatched node must still be dropped."""
    result = make_service(tmp_path, EDGE_NODES, EDGES).search_graph(
        query="alpha", limit=10
    )

    assert sorted(_origin_ids(result)) == ["hub", "spoke"]
    assert [e["id"].rsplit("::", 1)[-1] for e in result["edges"]] == ["inside-edge"]


def test_federated_search_defaults_to_substring_matching():
    result = _manager(NODES).search_nodes(query=QUERY, node_types=None, limit=10)

    assert result["nodes"] == []


def test_federated_search_keeps_the_match_mode_when_semantic_is_requested(tmp_path):
    """Semantic ranking replaces the lexical mode for local results only; the
    federated half is still matched lexically in the requested mode."""
    result = _federated_service(tmp_path, NODES).search_graph(
        query=QUERY, match_mode=MATCH_MODE_ANY_TERM, semantic=True
    )

    assert _origin_ids(result) == ["exact", "two_terms", "desc_two", "desc_one"]


# "strong" hits one term on the name and another on the description; "alias"
# hits one term on an alias. Ranking by the best term puts "strong" first;
# ranking by the weakest would put its description hit below the alias.
TIER_NODES = [
    {"id": "alias", "type": "Actor", "name": "Zed", "aliases": ["rollout"]},
    {"id": "strong", "type": "Actor", "name": "Pricing", "description": "rollout"},
]

# Both are description-tier hits on one distinct term each, so scan order must
# decide; a repeated query word counted twice would lift "second" above "first".
REPEAT_NODES = [
    {"id": "first", "type": "Actor", "name": "Alpha", "description": "pricing"},
    {"id": "second", "type": "Actor", "name": "Beta", "description": "catalogue"},
]


@pytest.mark.parametrize(
    "nodes, query, expected",
    [
        (TIER_NODES, "pricing rollout", ["strong", "alias"]),
        (REPEAT_NODES, "catalogue catalogue pricing", ["first", "second"]),
        # Only the first MAX_ANY_TERM_TERMS distinct terms are matched, so a
        # term past the cap matches nothing.
        (
            REPEAT_NODES,
            " ".join(f"q{i}" for i in range(MAX_ANY_TERM_TERMS)) + " pricing",
            [],
        ),
    ],
)
def test_federated_any_term_ranks_and_caps_terms_like_local_search(
    tmp_path, nodes, query, expected
):
    local = _local_service(tmp_path / "local", nodes).search_graph(
        query=query, match_mode=MATCH_MODE_ANY_TERM
    )
    federated = _federated_service(tmp_path / "fed", nodes).search_graph(
        query=query, match_mode=MATCH_MODE_ANY_TERM
    )

    assert _origin_ids(local) == expected
    assert _origin_ids(federated) == expected


def test_edge_to_a_node_cut_by_the_limit_trim_is_dropped(tmp_path):
    """A tag filter widens the federated window past the limit, so both matches
    come back and the trim cuts one: the edge between them must go with it."""
    nodes = [dict(n, tags=["t"]) for n in EDGE_NODES]
    result = _federated_service(tmp_path, nodes, EDGES).search_graph(
        query="alpha", limit=1, tags_any=["t"]
    )

    assert len(result["nodes"]) == 1
    assert result["edges"] == []
