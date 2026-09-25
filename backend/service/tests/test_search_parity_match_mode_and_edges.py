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
from backend.core.storage_search import MATCH_MODE_ANY_TERM, MATCH_MODE_SUBSTRING
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
    "match_mode, expected",
    [
        (MATCH_MODE_SUBSTRING, []),
        (MATCH_MODE_ANY_TERM, ["exact", "two_terms", "desc_two", "desc_one"]),
    ],
)
def test_federated_search_matches_and_ranks_like_local_search(
    tmp_path, match_mode, expected
):
    local = _local_service(tmp_path / "local", NODES).search_graph(
        query=QUERY, match_mode=match_mode
    )
    federated = _federated_service(tmp_path / "fed", NODES).search_graph(
        query=QUERY, match_mode=match_mode
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
