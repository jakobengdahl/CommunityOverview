"""Tests for federation configuration loading and validation."""

import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError
from backend.federation.config import FederationSync

from backend.federation.config import (
    FederationFileConfig,
    load_federation_config,
    resolve_federation_config_context,
    resolve_federation_config_path,
    summarize_federation_config,
)


@pytest.fixture(autouse=True)
def reset_env():
    old_federation_file = os.environ.pop("FEDERATION_FILE", None)
    old_graph_fed = os.environ.pop("GRAPH_FEDERATION_CONFIG", None)
    old_tenant_config_dir = os.environ.pop("COMMUNITYOVERVIEW_TENANT_CONFIG_DIR", None)
    yield
    os.environ.pop("FEDERATION_FILE", None)
    os.environ.pop("GRAPH_FEDERATION_CONFIG", None)
    os.environ.pop("COMMUNITYOVERVIEW_TENANT_CONFIG_DIR", None)
    if old_federation_file is not None:
        os.environ["FEDERATION_FILE"] = old_federation_file
    if old_graph_fed is not None:
        os.environ["GRAPH_FEDERATION_CONFIG"] = old_graph_fed
    if old_tenant_config_dir is not None:
        os.environ["COMMUNITYOVERVIEW_TENANT_CONFIG_DIR"] = old_tenant_config_dir


def test_missing_config_disables_federation(tmp_path: Path):
    os.environ["FEDERATION_FILE"] = str(tmp_path / "missing.json")

    config = load_federation_config()

    assert config.federation.enabled is False
    assert config.federation.graphs == []


def test_load_valid_config(tmp_path: Path):
    config_file = tmp_path / "federation.json"
    config_file.write_text(
        json.dumps(
            {
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
                            },
                        }
                    ],
                }
            }
        ),
        encoding="utf-8",
    )
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
    config = FederationFileConfig.model_validate(
        {
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
                            "allow_adopt": True,
                        },
                    }
                ],
            }
        }
    )

    summary = summarize_federation_config(config)

    assert summary["enabled"] is True
    assert summary["configured_graphs"] == 1
    assert summary["active_graphs"] == 1
    assert summary["graphs"][0]["graph_id"] == "esam-main"
    assert summary["graphs"][0]["capabilities"]["allow_adopt"] is True


def test_resolve_path_uses_env_override():
    os.environ["FEDERATION_FILE"] = "/tmp/fed.json"
    assert resolve_federation_config_path() == "/tmp/fed.json"


def test_resolve_path_uses_tenant_config_dir(tmp_path: Path):
    tenant_dir = tmp_path / "tenant-config"
    tenant_dir.mkdir()

    os.environ["COMMUNITYOVERVIEW_TENANT_CONFIG_DIR"] = str(tenant_dir)

    context = resolve_federation_config_context()

    assert context["source"] == "tenant_config_dir"
    assert context["tenant_config_dir"] == str(tenant_dir.resolve())
    assert context["path"] == str((tenant_dir / "federation_config.json").resolve())


def test_explicit_env_override_beats_tenant_config_dir(tmp_path: Path):
    tenant_dir = tmp_path / "tenant-config"
    tenant_dir.mkdir()
    explicit_path = tmp_path / "override.json"

    os.environ["COMMUNITYOVERVIEW_TENANT_CONFIG_DIR"] = str(tenant_dir)
    os.environ["FEDERATION_FILE"] = str(explicit_path)

    context = resolve_federation_config_context()

    assert context["source"] == "explicit_env"
    assert context["tenant_config_dir"] == str(tenant_dir.resolve())
    assert context["path"] == str(explicit_path.resolve())


def test_config_with_depth_levels(tmp_path):
    cfg = tmp_path / "federation.json"
    cfg.write_text(
        json.dumps(
            {
                "federation": {
                    "enabled": True,
                    "max_traversal_depth": 4,
                    "depth_levels": [1, 2, 4],
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
            }
        ),
        encoding="utf-8",
    )

    os.environ["FEDERATION_FILE"] = str(cfg)
    loaded = load_federation_config()

    assert loaded.federation.depth_levels == [1, 2, 4]


def test_validate_interval_valid():
    sync = FederationSync(interval_seconds=300)
    assert sync.interval_seconds == 300


def test_validate_interval_invalid():
    with pytest.raises(ValidationError) as exc_info:
        FederationSync(interval_seconds=5)

    assert "interval_seconds must be >= 10" in str(exc_info.value)
