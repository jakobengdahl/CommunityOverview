"""Integration-style tests for GraphService federated search merge."""

import json

from backend.core import GraphStorage
from backend.federation.config import FederationFileConfig
from backend.federation.manager import FederationManager
from backend.service import GraphService


def _make_manager_with_single_cached_node() -> FederationManager:
    config = FederationFileConfig.model_validate({
        "federation": {
            "enabled": True,
            "graphs": [
                {
                    "graph_id": "esam-main",
                    "display_name": "eSam",
                    "enabled": True,
                    "endpoints": {
                        "graph_json_url": "https://example.invalid/graph.json"
                    },
                }
            ],
        }
    })
    manager = FederationManager(config)

    # Inject a cached federated node directly to isolate service merge behavior.
    manager._cache["esam-main"].nodes = {
        "federated::esam-main::remote-1": manager._build_cache(
            config.federation.graphs[0],
            [{"id": "remote-1", "type": "Actor", "name": "eSam external"}],
            [],
        )[0]["federated::esam-main::remote-1"]
    }
    return manager


def test_search_graph_merges_local_and_federated_results(tmp_path):
    graph_file = tmp_path / "graph.json"
    graph_file.write_text(json.dumps({
        "nodes": [
            {
                "id": "local-1",
                "type": "Actor",
                "name": "Local eSam collaboration",
                "description": "Local node",
                "summary": "",
                "tags": [],
                "metadata": {},
            }
        ],
        "edges": [],
    }), encoding="utf-8")

    storage = GraphStorage(str(graph_file))
    manager = _make_manager_with_single_cached_node()

    service = GraphService(storage, federation_manager=manager)
    result = service.search_graph(query="esam", node_types=["Actor"], limit=10)

    assert result["total"] == 2
    assert result["federation"]["included"] is True
    assert result["federation"]["federated_nodes"] == 1


def test_search_graph_respects_limit_before_federated_merge(tmp_path):
    graph_file = tmp_path / "graph.json"
    graph_file.write_text(json.dumps({
        "nodes": [
            {
                "id": "local-1",
                "type": "Actor",
                "name": "Local eSam collaboration",
                "description": "Local node",
                "summary": "",
                "tags": [],
                "metadata": {},
            }
        ],
        "edges": [],
    }), encoding="utf-8")

    storage = GraphStorage(str(graph_file))
    manager = _make_manager_with_single_cached_node()

    service = GraphService(storage, federation_manager=manager)
    result = service.search_graph(query="esam", node_types=["Actor"], limit=1)

    assert result["total"] == 1
    assert result["federation"]["federated_nodes"] == 0


def test_search_graph_applies_federation_depth_budget(tmp_path):
    graph_file = tmp_path / "graph.json"
    graph_file.write_text(json.dumps({"nodes": [], "edges": []}), encoding="utf-8")
    storage = GraphStorage(str(graph_file))

    config = FederationFileConfig.model_validate({
        "federation": {
            "enabled": True,
            "max_traversal_depth": 1,
            "graphs": [
                {
                    "graph_id": "esam-main",
                    "display_name": "eSam",
                    "enabled": True,
                    "max_depth_override": 1,
                    "endpoints": {"graph_json_url": "https://example.invalid/graph.json"},
                }
            ],
        }
    })
    manager = FederationManager(config)

    graph = config.federation.graphs[0]
    cache_nodes, _ = manager._build_cache(
        graph,
        [
            {"id": "remote-1", "type": "Actor", "name": "Depth one"},
            {"id": "remote-2", "type": "Actor", "name": "Depth two", "metadata": {"federation_distance": 2}},
        ],
        [],
    )
    # ensure one node is beyond allowed depth
    cache_nodes["federated::esam-main::remote-2"].metadata["federation_distance"] = 2
    manager._cache["esam-main"].nodes = cache_nodes

    service = GraphService(storage, federation_manager=manager)
    result = service.search_graph(query="Depth", node_types=["Actor"], limit=10)

    names = [n["name"] for n in result["nodes"]]
    assert "Depth one" in names
    assert "Depth two" not in names


def test_search_graph_uses_runtime_federation_depth_override(tmp_path):
    graph_file = tmp_path / "graph.json"
    graph_file.write_text(json.dumps({"nodes": [], "edges": []}), encoding="utf-8")
    storage = GraphStorage(str(graph_file))

    config = FederationFileConfig.model_validate({
        "federation": {
            "enabled": True,
            "max_traversal_depth": 3,
            "graphs": [
                {
                    "graph_id": "esam-main",
                    "display_name": "eSam",
                    "enabled": True,
                    "max_depth_override": 3,
                    "endpoints": {"graph_json_url": "https://example.invalid/graph.json"},
                }
            ],
        }
    })
    manager = FederationManager(config)

    graph = config.federation.graphs[0]
    cache_nodes, _ = manager._build_cache(
        graph,
        [
            {"id": "remote-1", "type": "Actor", "name": "Depth one"},
            {"id": "remote-2", "type": "Actor", "name": "Depth two", "metadata": {"federation_distance": 2}},
        ],
        [],
    )
    cache_nodes["federated::esam-main::remote-2"].metadata["federation_distance"] = 2
    manager._cache["esam-main"].nodes = cache_nodes

    service = GraphService(storage, federation_manager=manager)
    result = service.search_graph(query="Depth", node_types=["Actor"], limit=10, federation_depth=1)

    names = [n["name"] for n in result["nodes"]]
    assert "Depth one" in names
    assert "Depth two" not in names
    assert result["federation"]["depth"] == 1


def test_graph_stats_exposes_depth_and_graph_labels(tmp_path):
    graph_file = tmp_path / "graph.json"
    graph_file.write_text(json.dumps({
        "nodes": [],
        "edges": [],
        "metadata": {"graph_name": "My Local Graph"},
    }), encoding="utf-8")
    storage = GraphStorage(str(graph_file))

    config = FederationFileConfig.model_validate({
        "federation": {
            "enabled": True,
            "max_traversal_depth": 3,
            "graphs": [
                {
                    "graph_id": "esam-main",
                    "display_name": "eSam",
                    "enabled": True,
                    "max_depth_override": 2,
                    "endpoints": {"graph_json_url": "https://example.invalid/graph.json"},
                }
            ],
        }
    })
    manager = FederationManager(config)

    service = GraphService(storage, federation_manager=manager)
    stats = service.get_graph_stats()

    assert stats["federation"]["local_graph_name"] == "My Local Graph"
    assert stats["federation"]["max_selectable_depth"] == 2
    assert stats["federation"]["selectable_depth_levels"] == [1, 2]
    assert stats["federation"]["search_has_multiple_graphs"] is True
    assert stats["federation"]["graph_display_names"]["esam-main"] == "eSam"


def test_graph_stats_uses_configured_depth_levels_when_present(tmp_path):
    graph_file = tmp_path / "graph.json"
    graph_file.write_text(json.dumps({"nodes": [], "edges": []}), encoding="utf-8")
    storage = GraphStorage(str(graph_file))

    config = FederationFileConfig.model_validate({
        "federation": {
            "enabled": True,
            "max_traversal_depth": 4,
            "depth_levels": [1, 3, 4],
            "graphs": [
                {
                    "graph_id": "esam-main",
                    "display_name": "eSam",
                    "enabled": True,
                    "max_depth_override": 3,
                    "endpoints": {"graph_json_url": "https://example.invalid/graph.json"},
                }
            ],
        }
    })
    manager = FederationManager(config)

    service = GraphService(storage, federation_manager=manager)
    stats = service.get_graph_stats()

    assert stats["federation"]["max_selectable_depth"] == 3
    assert stats["federation"]["selectable_depth_levels"] == [1, 3]
