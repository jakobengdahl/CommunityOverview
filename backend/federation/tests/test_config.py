"""Tests for federation configuration loading and validation."""

import json
import os
from pathlib import Path

import pytest

from backend.federation.config import (
    FederationFileConfig,
    load_federation_config,
    resolve_federation_config_path,
    summarize_federation_config,
)


@pytest.fixture(autouse=True)
def reset_env():
    old_federation_file = os.environ.pop("FEDERATION_FILE", None)
    old_graph_fed = os.environ.pop("GRAPH_FEDERATION_CONFIG", None)
    yield
    if old_federation_file is not None:
        os.environ["FEDERATION_FILE"] = old_federation_file
    if old_graph_fed is not None:
        os.environ["GRAPH_FEDERATION_CONFIG"] = old_graph_fed


def test_missing_config_disables_federation(tmp_path: Path):
    os.environ["FEDERATION_FILE"] = str(tmp_path / "missing.json")

    config = load_federation_config()

    assert config.federation.enabled is False
    assert config.federation.graphs == []


def test_load_valid_config(tmp_path: Path):
    config_file = tmp_path / "federation.json"
    config_file.write_text(json.dumps({
        "federation": {
            "enabled": True,
            "max_traversal_depth": 2,
            "default_timeout_ms": 1200,
            "allow_live_remote_enrichment": True,
            "graphs": [
                {
                    "graph_id": "esam-main",
                    "display_name": "eSam",
                    "enabled": True,
                    "endpoints": {
                        "graph_json_url": "https://example.org/graph.json"
                    }
                }
            ]
        }
    }), encoding="utf-8")
    os.environ["FEDERATION_FILE"] = str(config_file)

    config = load_federation_config()

    assert config.federation.enabled is True
    assert len(config.federation.graphs) == 1
    assert config.federation.graphs[0].graph_id == "esam-main"


def test_invalid_config_falls_back_to_disabled(tmp_path: Path):
    config_file = tmp_path / "broken.json"
    config_file.write_text("{ not-json", encoding="utf-8")
    os.environ["FEDERATION_FILE"] = str(config_file)

    config = load_federation_config()

    assert isinstance(config, FederationFileConfig)
    assert config.federation.enabled is False


def test_summary_contains_graph_metadata():
    config = FederationFileConfig.model_validate({
        "federation": {
            "enabled": True,
            "max_traversal_depth": 2,
            "graphs": [
                {
                    "graph_id": "esam-main",
                    "display_name": "eSam",
                    "enabled": True,
                    "trust_level": "partner",
                    "endpoints": {
                        "graph_json_url": "https://example.org/graph.json"
                    },
                    "capabilities": {
                        "allow_read": True,
                        "allow_write": False,
                        "allow_adopt": True
                    }
                }
            ]
        }
    })

    summary = summarize_federation_config(config)

    assert summary["enabled"] is True
    assert summary["configured_graphs"] == 1
    assert summary["active_graphs"] == 1
    assert summary["graphs"][0]["graph_id"] == "esam-main"
    assert summary["graphs"][0]["capabilities"]["allow_adopt"] is True


def test_resolve_path_uses_env_override():
    os.environ["FEDERATION_FILE"] = "/tmp/fed.json"
    assert resolve_federation_config_path() == "/tmp/fed.json"


def test_config_with_depth_levels(tmp_path):
    cfg = tmp_path / "federation.json"
    cfg.write_text(json.dumps({
        "federation": {
            "enabled": True,
            "max_traversal_depth": 4,
            "depth_levels": [1, 2, 4],
            "graphs": [
                {
                    "graph_id": "esam-main",
                    "display_name": "eSam",
                    "enabled": True,
                    "endpoints": {"graph_json_url": "https://example.invalid/graph.json"}
                }
            ]
        }
    }), encoding="utf-8")

    os.environ["FEDERATION_FILE"] = str(cfg)
    loaded = load_federation_config()

    assert loaded.federation.depth_levels == [1, 2, 4]
