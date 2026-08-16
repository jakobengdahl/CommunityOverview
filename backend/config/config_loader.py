"""
Configuration loader for the Community Knowledge Graph.

This module loads and validates the schema configuration from a JSON file.
The configuration defines:
- Node types with their fields and colors
- Relationship types
- Presentation settings (colors, prompts, introduction text)

The config file path can be set via SCHEMA_FILE environment variable,
defaulting to config/default/schema_config.json.
"""

import os
import re
import json
import logging
from typing import Dict, List, Optional, Any
from pydantic import BaseModel, ConfigDict, Field, field_validator

from backend.runtime.config_context import (
    resolve_federation_config_path_info,
    resolve_schema_config_path_info,
)
from backend.runtime.request_context import (
    get_public_request_actor_context,
    get_public_request_graph_selection_context,
    get_public_request_scope_context,
)
from backend.config.model_profiles import ModelProfile, ModelProfilesConfig

logger = logging.getLogger(__name__)

# Default config path relative to project root
DEFAULT_CONFIG_PATH = "config/default/schema_config.json"

# System node types managed entirely by code.
# These are injected at startup and must never be defined in schema_config.json.
# Use system.disabled_node_types in schema_config.json to opt out of individual types.
SYSTEM_NODE_TYPES = {
    "SavedView": {
        "fields": ["name", "description", "summary", "metadata"],
        "static": True,
        "category": "system",
        "description": "Saved graph view snapshots for quick navigation",
        "color": "#6B7280",
        "icon": "BookmarkFill",
    },
    "VisualizationView": {
        "fields": ["name", "description", "summary", "metadata"],
        "static": True,
        "category": "system",
        "description": "Saved graph view snapshots (legacy)",
        "color": "#6B7280",
        "icon": "BookmarkFill",
    },
    "EventSubscription": {
        "fields": ["name", "description", "summary", "metadata"],
        "static": True,
        "category": "system",
        "description": (
            "Webhook subscription for graph mutation events. "
            "Configuration stored in metadata: filters (entity_kind, node_types, "
            "operations, keywords) and delivery (webhook_url, ignore_origins, ignore_session_ids)"
        ),
        "color": "#8B5CF6",
        "icon": "BellFill",
    },
    "Agent": {
        "fields": ["name", "description", "summary", "metadata"],
        "static": True,
        "category": "system",
        "description": (
            "AI agent configuration. Links to EventSubscription via metadata.subscription_id. "
            "Agent config in metadata: enabled, subscription_id, mcp_integration_ids, "
            "prompts, skills_urls, skill_node_ids."
        ),
        "color": "#EC4899",
        "icon": "CpuFill",
    },
    "Skill": {
        "fields": ["name", "description", "summary", "metadata"],
        "static": True,
        "category": "system",
        "ui_form": "skill",
        "description": (
            "Reusable AI skill definition (SKILL.md-compatible). "
            "metadata: {source_url, content, allowed_tools, when_to_use, version, effort}. "
            "Can be linked to Agent nodes via USES_SKILL edges."
        ),
        "color": "#8B5CF6",
        "icon": "StarFill",
    },
    "ActiveKnowledgeCollection": {
        "fields": ["name", "description", "summary", "tags"],
        "static": True,
        "category": "system",
        "description": (
            "Active knowledge collection configuration - enables structured data gathering "
            "through a special AI-assistant kiosk."
        ),
        "color": "#F59E0B",
        "icon": "FunnelFill",
    },
    "CollectionResponse": {
        "fields": ["name", "description", "summary", "metadata"],
        "static": True,
        "category": "system",
        "description": (
            "A single structured submission gathered by an ActiveKnowledgeCollection. "
            "metadata: {collection_short_name, collection_id, answers[], submitted_at}. "
            "answers is a flat list of {field_id, label, type, value} so responses can be "
            "aggregated into simple statistics across a collection."
        ),
        "color": "#F59E0B",
        "icon": "ClipboardCheckFill",
    },
}


class NodeTypeConfig(BaseModel):
    """Configuration for a single node type."""

    fields: List[str] = Field(
        default_factory=lambda: ["name", "description", "summary"]
    )
    static: bool = False
    category: str = "domain"  # "domain" = configurable, "system" = foundational
    description: str = ""
    color: str = "#9CA3AF"  # Default gray
    icon: str = ""  # Bootstrap Icon name (e.g. "PersonFill", "DatabaseFill")
    ui_form: str = ""  # Custom form variant, e.g. "skill" opens CreateSkillDialog
    labels: Dict[str, str] = Field(
        default_factory=dict
    )  # Localized names, e.g. {"sv": "Mål"}
    context_menu: List[Dict[str, Any]] = Field(
        default_factory=list
    )  # Extra right-click menu items


class RelationshipTypeConfig(BaseModel):
    """Configuration for a single relationship type."""

    description: str = ""


class SchemaConfig(BaseModel):
    """Schema configuration including node and relationship types."""

    node_types: Dict[str, NodeTypeConfig] = Field(default_factory=dict)
    relationship_types: Dict[str, RelationshipTypeConfig] = Field(default_factory=dict)

    @field_validator("node_types", mode="before")
    @classmethod
    def convert_node_types(cls, v):
        """Convert raw dict values to NodeTypeConfig."""
        if isinstance(v, dict):
            return {
                k: NodeTypeConfig(**val) if isinstance(val, dict) else val
                for k, val in v.items()
            }
        return v

    @field_validator("relationship_types", mode="before")
    @classmethod
    def convert_relationship_types(cls, v):
        """Convert raw dict values to RelationshipTypeConfig."""
        if isinstance(v, dict):
            return {
                k: RelationshipTypeConfig(**val) if isinstance(val, dict) else val
                for k, val in v.items()
            }
        return v


class ExpertAgentConfig(BaseModel):
    """Configuration for an expert agent available in the chat."""

    id: str
    name: str
    name_en: str = ""
    specialty: str = ""
    specialty_en: str = ""
    color: str = "#9CA3AF"
    icon: str = "CpuFill"
    intro_sv: str = ""
    intro_en: str = ""
    system_context: str = ""
    # URLs to SKILL.md files or GitHub repos.
    # Loaded and injected into system_context at startup — wired in server.py (TODO: Phase 3).
    skills_urls: List[str] = Field(default_factory=list)


from backend.skills.loader import SkillsConfig  # noqa: E402,F401 — mid-module import breaks a cycle; re-exported for callers


class LanguagePolicyConfig(BaseModel):
    """Per-graph language policy for graph content."""

    mode: str = "preferred"
    primary_language: str = "en"
    allowed_languages: List[str] = Field(default_factory=lambda: ["en", "sv"])
    description_sv: str = "Engelska är huvudspråk i grafen. Svenska accepteras när det är naturligt eller etablerat."
    description_en: str = "English is the primary graph language. Swedish is accepted when natural or established."


class GuideStepConfig(BaseModel):
    """A single step in an interactive guide."""

    type: str = "tooltip"  # tooltip | input | <action-type>
    target: str = "center"  # toolbar | chat | search | header | canvas | center
    target_position: str = "auto"  # auto | left | right | above | below
    text: str = ""
    text_sv: str = ""
    # For input steps
    input_label: str = ""
    input_label_sv: str = ""
    input_placeholder: str = ""
    input_placeholder_sv: str = ""
    store_as: str = ""  # key to store collected input under
    # For action steps
    action: str = ""  # explicit override; defaults to 'type' when type is actionable
    # Node actions (create_node, update_node, delete_node, show_node_detail, focus_node)
    node_type: str = ""
    node_id: str = ""
    node_data: Dict[str, Any] = Field(default_factory=dict)
    # Search / fill actions
    query: str = ""
    fill_text: str = ""  # alias for query in fill_chat_input / fill_search_input
    animated: bool = True  # animate typing for fill_* actions
    auto_send: bool = False  # auto-send after fill_chat_input animation
    # Edge actions (create_edge, delete_edge)
    source_id: str = ""
    target_id: str = ""
    edge_id: str = ""
    edge_type: str = ""
    edge_label: str = ""
    # Saved view action
    view_name: str = ""


class GuideConfig(BaseModel):
    """An interactive guide definition."""

    id: str
    name: str = ""
    name_sv: str = ""
    steps: List[GuideStepConfig] = Field(default_factory=list)


class CapabilityConfig(BaseModel):
    """Public capability metadata exposed for client discovery."""

    id: str
    name: str
    description: str = ""
    enabled: bool = True


class RuntimeMetadataConfig(BaseModel):
    """Public runtime metadata exposed for deployment introspection."""

    runtime_mode: str = "standalone"
    enabled_extensions: List[str] = Field(default_factory=list)


class RestInterfaceFilterConfig(BaseModel):
    """Tag/subtype filter applied to a custom REST interface.

    Semantics (all independent, combined with AND across the three fields):
    - ``tags_all``: the entity must carry *every* listed tag (AND).
    - ``tags_any``: the entity must carry *at least one* listed tag (OR).
    - ``subtypes_any``: the node must carry at least one listed subtype (OR).
      Ignored for edge interfaces (edges have no subtypes).

    An empty list disables that dimension. All empty = no filtering.
    """

    tags_all: List[str] = Field(default_factory=list)
    tags_any: List[str] = Field(default_factory=list)
    subtypes_any: List[str] = Field(default_factory=list)


class RestInterfaceConfig(BaseModel):
    """A single config-driven dedicated REST interface for one node/edge type.

    Exposes one node type (or edge type) at its own GET endpoint, bypassing the
    generic node/edge REST interface, with optional tag/subtype filters. The
    endpoint honours the same read authorization and graph-scope narrowing as
    the generic interface — it never returns more than a generic search would.

    ``path`` is the URL segment appended to the router prefix (e.g. ``actors``
    served at ``/api/actors``). It is normalised (leading/trailing slashes
    stripped) and validated against a conservative pattern.
    """

    path: str
    entity: str = "node"  # "node" | "edge"
    node_type: str = ""
    edge_type: str = ""
    enabled: bool = True
    limit: int = Field(default=500, ge=1, le=5000)
    filters: RestInterfaceFilterConfig = Field(
        default_factory=RestInterfaceFilterConfig
    )

    @field_validator("path")
    @classmethod
    def _validate_path(cls, v: str) -> str:
        normalized = v.strip().strip("/")
        if not normalized:
            raise ValueError("rest_interfaces[].path must be a non-empty URL segment")
        if not re.fullmatch(r"[a-z0-9][a-z0-9/_-]*", normalized):
            raise ValueError(
                "rest_interfaces[].path must be lowercase alphanumeric with "
                "'-', '_' or '/' separators (e.g. 'actors' or 'people/actors'): "
                f"got {v!r}"
            )
        return normalized

    @field_validator("entity")
    @classmethod
    def _validate_entity(cls, v: str) -> str:
        entity = (v or "").strip().lower()
        if entity not in {"node", "edge"}:
            raise ValueError(
                f"rest_interfaces[].entity must be 'node' or 'edge', got {v!r}"
            )
        return entity


class PresentationConfig(BaseModel):
    """Presentation configuration for UI and prompts."""

    title: str = "Community Knowledge Graph"
    introduction: str = "Welcome to the knowledge graph."
    colors: Dict[str, str] = Field(default_factory=dict)
    prompt_prefix: str = ""
    prompt_suffix: str = ""
    default_language: str = "en"
    language_policy: LanguagePolicyConfig = Field(default_factory=LanguagePolicyConfig)
    widget_url: str = ""  # URL template for the graph widget
    expert_agents: List[ExpertAgentConfig] = Field(default_factory=list)
    skills_config: SkillsConfig = Field(default_factory=SkillsConfig)
    capabilities: List[CapabilityConfig] = Field(default_factory=list)
    guides: List[GuideConfig] = Field(default_factory=list)


class SystemConfig(BaseModel):
    """System-level toggles for built-in node types managed by code."""

    disabled_node_types: List[str] = Field(default_factory=list)


class SchemaFileConfig(BaseModel):
    """Root configuration model for the schema file."""

    schema_: SchemaConfig = Field(alias="schema", default_factory=SchemaConfig)
    presentation: PresentationConfig = Field(default_factory=PresentationConfig)
    runtime: RuntimeMetadataConfig = Field(default_factory=RuntimeMetadataConfig)
    system: SystemConfig = Field(default_factory=SystemConfig)
    # Config-driven dedicated REST interfaces per node/edge type (open core).
    # Empty by default — only the generic node/edge REST interface is exposed.
    rest_interfaces: List[RestInterfaceConfig] = Field(default_factory=list)
    # Named LLM/model profiles across providers (see docs/PROFILES.md). Empty
    # by default — that is the legacy single-provider mode (LLM_PROVIDER /
    # LLM_MODEL / OPENAI_API_KEY / ANTHROPIC_API_KEY environment variables).
    model_profiles: ModelProfilesConfig = Field(default_factory=ModelProfilesConfig)

    model_config = ConfigDict(populate_by_name=True)


class ConfigLoader:
    """
    Singleton configuration loader.

    Loads the schema configuration once and provides access to
    schema and presentation settings.
    """

    _instance: Optional["ConfigLoader"] = None

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
        return resolve_schema_config_path_info(DEFAULT_CONFIG_PATH)["path"]

    def _load_config(self) -> None:
        """Load and validate the configuration file."""
        self._config_path = self._get_config_path()

        try:
            with open(self._config_path, "r", encoding="utf-8") as f:
                raw_config = json.load(f)

            self._sanitize_rest_interfaces(raw_config)
            self._config = SchemaFileConfig(**raw_config)
            logger.info(f"Loaded schema configuration from: {self._config_path}")

        except FileNotFoundError:
            logger.warning(
                f"Config file not found at {self._config_path}, using defaults"
            )
            self._config = SchemaFileConfig()

        except json.JSONDecodeError as e:
            logger.warning(f"Invalid JSON in config file: {e}, using defaults")
            self._config = SchemaFileConfig()

        except Exception as e:
            logger.warning(f"Error loading config: {e}, using defaults")
            self._config = SchemaFileConfig()

        # Strip system node types that may be defined in the config file (backward compat).
        # System types are now managed entirely in code via SYSTEM_NODE_TYPES.
        self._strip_system_types_from_config()
        self._apply_system_types()

    @staticmethod
    def _sanitize_rest_interfaces(raw_config: Dict[str, Any]) -> None:
        """Drop malformed ``rest_interfaces`` entries in place before validation.

        ``rest_interfaces`` entries carry strict validators (path/entity). Left in
        the raw document, a single malformed entry would fail the whole
        ``SchemaFileConfig`` construction and trip the loader's global fallback to
        empty defaults — silently reverting every node type, relationship, profile,
        etc. Validating each entry independently here and discarding only the bad
        ones keeps one typo from nuking the entire config, matching the per-entry
        graceful skipping the router already does for missing node_type/edge_type.
        """
        raw = raw_config.get("rest_interfaces")
        if raw is None:
            return
        if not isinstance(raw, list):
            logger.warning(
                "rest_interfaces must be a list, got %s — ignoring", type(raw).__name__
            )
            raw_config["rest_interfaces"] = []
            return
        valid: List[Dict[str, Any]] = []
        for index, entry in enumerate(raw):
            try:
                RestInterfaceConfig(**entry)
            except Exception as exc:
                logger.warning(
                    "Skipping invalid rest_interfaces[%d] (%r): %s", index, entry, exc
                )
                continue
            valid.append(entry)
        raw_config["rest_interfaces"] = valid

    def _strip_system_types_from_config(self) -> None:
        """Remove any system node types found in the loaded config (backward compat)."""
        to_remove = set(self._config.schema_.node_types.keys()) & set(
            SYSTEM_NODE_TYPES.keys()
        )
        for name in to_remove:
            del self._config.schema_.node_types[name]
        if to_remove:
            logger.warning(
                "System node types found in schema_config.json and removed "
                "(they are now managed by code — remove them from your config file): %s",
                sorted(to_remove),
            )

    def _apply_system_types(self) -> None:
        """Inject system node types from code, respecting system.disabled_node_types."""
        disabled = set(self._config.system.disabled_node_types)
        unknown = disabled - set(SYSTEM_NODE_TYPES.keys())
        if unknown:
            logger.warning(
                "system.disabled_node_types contains unrecognised names (typo?): %s. "
                "Valid names: %s",
                sorted(unknown),
                sorted(SYSTEM_NODE_TYPES.keys()),
            )
        for type_name, type_config in SYSTEM_NODE_TYPES.items():
            if type_name in disabled:
                logger.info(
                    "System node type '%s' disabled via system.disabled_node_types",
                    type_name,
                )
                continue
            self._config.schema_.node_types[type_name] = NodeTypeConfig(**type_config)

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
                "color": cfg.color,
                "icon": cfg.icon,
                "ui_form": cfg.ui_form,
                "labels": cfg.labels,
                "context_menu": cfg.context_menu,
            }
            for name, cfg in schema.node_types.items()
        },
        "relationship_types": {
            name: {"description": cfg.description}
            for name, cfg in schema.relationship_types.items()
        },
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
        "language_policy": pres.language_policy.model_dump(),
        "widget_url": pres.widget_url,
        "expert_agents": [agent.model_dump() for agent in pres.expert_agents],
        "capabilities": [capability.model_dump() for capability in pres.capabilities],
        "guides": [guide.model_dump() for guide in pres.guides],
    }


# Capability every deployment reports whether or not its config declares one, so
# an agent can always ask whether this instance's canvas actually tweens an
# `apply_visualization_layout` animation hint instead of snapping to the targets.
# The shipped canvas does, hence the default; a deployment running an older
# frontend declares the same id with "enabled": false in its presentation config
# to say otherwise.
_ANIMATED_LAYOUT_CAPABILITY = CapabilityConfig(
    id="animated_layout",
    name="Animated layout",
    description=(
        "The canvas tweens an apply_visualization_layout animation hint "
        "(animate/duration_ms/easing) instead of applying the move immediately. "
        "A viewer who asked for reduced motion still snaps to the final "
        "positions — that is a per-viewer client-side decision this flag cannot "
        "report."
    ),
    enabled=True,
)


def get_capabilities() -> Dict[str, Any]:
    """Get the public capability manifest for client discovery.

    Deployment-declared capabilities come first, in config order; a server-known
    default (see ``_ANIMATED_LAYOUT_CAPABILITY``) is appended only when the
    config does not already declare that id, so a deployment always keeps the
    last word on its own capabilities.
    """
    loader = _get_loader()
    capabilities = [
        capability.model_dump()
        for capability in loader.config.presentation.capabilities
    ]
    declared_ids = {capability.get("id") for capability in capabilities}
    if _ANIMATED_LAYOUT_CAPABILITY.id not in declared_ids:
        capabilities.append(_ANIMATED_LAYOUT_CAPABILITY.model_dump())
    return {"capabilities": capabilities}


def _normalize_runtime_mode(runtime_mode: Optional[str]) -> str:
    """Normalize runtime mode to a supported public value."""
    normalized = (runtime_mode or "").strip().lower()
    if normalized in {"standalone", "hosted"}:
        return normalized
    return "standalone"


def _parse_enabled_extensions(raw_value: Optional[str]) -> List[str]:
    """Parse comma-separated extension identifiers from environment input."""
    if not raw_value:
        return []

    extensions: List[str] = []
    for value in raw_value.split(","):
        identifier = value.strip()
        if identifier and identifier not in extensions:
            extensions.append(identifier)
    return extensions


def get_runtime_info() -> Dict[str, Any]:
    """Get the public runtime metadata for deployment introspection."""
    loader = _get_loader()
    runtime = loader.config.runtime

    runtime_mode = _normalize_runtime_mode(runtime.runtime_mode)
    env_runtime_mode = os.getenv("COMMUNITYOVERVIEW_RUNTIME_MODE")
    if env_runtime_mode is not None:
        runtime_mode = _normalize_runtime_mode(env_runtime_mode)

    enabled_extensions = list(runtime.enabled_extensions)
    env_enabled_extensions = os.getenv("COMMUNITYOVERVIEW_ENABLED_EXTENSIONS")
    if env_enabled_extensions is not None:
        enabled_extensions = _parse_enabled_extensions(env_enabled_extensions)

    return {
        "runtime_mode": runtime_mode,
        "enabled_extensions": enabled_extensions,
    }


def get_tenant_context() -> Dict[str, Any]:
    """Get the tenant/deployment context metadata.

    Values are read from environment variables with safe standalone defaults
    when the variables are unset.

    Env vars:
        COMMUNITYOVERVIEW_TENANT_ID   - Unique tenant identifier
        COMMUNITYOVERVIEW_TENANT_NAME - Human-readable tenant name
        COMMUNITYOVERVIEW_ENVIRONMENT - Deployment environment (e.g. local, staging, production)
    """
    return {
        "tenant_id": os.getenv("COMMUNITYOVERVIEW_TENANT_ID", ""),
        "tenant_name": os.getenv("COMMUNITYOVERVIEW_TENANT_NAME", ""),
        "environment": os.getenv("COMMUNITYOVERVIEW_ENVIRONMENT", "local"),
    }


PUBLIC_BASE_URL_ENV = "COMMUNITYOVERVIEW_PUBLIC_BASE_URL"


def get_public_base_url() -> str:
    """Externally reachable base URL used to build shareable links, or "".

    Set per environment/tenant in hosted deployments (scheme + host + optional
    base path). Unset in standalone/local use, in which case link builders
    return ``None`` rather than emitting a guessed or ``localhost`` URL.
    """
    return os.getenv(PUBLIC_BASE_URL_ENV, "").strip()


def build_session_url(session_id: str) -> Optional[str]:
    """Canonical direct link to a visualization session, or ``None``.

    Keeps the established ``?session=<id>`` form the frontend reads and reflects,
    with the server owning the base URL so an assistant never guesses a host
    (see ``docs/MCP_SESSION_LIFECYCLE_CONTRACT.md`` §5). Returns ``None`` when no
    public base URL is configured.
    """
    from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

    base = get_public_base_url()
    if not base or not session_id:
        return None
    parts = urlsplit(base)
    query = dict(parse_qsl(parts.query))
    query["session"] = session_id
    # Preserve any base path; ensure a "/" path when the base is a bare origin so
    # the result is "https://host/?session=<id>" rather than "https://host?...".
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path or "/", urlencode(query), "")
    )


def _get_resolved_config_context() -> Dict[str, Any]:
    """Get internal config resolution details, including resolved filesystem paths."""
    schema_context = resolve_schema_config_path_info(DEFAULT_CONFIG_PATH)
    federation_context = resolve_federation_config_path_info(
        "config/default/federation_config.json"
    )

    return {
        **get_tenant_context(),
        "tenant_config_dir": schema_context["tenant_config_dir"],
        "schema_config_path": schema_context["path"],
        "schema_config_source": schema_context["source"],
        "federation_config_path": federation_context["path"],
        "federation_config_source": federation_context["source"],
    }


def get_config_context() -> Dict[str, Any]:
    """Get the effective public config scope without exposing filesystem paths."""
    resolved_context = _get_resolved_config_context()
    return {
        "tenant_id": resolved_context["tenant_id"],
        "tenant_name": resolved_context["tenant_name"],
        "environment": resolved_context["environment"],
        "tenant_config_dir_configured": bool(resolved_context["tenant_config_dir"]),
        "schema_config_source": resolved_context["schema_config_source"],
        "federation_config_source": resolved_context["federation_config_source"],
    }


def get_request_actor_info() -> Dict[str, Any]:
    """Get the default public request actor context for this deployment."""
    return get_public_request_actor_context()


def get_request_scope_info() -> Dict[str, Any]:
    """Get the default public request scope context for this deployment."""
    return get_public_request_scope_context()


def get_request_graph_selection_info() -> Dict[str, Any]:
    """Get the default public graph/workspace selection summary for this deployment."""
    return get_public_request_graph_selection_context()


def get_rest_interfaces() -> List[RestInterfaceConfig]:
    """Get the configured custom REST interfaces (open core).

    Returns an empty list when none are configured, in which case only the
    generic node/edge REST interface is exposed.
    """
    loader = _get_loader()
    return list(loader.config.rest_interfaces)


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


def get_skills_config() -> SkillsConfig:
    """Get the SkillsConfig from the presentation section."""
    loader = _get_loader()
    return loader.config.presentation.skills_config


def get_expert_agent_configs() -> "List[ExpertAgentConfig]":
    """Get the list of ExpertAgentConfig objects from the presentation section."""
    loader = _get_loader()
    return loader.config.presentation.expert_agents


def get_model_profiles() -> List[ModelProfile]:
    """
    Get all configured model profiles (enabled and disabled), in file order.

    An empty list means no model profiles are configured — callers should fall
    back to the legacy single-provider environment configuration.
    """
    loader = _get_loader()
    return list(loader.config.model_profiles.profiles)


def get_model_profile_selection_enabled() -> bool:
    """Whether the chat UI may select a model profile other than the default."""
    loader = _get_loader()
    return loader.config.model_profiles.selection_enabled


def get_model_profiles_public() -> Dict[str, Any]:
    """
    Get the public (non-secret) view of model profile configuration.

    Only enabled profiles are exposed — disabled profiles are an
    implementation/config detail, not a user-facing choice. credential_ref,
    endpoint and options are omitted; they are server-side resolution details,
    not needed by clients.
    """
    from backend.config.model_profiles import get_default_profile, get_enabled_profiles

    loader = _get_loader()
    profiles = loader.config.model_profiles.profiles
    default_profile = get_default_profile(profiles)

    return {
        "selection_enabled": loader.config.model_profiles.selection_enabled,
        "default_profile_id": default_profile.id if default_profile else None,
        "profiles": [
            {
                "id": p.id,
                "name": p.name,
                "provider": p.provider,
                "model": p.model,
                "default": p.default,
            }
            for p in get_enabled_profiles(profiles)
        ],
    }
