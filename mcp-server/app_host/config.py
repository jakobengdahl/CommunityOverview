"""
Configuration for the App Host server.

Provides sensible defaults that can be overridden via environment variables
or by passing a custom AppConfig to create_app().
"""

import os
from dataclasses import dataclass, field
from typing import Optional
from pathlib import Path


@dataclass
class AppConfig:
    """Configuration for the app host server."""

    # Graph storage configuration
    graph_file: str = field(default_factory=lambda: os.getenv("GRAPH_FILE", "graph.json"))
    embeddings_file: Optional[str] = field(default_factory=lambda: os.getenv("EMBEDDINGS_FILE"))

    # Server configuration
    host: str = field(default_factory=lambda: os.getenv("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(os.getenv("PORT", "8000")))

    # API configuration
    api_prefix: str = field(default_factory=lambda: os.getenv("API_PREFIX", "/api"))

    # Static files configuration
    web_static_path: Optional[str] = field(default_factory=lambda: os.getenv("WEB_STATIC_PATH"))
    widget_static_path: Optional[str] = field(default_factory=lambda: os.getenv("WIDGET_STATIC_PATH"))

    # MCP configuration
    mcp_name: str = field(default_factory=lambda: os.getenv("MCP_NAME", "community-knowledge-graph"))

    def __post_init__(self):
        """Resolve default static paths relative to this package."""
        if self.web_static_path is None:
            # Default to apps/web/dist relative to mcp-server directory
            base_dir = Path(__file__).parent.parent
            self.web_static_path = str(base_dir / "apps" / "web" / "dist")

        if self.widget_static_path is None:
            # Default to apps/widget/dist relative to mcp-server directory
            base_dir = Path(__file__).parent.parent
            self.widget_static_path = str(base_dir / "apps" / "widget" / "dist")

    @classmethod
    def from_env(cls) -> "AppConfig":
        """Create configuration from environment variables."""
        return cls()

    def get_graph_path(self) -> Path:
        """Get resolved path to graph file."""
        graph_path = Path(self.graph_file)
        if not graph_path.is_absolute():
            # Resolve relative to mcp-server directory
            base_dir = Path(__file__).parent.parent
            graph_path = base_dir / self.graph_file
        return graph_path
