"""
Configuration loader for the Community Knowledge Graph.

This module loads and validates the schema configuration from a JSON file.
The configuration defines:
- Node types with their fields and colors
- Relationship types
- Presentation settings (colors, prompts, introduction text)

The config file path can be set via SCHEMA_FILE environment variable,
defaulting to config/schema_config.json.
"""

import os
import json
from typing import Dict, List, Optional, Any
from pathlib import Path
from pydantic import BaseModel, Field, validator

# Default config path relative to project root
DEFAULT_CONFIG_PATH = "config/schema_config.json"

# Static node types that must always exist
STATIC_NODE_TYPES = {
    "SavedView": {
        "fields": ["name", "description", "summary", "metadata"],
        "static": True,
        "description": "Saved graph view snapshots for quick navigation",
        "color": "#6B7280"
    },
    "VisualizationView": {
        "fields": ["name", "description", "summary", "metadata"],
        "static": True,
        "description": "Saved graph view snapshots (legacy)",
        "color": "#6B7280"
    }
}


class NodeTypeConfig(BaseModel):
    """Configuration for a single node type."""
    fields: List[str] = Field(default_factory=lambda: ["name", "description", "summary"])
    static: bool = False
    category: str = "domain"  # "domain" = configurable, "system" = foundational
    description: str = ""
    color: str = "#9CA3AF"  # Default gray


class RelationshipTypeConfig(BaseModel):
    """Configuration for a single relationship type."""
    description: str = ""


class SchemaConfig(BaseModel):
    """Schema configuration including node and relationship types."""
    node_types: Dict[str, NodeTypeConfig] = Field(default_factory=dict)
    relationship_types: Dict[str, RelationshipTypeConfig] = Field(default_factory=dict)

    @validator('node_types', pre=True)
    def convert_node_types(cls, v):
        """Convert raw dict values to NodeTypeConfig."""
        if isinstance(v, dict):
            return {k: NodeTypeConfig(**val) if isinstance(val, dict) else val for k, val in v.items()}
        return v

    @validator('relationship_types', pre=True)
    def convert_relationship_types(cls, v):
        """Convert raw dict values to RelationshipTypeConfig."""
        if isinstance(v, dict):
            return {k: RelationshipTypeConfig(**val) if isinstance(val, dict) else val for k, val in v.items()}
        return v


class PresentationConfig(BaseModel):
    """Presentation configuration for UI and prompts."""
    title: str = "Community Knowledge Graph"
    introduction: str = "Welcome to the knowledge graph."
    colors: Dict[str, str] = Field(default_factory=dict)
    prompt_prefix: str = ""
    prompt_suffix: str = ""
    default_language: str = "en"
    widget_url: str = ""  # URL template for the graph widget


class SchemaFileConfig(BaseModel):
    """Root configuration model for the schema file."""
    schema_: SchemaConfig = Field(alias="schema", default_factory=SchemaConfig)
    presentation: PresentationConfig = Field(default_factory=PresentationConfig)

    class Config:
        populate_by_name = True


class ConfigLoader:
    """
    Singleton configuration loader.

    Loads the schema configuration once and provides access to
    schema and presentation settings.
    """
    _instance: Optional['ConfigLoader'] = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._config = None
            cls._instance._config_path = None
        return cls._instance

    def __init__(self):
        if self._config is None:
            self._load_config()

    @classmethod
    def reset_instance(cls):
        """Reset the singleton instance (for testing)."""
        cls._instance = None

    def _get_config_path(self) -> str:
        """Get the configuration file path."""
        # Check environment variable first
        env_path = os.getenv("SCHEMA_FILE") or os.getenv("GRAPH_SCHEMA_CONFIG")
        if env_path:
            return env_path

        # Default path relative to the project root
        # Find project root by looking for config directory
        current = Path(__file__).parent.parent  # Go up from backend/
        config_path = current / DEFAULT_CONFIG_PATH

        if config_path.exists():
            return str(config_path)

        # Try current working directory
        cwd_config = Path.cwd() / DEFAULT_CONFIG_PATH
        if cwd_config.exists():
            return str(cwd_config)

        return str(config_path)  # Return default even if not exists

    def _load_config(self) -> None:
        """Load and validate the configuration file."""
        self._config_path = self._get_config_path()

        try:
            with open(self._config_path, 'r', encoding='utf-8') as f:
                raw_config = json.load(f)

            self._config = SchemaFileConfig(**raw_config)
            print(f"Loaded schema configuration from: {self._config_path}")

        except FileNotFoundError:
            print(f"Warning: Config file not found at {self._config_path}, using defaults")
            self._config = SchemaFileConfig()

        except json.JSONDecodeError as e:
            print(f"Warning: Invalid JSON in config file: {e}, using defaults")
            self._config = SchemaFileConfig()

        except Exception as e:
            print(f"Warning: Error loading config: {e}, using defaults")
            self._config = SchemaFileConfig()

        # Ensure static node types are present
        self._ensure_static_types()

    def _ensure_static_types(self) -> None:
        """Ensure that static node types (SavedView, etc.) are always present."""
        for type_name, type_config in STATIC_NODE_TYPES.items():
            if type_name not in self._config.schema_.node_types:
                self._config.schema_.node_types[type_name] = NodeTypeConfig(**type_config)
            else:
                # Mark as static even if defined in config
                self._config.schema_.node_types[type_name].static = True

    def reload(self) -> None:
        """Reload the configuration from disk."""
        self._config = None
        self._load_config()

    @property
    def config(self) -> SchemaFileConfig:
        """Get the full configuration."""
        return self._config

    @property
    def config_path(self) -> str:
        """Get the path to the loaded config file."""
        return self._config_path


# Module-level singleton instance
_loader: Optional[ConfigLoader] = None


def _get_loader() -> ConfigLoader:
    """Get or create the ConfigLoader singleton."""
    global _loader
    if _loader is None:
        _loader = ConfigLoader()
    return _loader


def get_schema() -> Dict[str, Any]:
    """
    Get the schema configuration.

    Returns a dict with:
    - node_types: Dict of node type name -> config
    - relationship_types: Dict of relationship type name -> config
    """
    loader = _get_loader()
    schema = loader.config.schema_

    return {
        "node_types": {
            name: {
                "fields": cfg.fields,
                "static": cfg.static,
                "category": cfg.category,
                "description": cfg.description,
                "color": cfg.color
            }
            for name, cfg in schema.node_types.items()
        },
        "relationship_types": {
            name: {
                "description": cfg.description
            }
            for name, cfg in schema.relationship_types.items()
        }
    }


def get_presentation() -> Dict[str, Any]:
    """
    Get the presentation configuration.

    Returns a dict with:
    - title: Application title
    - introduction: Welcome text
    - colors: Dict of node type -> color
    - prompt_prefix: Prefix for LLM system prompt
    - prompt_suffix: Suffix for LLM system prompt
    - default_language: Default language code
    """
    loader = _get_loader()
    pres = loader.config.presentation

    # Build colors from presentation or fallback to schema
    colors = dict(pres.colors)
    schema = loader.config.schema_
    for name, cfg in schema.node_types.items():
        if name not in colors:
            colors[name] = cfg.color

    return {
        "title": pres.title,
        "introduction": pres.introduction,
        "colors": colors,
        "prompt_prefix": pres.prompt_prefix,
        "prompt_suffix": pres.prompt_suffix,
        "default_language": pres.default_language,
        "widget_url": pres.widget_url
    }


def get_node_type_names() -> List[str]:
    """Get list of all node type names."""
    loader = _get_loader()
    return list(loader.config.schema_.node_types.keys())


def get_relationship_type_names() -> List[str]:
    """Get list of all relationship type names."""
    loader = _get_loader()
    return list(loader.config.schema_.relationship_types.keys())


def get_node_color(node_type: str) -> str:
    """Get the color for a specific node type."""
    loader = _get_loader()
    schema = loader.config.schema_
    pres = loader.config.presentation

    # Check presentation colors first
    if node_type in pres.colors:
        return pres.colors[node_type]

    # Fall back to schema-defined color
    if node_type in schema.node_types:
        return schema.node_types[node_type].color

    # Default gray
    return "#9CA3AF"


def get_config_path() -> str:
    """Get the path to the loaded configuration file."""
    loader = _get_loader()
    return loader.config_path


def reload_config() -> None:
    """Reload the configuration from disk."""
    loader = _get_loader()
    loader.reload()


def reset_loader() -> None:
    """Reset the loader (for testing purposes)."""
    global _loader
    _loader = None
    ConfigLoader.reset_instance()
