"""Tests for adopting federated nodes into the local graph."""

import json

from backend.core import GraphStorage
from backend.federation.config import FederationFileConfig
from backend.federation.manager import FederationManager
from backend.service import GraphService


def _service_with_cached_federated_node(tmp_path, source_node=None):
    graph_file = tmp_path / "graph.json"
    graph_file.write_text(json.dumps({"nodes": [], "edges": []}), encoding="utf-8")
    storage = GraphStorage(str(graph_file))

    config = FederationFileConfig.model_validate(
        {
            "federation": {
                "enabled": True,
                "graphs": [
                    {
                        "graph_id": "esam-main",
                        "display_name": "eSam",
                        "enabled": True,
                        "capabilities": {"allow_adopt": True},
                        "endpoints": {
                            "graph_json_url": "https://example.invalid/graph.json"
                        },
                    }
                ],
            }
        }
    )

    manager = FederationManager(config)
    cache_nodes, _ = manager._build_cache(
        config.federation.graphs[0],
        [source_node or {"id": "remote-1", "type": "Actor", "name": "External Node"}],
        [],
    )
    manager._cache["esam-main"].nodes = cache_nodes

    return GraphService(storage, federation_manager=manager)


def test_adopt_federated_node_creates_local_clone(tmp_path):
    service = _service_with_cached_federated_node(tmp_path)

    result = service.adopt_federated_node(
        "federated::esam-main::remote-1", local_name="Local clone"
    )

    assert result["success"] is True
    assert result["adopted_node"]["name"] == "Local clone"
    assert result["adopted_node"]["metadata"]["is_adopted"] is True
    assert (
        result["adopted_node"]["metadata"]["adopted_from"]["origin_graph_id"]
        == "esam-main"
    )
    assert result["lineage_edge"]["metadata"]["is_federated_lineage"] is True
    assert len(result["added_edge_ids"]) == 1


def test_adopt_federated_node_requires_existing_cached_node(tmp_path):
    service = _service_with_cached_federated_node(tmp_path)

    result = service.adopt_federated_node("federated::esam-main::missing")

    assert result["success"] is False


def test_adopt_reuses_existing_reference_node_when_forcing_new_copy(tmp_path):
    service = _service_with_cached_federated_node(tmp_path)

    first = service.adopt_federated_node(
        "federated::esam-main::remote-1", local_name="First"
    )
    second = service.adopt_federated_node(
        "federated::esam-main::remote-1", local_name="Second", create_new_copy=True
    )

    assert first["success"] is True
    assert second["success"] is True
    assert len(second["added_edge_ids"]) == 1


def test_adopt_returns_existing_when_already_adopted(tmp_path):
    service = _service_with_cached_federated_node(tmp_path)

    first = service.adopt_federated_node(
        "federated::esam-main::remote-1", local_name="First"
    )
    second = service.adopt_federated_node(
        "federated::esam-main::remote-1", local_name="Second"
    )

    assert first["success"] is True
    assert second["success"] is True
    assert second["already_adopted"] is True
    assert second["adopted_node"]["name"] == "First"
    assert second["added_node_ids"] == []


def test_adopt_can_force_new_copy(tmp_path):
    service = _service_with_cached_federated_node(tmp_path)

    first = service.adopt_federated_node(
        "federated::esam-main::remote-1", local_name="First"
    )
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

    config = FederationFileConfig.model_validate(
        {
            "federation": {
                "enabled": True,
                "graphs": [
                    {
                        "graph_id": "esam-main",
                        "display_name": "eSam",
                        "enabled": True,
                        "capabilities": {"allow_adopt": False},
                        "endpoints": {
                            "graph_json_url": "https://example.invalid/graph.json"
                        },
                    }
                ],
            }
        }
    )

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


# Regression coverage for
# f3e27923-3a2f-40cc-9d0a-a6f4ac3e29c4 ("adopt_federated_node bypasses the
# SavedView annotation write-guard"): adopt_federated_node builds its local
# Node copy and calls storage.add_nodes directly, bypassing the
# mutations.add_nodes wrapper where saved_view_annotation_error is normally
# enforced. A SavedView/VisualizationView adopted from an adversarial or
# unpatched federated source must still be rejected when its metadata carries
# a non-embedded image annotation URL.
_REMOTE_IMAGE_ANNOTATION = {
    "id": "img-1",
    "type": "image",
    "kind": "image",
    "position": {"x": 0, "y": 0},
    "geometry": {"x": 0, "y": 0, "w": 10, "h": 10, "rotation": 0},
    "image": {
        "url": "https://attacker.example/tracker.png",
        "width": 10,
        "height": 10,
    },
    "alt": "",
}
_EMBEDDED_IMAGE_ANNOTATION = {
    **_REMOTE_IMAGE_ANNOTATION,
    "image": {
        "url": (
            "data:image/webp;base64,UklGRhoAAABXRUJQVlA4TA0AAAAvAAAAEAcQERGIiP4HAA=="
        ),
        "width": 10,
        "height": 10,
    },
}


def _federated_saved_view_source(annotation):
    return {
        "id": "remote-view-1",
        "type": "SavedView",
        "name": "Remote View",
        "metadata": {
            "node_ids": ["actor-1"],
            "positions": {"actor-1": {"x": 0, "y": 0}},
            "annotation_schema_version": 1,
            "annotation_document": {
                "schema_version": 1,
                "annotations": [annotation],
            },
            "annotations": [annotation],
        },
    }


def test_adopt_federated_node_rejects_saved_view_with_remote_image_annotation(
    tmp_path,
):
    service = _service_with_cached_federated_node(
        tmp_path,
        source_node=_federated_saved_view_source(_REMOTE_IMAGE_ANNOTATION),
    )

    result = service.adopt_federated_node("federated::esam-main::remote-view-1")

    assert result["success"] is False
    assert "embedded" in result["message"]
    assert result["added_node_ids"] == []
    # Nothing was persisted locally: no local SavedView clone exists.
    assert service.get_saved_view("Remote View")["success"] is False


def test_adopt_federated_node_accepts_saved_view_with_embedded_image_annotation(
    tmp_path,
):
    service = _service_with_cached_federated_node(
        tmp_path,
        source_node=_federated_saved_view_source(_EMBEDDED_IMAGE_ANNOTATION),
    )

    result = service.adopt_federated_node("federated::esam-main::remote-view-1")

    assert result["success"] is True
    assert (
        result["adopted_node"]["metadata"]["annotation_document"]["annotations"][0][
            "image"
        ]["url"]
        == _EMBEDDED_IMAGE_ANNOTATION["image"]["url"]
    )


def test_adopt_federated_node_accepts_saved_view_with_no_annotation_content(
    tmp_path,
):
    source = {
        "id": "remote-view-2",
        "type": "SavedView",
        "name": "Empty Remote View",
        "metadata": {
            "node_ids": ["actor-1"],
            "positions": {"actor-1": {"x": 0, "y": 0}},
        },
    }
    service = _service_with_cached_federated_node(tmp_path, source_node=source)

    result = service.adopt_federated_node("federated::esam-main::remote-view-2")

    assert result["success"] is True
