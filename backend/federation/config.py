"""Federation configuration models and loader.

This module introduces the startup-only federation configuration contract.
Configuration is intentionally read-only at runtime and loaded from file/env.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Literal

from pydantic import BaseModel, Field, validator


DEFAULT_FEDERATION_PATH = "config/federation_config.json"


class FederationEndpoints(BaseModel):
    """Connection endpoints for a remote graph."""

    graph_json_url: Optional[str] = None
    mcp_url: Optional[str] = None
    gui_url: Optional[str] = None


class FederationCapabilities(BaseModel):
    """Allowed operations for a remote graph."""

    allow_read: bool = True
    allow_write: bool = False
    allow_adopt: bool = False


class FederationSync(BaseModel):
    """Sync behavior for a configured remote graph."""

    mode: Literal["manual", "scheduled"] = "scheduled"
    interval_seconds: int = 300
    on_startup: bool = True
    on_demand: bool = True

    @validator("interval_seconds")
    def validate_interval(cls, value: int) -> int:
        if value < 10:
            raise ValueError("interval_seconds must be >= 10")
        return value


class FederationAuth(BaseModel):
    """Authentication hint for remote connection."""

    type: Literal["none", "bearer", "api_key"] = "none"
    env_token: Optional[str] = None


class FederationGraphConfig(BaseModel):
    """Configuration for one external graph target."""

    graph_id: str
    display_name: str
    enabled: bool = True
    trust_level: Literal["internal", "partner", "external"] = "external"
    max_depth_override: Optional[int] = None
    endpoints: FederationEndpoints = Field(default_factory=FederationEndpoints)
    capabilities: FederationCapabilities = Field(default_factory=FederationCapabilities)
    sync: FederationSync = Field(default_factory=FederationSync)
    auth: FederationAuth = Field(default_factory=FederationAuth)

    @validator("graph_id")
    def validate_graph_id(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("graph_id cannot be empty")
        return value

    @validator("max_depth_override")
    def validate_depth_override(cls, value: Optional[int]) -> Optional[int]:
        if value is not None and value < 0:
            raise ValueError("max_depth_override must be >= 0")
        return value

    @validator("endpoints")
    def validate_endpoints(cls, value: FederationEndpoints) -> FederationEndpoints:
        if not any([value.graph_json_url, value.mcp_url, value.gui_url]):
            raise ValueError("At least one endpoint URL must be configured")
        return value


class FederationSettings(BaseModel):
    """Top-level federation settings."""

    enabled: bool = False
    max_traversal_depth: int = 0
    depth_levels: Optional[List[int]] = None
    default_timeout_ms: int = 1200
    allow_live_remote_enrichment: bool = False
    graphs: List[FederationGraphConfig] = Field(default_factory=list)

    @validator("max_traversal_depth")
    def validate_traversal_depth(cls, value: int) -> int:
        if value < 0:
            raise ValueError("max_traversal_depth must be >= 0")
        return value


    @validator("depth_levels")
    def validate_depth_levels(cls, value: Optional[List[int]]) -> Optional[List[int]]:
        if value is None:
            return value
        cleaned = sorted({int(v) for v in value if int(v) >= 1})
        if not cleaned:
            raise ValueError("depth_levels must contain at least one value >= 1")
        return cleaned

    @validator("default_timeout_ms")
    def validate_timeout(cls, value: int) -> int:
        if value < 100:
            raise ValueError("default_timeout_ms must be >= 100")
        return value


class FederationFileConfig(BaseModel):
    """Root model for federation config file."""

    federation: FederationSettings = Field(default_factory=FederationSettings)


def resolve_federation_config_path() -> str:
    """Resolve federation config path from environment or defaults."""
    env_path = os.getenv("FEDERATION_FILE") or os.getenv("GRAPH_FEDERATION_CONFIG")
    if env_path:
        return env_path

    project_root = Path(__file__).parent.parent.parent
    default_path = project_root / DEFAULT_FEDERATION_PATH
    return str(default_path)


def load_federation_config() -> FederationFileConfig:
    """Load and validate federation config, falling back to safe disabled defaults."""
    path = resolve_federation_config_path()

    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        config = FederationFileConfig(**payload)
        print(f"Loaded federation configuration from: {path}")
        return config
    except FileNotFoundError:
        print(f"Federation config not found at {path}, federation disabled")
        return FederationFileConfig()
    except Exception as exc:
        print(f"Invalid federation config at {path}: {exc}, federation disabled")
        return FederationFileConfig()


def summarize_federation_config(config: FederationFileConfig) -> Dict[str, Any]:
    """Generate a compact status summary safe for info/health responses."""
    settings = config.federation
    enabled_graphs = [g for g in settings.graphs if g.enabled]

    return {
        "enabled": settings.enabled,
        "configured_graphs": len(settings.graphs),
        "active_graphs": len(enabled_graphs),
        "max_traversal_depth": settings.max_traversal_depth,
        "depth_levels": settings.depth_levels,
        "allow_live_remote_enrichment": settings.allow_live_remote_enrichment,
        "graphs": [
            {
                "graph_id": graph.graph_id,
                "display_name": graph.display_name,
                "enabled": graph.enabled,
                "trust_level": graph.trust_level,
                "max_depth_override": graph.max_depth_override,
                "capabilities": {
                    "allow_read": graph.capabilities.allow_read,
                    "allow_write": graph.capabilities.allow_write,
                    "allow_adopt": graph.capabilities.allow_adopt,
                },
            }
            for graph in settings.graphs
        ],
    }
