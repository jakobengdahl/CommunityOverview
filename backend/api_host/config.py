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
    graph_file: str = field(
        default_factory=lambda: os.getenv("GRAPH_FILE", "graph.json")
    )
    embeddings_file: Optional[str] = field(
        default_factory=lambda: os.getenv("EMBEDDINGS_FILE")
    )

    # Shared-session store directory (one JSON file per session). Defaults to a
    # "sessions" directory next to the graph file when unset.
    sessions_dir: Optional[str] = field(
        default_factory=lambda: os.getenv("SESSIONS_DIR")
    )

    # Server configuration
    host: str = field(default_factory=lambda: os.getenv("HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(os.getenv("PORT", "8000")))

    # API configuration
    api_prefix: str = field(default_factory=lambda: os.getenv("API_PREFIX", "/api"))

    # Static files configuration
    web_static_path: Optional[str] = field(
        default_factory=lambda: os.getenv("WEB_STATIC_PATH")
    )
    widget_static_path: Optional[str] = field(
        default_factory=lambda: os.getenv("WIDGET_STATIC_PATH")
    )

    # MCP configuration
    mcp_name: str = field(
        default_factory=lambda: os.getenv("MCP_NAME", "community-knowledge-graph")
    )

    # Security configuration
    auth_enabled: bool = field(
        default_factory=lambda: os.getenv("AUTH_ENABLED", "false").lower() == "true"
    )
    auth_username: str = field(
        default_factory=lambda: os.getenv("AUTH_USERNAME", "admin")
    )
    auth_password: Optional[str] = field(
        default_factory=lambda: os.getenv("AUTH_PASSWORD")
    )
    auth_bearer_token: Optional[str] = field(
        default_factory=lambda: os.getenv("AUTH_BEARER_TOKEN")
    )
    # Default to no cross-origin access (same-origin only). Set CORS_ALLOWED_ORIGINS
    # to a comma-separated list (or "*") to opt specific origins in. A wildcard
    # default would let any site drive an auth-bypassed instance from the browser.
    cors_allowed_origins: list[str] = field(
        default_factory=lambda: [
            o.strip()
            for o in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",")
            if o.strip()
        ]
    )
    mcp_basic_auth: bool = field(
        default_factory=lambda: os.getenv("MCP_BASIC_AUTH", "false").lower() == "true"
    )
    # Number of trusted reverse-proxy hops in front of the app. 0 (default) means
    # the app is reached directly, so per-client rate limiting keys on the socket
    # peer. Behind a proxy (e.g. Cloud Run: set to 1) the real client IP is read
    # from X-Forwarded-For, counting from the right so client-spoofed entries on
    # the left are ignored — otherwise every user shares one rate-limit bucket.
    trusted_proxy_hops: int = field(
        default_factory=lambda: int(os.getenv("TRUSTED_PROXY_HOPS", "0"))
    )
    # When set to False, /mcp and /execute_tool bypass auth even if auth_enabled=True.
    # When None (default / env var absent), MCP follows auth_enabled — no behaviour change.
    mcp_auth_enabled: Optional[bool] = field(
        default_factory=lambda: (
            None
            if (_v := os.getenv("MCP_AUTH_ENABLED")) is None
            else _v.lower() == "true"
        )
    )

    # Profile configuration
    config_profile: str = field(
        default_factory=lambda: os.getenv("CONFIG_PROFILE", "default")
    )

    def __post_init__(self):
        """Resolve default static paths relative to this package."""
        if self.web_static_path is None:
            # Default to frontend/web/dist relative to project root
            project_root = Path(__file__).parent.parent.parent
            self.web_static_path = str(project_root / "frontend" / "web" / "dist")

        if self.widget_static_path is None:
            # Default to frontend/widget/dist relative to project root
            project_root = Path(__file__).parent.parent.parent
            self.widget_static_path = str(project_root / "frontend" / "widget" / "dist")

    @classmethod
    def from_env(cls) -> "AppConfig":
        """Create configuration from environment variables."""
        return cls()

    def get_graph_path(self) -> Path:
        """Get resolved path to graph file."""
        graph_path = Path(self.graph_file)
        if not graph_path.is_absolute():
            # Try resolving relative to project root first (for data/active/graph.json)
            project_root = Path(__file__).parent.parent.parent
            candidate = project_root / self.graph_file
            if candidate.exists() or "data/" in self.graph_file:
                graph_path = candidate
            else:
                # Fall back to resolving relative to backend directory
                backend_dir = Path(__file__).parent.parent
                graph_path = backend_dir / self.graph_file
        return graph_path
