"""Tests for adopting federated nodes into the local graph."""

import json

from backend.core import GraphStorage
from backend.federation.config import FederationFileConfig
from backend.federation.manager import FederationManager
from backend.service import GraphService


def _service_with_cached_federated_node(tmp_path):
    graph_file = tmp_path / "graph.json"
    graph_file.write_text(json.dumps({"nodes": [], "edges": []}), encoding="utf-8")
    storage = GraphStorage(str(graph_file))

    config = FederationFileConfig.model_validate({
        "federation": {
            "enabled": True,
            "graphs": [
                {
                    "graph_id": "esam-main",
                    "display_name": "eSam",
                    "enabled": True,
                    "capabilities": {"allow_adopt": True},
                    "endpoints": {"graph_json_url": "https://example.invalid/graph.json"},
                }
            ],
        }
    })

    manager = FederationManager(config)
    cache_nodes, _ = manager._build_cache(
        config.federation.graphs[0],
        [{"id": "remote-1", "type": "Actor", "name": "External Node"}],
        [],
    )
    manager._cache["esam-main"].nodes = cache_nodes

    return GraphService(storage, federation_manager=manager)


def test_adopt_federated_node_creates_local_clone(tmp_path):
    service = _service_with_cached_federated_node(tmp_path)

    result = service.adopt_federated_node("federated::esam-main::remote-1", local_name="Local clone")

    assert result["success"] is True
    assert result["adopted_node"]["name"] == "Local clone"
    assert result["adopted_node"]["metadata"]["is_adopted"] is True
    assert result["adopted_node"]["metadata"]["adopted_from"]["origin_graph_id"] == "esam-main"
    assert result["lineage_edge"]["metadata"]["is_federated_lineage"] is True
    assert len(result["added_edge_ids"]) == 1


def test_adopt_federated_node_requires_existing_cached_node(tmp_path):
    service = _service_with_cached_federated_node(tmp_path)

    result = service.adopt_federated_node("federated::esam-main::missing")

    assert result["success"] is False


def test_adopt_reuses_existing_reference_node_when_forcing_new_copy(tmp_path):
    service = _service_with_cached_federated_node(tmp_path)

    first = service.adopt_federated_node("federated::esam-main::remote-1", local_name="First")
    second = service.adopt_federated_node(
        "federated::esam-main::remote-1", local_name="Second", create_new_copy=True
    )

    assert first["success"] is True
    assert second["success"] is True
    assert len(second["added_edge_ids"]) == 1


def test_adopt_returns_existing_when_already_adopted(tmp_path):
    service = _service_with_cached_federated_node(tmp_path)

    first = service.adopt_federated_node("federated::esam-main::remote-1", local_name="First")
    second = service.adopt_federated_node("federated::esam-main::remote-1", local_name="Second")

    assert first["success"] is True
    assert second["success"] is True
    assert second["already_adopted"] is True
    assert second["adopted_node"]["name"] == "First"
    assert second["added_node_ids"] == []


def test_adopt_can_force_new_copy(tmp_path):
    service = _service_with_cached_federated_node(tmp_path)

    first = service.adopt_federated_node("federated::esam-main::remote-1", local_name="First")
    second = service.adopt_federated_node(
        "federated::esam-main::remote-1", local_name="Second", create_new_copy=True
    )

    assert first["success"] is True
    assert second["success"] is True
    assert second.get("already_adopted") is not True
    assert second["adopted_node"]["name"] == "Second"


def test_adopt_blocked_by_capability_policy(tmp_path):
    graph_file = tmp_path / "graph.json"
    graph_file.write_text(json.dumps({"nodes": [], "edges": []}), encoding="utf-8")
    storage = GraphStorage(str(graph_file))

    config = FederationFileConfig.model_validate({
        "federation": {
            "enabled": True,
            "graphs": [
                {
                    "graph_id": "esam-main",
                    "display_name": "eSam",
                    "enabled": True,
                    "capabilities": {"allow_adopt": False},
                    "endpoints": {"graph_json_url": "https://example.invalid/graph.json"},
                }
            ],
        }
    })

    manager = FederationManager(config)
    cache_nodes, _ = manager._build_cache(
        config.federation.graphs[0],
        [{"id": "remote-1", "type": "Actor", "name": "External Node"}],
        [],
    )
    manager._cache["esam-main"].nodes = cache_nodes

    service = GraphService(storage, federation_manager=manager)
    result = service.adopt_federated_node("federated::esam-main::remote-1")

    assert result["success"] is False
    assert "not allowed" in result["message"]
