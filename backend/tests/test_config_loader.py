"""
Tests for config_loader module.

Tests loading and validation of schema configuration from JSON files.
"""

import os
import json
import tempfile
import pytest
from pathlib import Path


class TestConfigLoader:
    """Test suite for config_loader functionality."""

    @pytest.fixture(autouse=True)
    def reset_loader(self):
        """Reset the config loader before each test."""
        # Import here to reset the module state
        from backend.config import config_loader

        config_loader.reset_loader()
        yield
        os.environ.pop("SCHEMA_FILE", None)
        os.environ.pop("COMMUNITYOVERVIEW_RUNTIME_MODE", None)
        os.environ.pop("COMMUNITYOVERVIEW_ENABLED_EXTENSIONS", None)
        os.environ.pop("COMMUNITYOVERVIEW_ACTOR_ID", None)
        os.environ.pop("COMMUNITYOVERVIEW_ACTOR_TYPE", None)
        os.environ.pop("COMMUNITYOVERVIEW_AUTH_SOURCE", None)
        os.environ.pop("COMMUNITYOVERVIEW_WORKSPACE_ID", None)
        os.environ.pop("COMMUNITYOVERVIEW_WORKSPACE_KIND", None)
        os.environ.pop("COMMUNITYOVERVIEW_GRAPH_SCOPE_ID", None)
        config_loader.reset_loader()

    def test_load_default_config(self):
        """Test loading the default configuration file."""
        from backend.config import config_loader

        schema = config_loader.get_schema()

        # Check that schema has node_types and relationship_types
        assert "node_types" in schema
        assert "relationship_types" in schema

        # All six system node types must be present regardless of config file content
        for system_type in config_loader.SYSTEM_NODE_TYPES:
            assert system_type in schema["node_types"], (
                f"System type '{system_type}' missing from schema"
            )
            assert schema["node_types"][system_type]["static"] is True
            assert schema["node_types"][system_type]["category"] == "system"

    def test_all_system_node_types_injected_from_code(self):
        """System node types must come from code, not from config."""
        from backend.config import config_loader

        schema = config_loader.get_schema()
        for type_name, type_config in config_loader.SYSTEM_NODE_TYPES.items():
            assert type_name in schema["node_types"]
            loaded = schema["node_types"][type_name]
            assert loaded["color"] == type_config["color"]
            assert loaded["icon"] == type_config["icon"]

    def test_system_node_types_in_config_are_stripped_not_doubled(self, tmp_path):
        """System node types defined in a config file must not produce duplicates."""
        import json
        from backend.config import config_loader

        config_with_system_types = {
            "schema": {
                "node_types": {
                    "SavedView": {
                        "fields": ["name"],
                        "static": True,
                        "category": "system",
                        "description": "old description",
                        "color": "#FFFFFF",
                        "icon": "OldIcon",
                    },
                    "Agent": {
                        "fields": ["name"],
                        "static": False,
                        "category": "system",
                        "description": "old agent",
                        "color": "#FFFFFF",
                        "icon": "OldIcon",
                    },
                    "MyDomain": {
                        "fields": ["name", "description"],
                        "category": "domain",
                        "description": "A domain type",
                        "color": "#123456",
                    },
                }
            }
        }
        config_file = tmp_path / "schema_config.json"
        config_file.write_text(json.dumps(config_with_system_types), encoding="utf-8")
        os.environ["SCHEMA_FILE"] = str(config_file)
        config_loader.reset_loader()

        schema = config_loader.get_schema()

        # Domain type must be present
        assert "MyDomain" in schema["node_types"]
        # System types must come from code, not from the stale config entries
        assert (
            schema["node_types"]["SavedView"]["color"]
            == config_loader.SYSTEM_NODE_TYPES["SavedView"]["color"]
        )
        assert (
            schema["node_types"]["Agent"]["color"]
            == config_loader.SYSTEM_NODE_TYPES["Agent"]["color"]
        )
        # No duplicates — each type appears exactly once
        type_names = list(schema["node_types"].keys())
        assert type_names.count("SavedView") == 1
        assert type_names.count("Agent") == 1

        del os.environ["SCHEMA_FILE"]

    def test_disabled_system_node_type_is_excluded(self, tmp_path):
        """A system node type listed in system.disabled_node_types must not appear in schema."""
        import json
        from backend.config import config_loader

        config = {
            "system": {"disabled_node_types": ["Agent", "Skill"]},
            "schema": {"node_types": {}},
        }
        config_file = tmp_path / "schema_config.json"
        config_file.write_text(json.dumps(config), encoding="utf-8")
        os.environ["SCHEMA_FILE"] = str(config_file)
        config_loader.reset_loader()

        schema = config_loader.get_schema()

        assert "Agent" not in schema["node_types"]
        assert "Skill" not in schema["node_types"]
        # Other system types must still be present
        assert "SavedView" in schema["node_types"]
        assert "EventSubscription" in schema["node_types"]

        del os.environ["SCHEMA_FILE"]

    def test_load_custom_config(self):
        """Test loading a custom configuration file."""
        from backend.config import config_loader

        # Set custom config path
        test_config_path = str(
            Path(__file__).parent.parent.parent
            / "config"
            / "test"
            / "schema_config.json"
        )
        os.environ["SCHEMA_FILE"] = test_config_path

        # Reset and reload
        config_loader.reset_loader()

        schema = config_loader.get_schema()

        # Check custom types are present
        assert "CustomActor" in schema["node_types"]
        assert "TestNode" in schema["node_types"]

        # Check static types are still present (always added)
        assert "SavedView" in schema["node_types"]

        # Check custom relationship types
        assert "CUSTOM_RELATION" in schema["relationship_types"]

        # Clean up
        del os.environ["SCHEMA_FILE"]

    def test_get_presentation(self):
        """Test getting presentation configuration."""
        from backend.config import config_loader

        presentation = config_loader.get_presentation()

        # Check presentation has expected fields
        assert "title" in presentation
        assert "introduction" in presentation
        assert "colors" in presentation
        assert "prompt_prefix" in presentation
        assert "prompt_suffix" in presentation
        assert "default_language" in presentation
        assert "default_chat_collapsed" in presentation
        assert "language_policy" in presentation

    def test_default_chat_collapsed_defaults_false(self):
        """A config without an explicit setting keeps the assistant panel open."""
        from backend.config import config_loader

        presentation = config_loader.get_presentation()

        assert presentation["default_chat_collapsed"] is False

    def test_custom_presentation(self):
        """Test presentation from custom config."""
        from backend.config import config_loader

        # Set custom config path
        test_config_path = str(
            Path(__file__).parent.parent.parent
            / "config"
            / "test"
            / "schema_config.json"
        )
        os.environ["SCHEMA_FILE"] = test_config_path

        config_loader.reset_loader()

        presentation = config_loader.get_presentation()

        assert presentation["title"] == "Test Knowledge Graph"
        assert presentation["introduction"] == "This is a test instance."
        assert presentation["prompt_prefix"] == "You are a test assistant."
        assert presentation["language_policy"]["mode"] == "required"
        assert presentation["language_policy"]["primary_language"] == "en"
        assert presentation["language_policy"]["allowed_languages"] == ["en"]
        assert presentation["capabilities"] == [
            {
                "id": "graph_export",
                "name": "Graph export",
                "description": "Allows clients to export graph data for offline analysis.",
                "enabled": True,
            },
            {
                "id": "assistant_guidance",
                "name": "Assistant guidance",
                "description": "Provides configuration for guided assistant interactions.",
                "enabled": False,
            },
        ]

        del os.environ["SCHEMA_FILE"]

    def test_get_capabilities_always_reports_animated_layout(self):
        """A deployment that declares nothing still answers the animation question.

        An agent sending an ``apply_visualization_layout`` animation hint cannot
        otherwise tell a tween from a snap, so the flag must be present whether
        or not a deployment configured a manifest.
        """
        from backend.config import config_loader

        capabilities = config_loader.get_capabilities()["capabilities"]

        assert [c["id"] for c in capabilities] == ["animated_layout"]
        assert capabilities[0]["enabled"] is True

    def test_get_capabilities_from_custom_config(self):
        """Test capability manifest is loaded from custom config."""
        from backend.config import config_loader

        test_config_path = str(
            Path(__file__).parent.parent.parent
            / "config"
            / "test"
            / "schema_config.json"
        )
        os.environ["SCHEMA_FILE"] = test_config_path
        config_loader.reset_loader()

        capabilities = config_loader.get_capabilities()["capabilities"]

        assert capabilities[:2] == [
            {
                "id": "graph_export",
                "name": "Graph export",
                "description": "Allows clients to export graph data for offline analysis.",
                "enabled": True,
            },
            {
                "id": "assistant_guidance",
                "name": "Assistant guidance",
                "description": "Provides configuration for guided assistant interactions.",
                "enabled": False,
            },
        ]
        assert [c["id"] for c in capabilities[2:]] == ["animated_layout"]

        del os.environ["SCHEMA_FILE"]

    def test_declared_animated_layout_capability_wins(self, tmp_path):
        """A deployment whose canvas does not tween says so, and is not overruled."""
        from backend.config import config_loader

        config_path = tmp_path / "schema_config.json"
        config_path.write_text(
            json.dumps(
                {
                    "node_types": {},
                    "relationship_types": {},
                    "presentation": {
                        "capabilities": [
                            {
                                "id": "animated_layout",
                                "name": "Animated layout",
                                "enabled": False,
                            }
                        ]
                    },
                }
            )
        )
        os.environ["SCHEMA_FILE"] = str(config_path)
        config_loader.reset_loader()

        capabilities = config_loader.get_capabilities()["capabilities"]

        assert [c["id"] for c in capabilities] == ["animated_layout"]
        assert capabilities[0]["enabled"] is False

    def test_get_runtime_info_defaults_to_standalone(self):
        """Test runtime metadata defaults to standalone mode with no extensions."""
        from backend.config import config_loader

        runtime_info = config_loader.get_runtime_info()

        assert runtime_info == {
            "runtime_mode": "standalone",
            "enabled_extensions": [],
        }

    def test_get_runtime_info_runtime_mode_env_override(self):
        """Test runtime mode can be overridden through environment configuration."""
        from backend.config import config_loader

        os.environ["COMMUNITYOVERVIEW_RUNTIME_MODE"] = "hosted"
        config_loader.reset_loader()

        runtime_info = config_loader.get_runtime_info()

        assert runtime_info == {
            "runtime_mode": "hosted",
            "enabled_extensions": [],
        }

    def test_get_runtime_info_enabled_extensions_env_override(self):
        """Test enabled extensions can be overridden through environment configuration."""
        from backend.config import config_loader

        os.environ["COMMUNITYOVERVIEW_RUNTIME_MODE"] = "hosted"
        os.environ["COMMUNITYOVERVIEW_ENABLED_EXTENSIONS"] = (
            "federation, analytics , federation,"
        )
        config_loader.reset_loader()

        runtime_info = config_loader.get_runtime_info()

        assert runtime_info == {
            "runtime_mode": "hosted",
            "enabled_extensions": ["federation", "analytics"],
        }

    def test_get_request_actor_info_defaults(self):
        """Request actor defaults remain anonymous and standalone-safe."""
        from backend.config import config_loader

        assert config_loader.get_request_actor_info() == {
            "actor_type": "",
            "is_authenticated": False,
            "auth_source": "anonymous",
            "has_actor": False,
            "source": "default",
        }

    def test_get_request_actor_info_env_override(self):
        """Request actor context can be populated by safe environment inputs."""
        from backend.config import config_loader

        os.environ["COMMUNITYOVERVIEW_ACTOR_ID"] = "env-actor"
        os.environ["COMMUNITYOVERVIEW_ACTOR_TYPE"] = "member"
        os.environ["COMMUNITYOVERVIEW_AUTH_SOURCE"] = "gateway"

        assert config_loader.get_request_actor_info() == {
            "actor_type": "member",
            "is_authenticated": True,
            "auth_source": "gateway",
            "has_actor": True,
            "source": "environment",
        }

    def test_get_request_scope_info_defaults(self):
        """Request scope defaults remain empty and standalone-safe."""
        from backend.config import config_loader

        assert config_loader.get_request_scope_info() == {
            "workspace_kind": "",
            "has_workspace": False,
            "has_graph": False,
            "has_selection": False,
            "selection_mode": "default",
            "selection_source": "default",
            "source": "default",
        }

    def test_get_request_scope_info_env_override(self):
        """Request scope context can be populated by safe environment inputs."""
        from backend.config import config_loader

        os.environ["COMMUNITYOVERVIEW_WORKSPACE_ID"] = "workspace-env"
        os.environ["COMMUNITYOVERVIEW_WORKSPACE_KIND"] = "personal"
        os.environ["COMMUNITYOVERVIEW_GRAPH_SCOPE_ID"] = "graph-env"

        assert config_loader.get_request_scope_info() == {
            "workspace_kind": "personal",
            "has_workspace": True,
            "has_graph": True,
            "has_selection": True,
            "selection_mode": "workspace_graph",
            "selection_source": "environment",
            "source": "environment",
        }

    def test_get_request_graph_selection_info_matches_public_scope_summary(self):
        """Selection summary reuses the same non-sensitive workspace/graph metadata."""
        from backend.config import config_loader

        os.environ["COMMUNITYOVERVIEW_WORKSPACE_ID"] = "workspace-env"
        os.environ["COMMUNITYOVERVIEW_WORKSPACE_KIND"] = "personal"
        os.environ["COMMUNITYOVERVIEW_GRAPH_SCOPE_ID"] = "graph-env"

        assert config_loader.get_request_graph_selection_info() == {
            "workspace_kind": "personal",
            "has_workspace": True,
            "has_graph": True,
            "has_selection": True,
            "selection_mode": "workspace_graph",
            "selection_source": "environment",
            "source": "environment",
        }

    def test_get_node_type_names(self):
        """Test getting list of node type names."""
        from backend.config import config_loader

        names = config_loader.get_node_type_names()

        assert isinstance(names, list)
        assert "SavedView" in names  # Static type always present

    def test_get_relationship_type_names(self):
        """Test getting list of relationship type names."""
        from backend.config import config_loader

        names = config_loader.get_relationship_type_names()

        assert isinstance(names, list)
        assert len(names) > 0

    def test_get_node_color(self):
        """Test getting color for a node type."""
        from backend.config import config_loader

        # SavedView should have gray color
        color = config_loader.get_node_color("SavedView")
        assert color == "#6B7280"

        # Unknown type should get default color
        color = config_loader.get_node_color("UnknownType")
        assert color == "#9CA3AF"

    def test_invalid_config_uses_defaults(self):
        """Test that invalid config file falls back to defaults."""
        from backend.config import config_loader

        # Create a temp file with invalid JSON
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("{ invalid json }")
            temp_path = f.name

        try:
            os.environ["SCHEMA_FILE"] = temp_path
            config_loader.reset_loader()

            # Should still work with defaults
            schema = config_loader.get_schema()
            assert "node_types" in schema
            # Static types should still be present
            assert "SavedView" in schema["node_types"]
        finally:
            os.unlink(temp_path)
            del os.environ["SCHEMA_FILE"]

    def test_missing_config_uses_defaults(self):
        """Test that missing config file falls back to defaults."""
        from backend.config import config_loader

        os.environ["SCHEMA_FILE"] = "/nonexistent/path/config.json"
        config_loader.reset_loader()

        # Should still work with defaults
        schema = config_loader.get_schema()
        assert "node_types" in schema
        assert "SavedView" in schema["node_types"]

        del os.environ["SCHEMA_FILE"]

    def test_config_path_getter(self):
        """Test getting the config file path."""
        from backend.config import config_loader

        path = config_loader.get_config_path()
        assert path is not None
        assert isinstance(path, str)


class TestSchemaIntegration:
    """Integration tests for schema with other backend components."""

    @pytest.fixture(autouse=True)
    def reset_loader(self):
        """Reset the config loader before each test."""
        from backend.config import config_loader

        config_loader.reset_loader()
        yield
        config_loader.reset_loader()

    def test_models_use_schema_types(self):
        """Test that models module uses schema for type validation."""
        pytest.importorskip("networkx")
        from backend.core import models

        # Get valid node types
        valid_types = models.get_node_type_names()
        assert len(valid_types) > 0

        # Check validation function works
        assert models.is_valid_node_type("SavedView") is True

    def test_service_returns_schema(self):
        """Test that GraphService returns schema correctly."""
        pytest.importorskip("networkx")
        import tempfile
        from backend.core import GraphStorage
        from backend.service import GraphService

        # Create temp graph file
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"nodes": [], "edges": []}, f)
            temp_path = f.name

        try:
            storage = GraphStorage(temp_path)
            service = GraphService(storage)

            schema = service.get_schema()

            assert "node_types" in schema
            assert "relationship_types" in schema
            assert "SavedView" in schema["node_types"]

            presentation = service.get_presentation()

            assert "title" in presentation
            assert "colors" in presentation

            capabilities = service.get_capabilities()
            assert [c["id"] for c in capabilities["capabilities"]] == [
                "animated_layout"
            ]
        finally:
            os.unlink(temp_path)

    def test_list_node_types_uses_config(self):
        """Test that list_node_types returns config-based types."""
        pytest.importorskip("networkx")
        import tempfile
        from backend.core import GraphStorage
        from backend.service import GraphService

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"nodes": [], "edges": []}, f)
            temp_path = f.name

        try:
            storage = GraphStorage(temp_path)
            service = GraphService(storage)

            result = service.list_node_types()

            assert "node_types" in result
            assert len(result["node_types"]) > 0

            # Check that each type has expected fields
            for node_type in result["node_types"]:
                assert "type" in node_type
                assert "color" in node_type
                assert "description" in node_type
        finally:
            os.unlink(temp_path)


class TestConfigWithAlternateFile:
    """Tests using alternate config files."""

    @pytest.fixture(autouse=True)
    def setup_and_cleanup(self):
        """Set up and clean up for alternate config tests."""
        from backend.config import config_loader

        config_loader.reset_loader()
        yield
        # Clean up env var
        if "SCHEMA_FILE" in os.environ:
            del os.environ["SCHEMA_FILE"]
        config_loader.reset_loader()

    def test_extra_node_type_in_custom_config(self):
        """Test that extra node types from custom config are available."""
        from backend.config import config_loader

        # Use test config with extra types
        test_config_path = str(
            Path(__file__).parent.parent.parent
            / "config"
            / "test"
            / "schema_config.json"
        )
        os.environ["SCHEMA_FILE"] = test_config_path
        config_loader.reset_loader()

        schema = config_loader.get_schema()

        # Custom types should be present
        assert "CustomActor" in schema["node_types"]
        assert schema["node_types"]["CustomActor"]["color"] == "#FF0000"
        assert (
            schema["node_types"]["CustomActor"]["description"]
            == "Custom actor type for testing"
        )

        # Static types should also be present
        assert "SavedView" in schema["node_types"]
        assert schema["node_types"]["SavedView"]["static"] is True

    def test_presentation_colors_from_custom_config(self):
        """Test that presentation colors are loaded from custom config."""
        from backend.config import config_loader

        test_config_path = str(
            Path(__file__).parent.parent.parent
            / "config"
            / "test"
            / "schema_config.json"
        )
        os.environ["SCHEMA_FILE"] = test_config_path
        config_loader.reset_loader()

        presentation = config_loader.get_presentation()

        assert presentation["colors"]["CustomActor"] == "#FF0000"
        assert presentation["colors"]["TestNode"] == "#00FF00"


class TestTenantContext:
    """Tests for get_tenant_context() function."""

    @pytest.fixture(autouse=True)
    def clean_env(self):
        """Ensure tenant context env vars are unset before and after each test."""
        for var in (
            "COMMUNITYOVERVIEW_TENANT_ID",
            "COMMUNITYOVERVIEW_TENANT_NAME",
            "COMMUNITYOVERVIEW_ENVIRONMENT",
            "COMMUNITYOVERVIEW_TENANT_CONFIG_DIR",
            "SCHEMA_FILE",
            "GRAPH_SCHEMA_CONFIG",
            "FEDERATION_FILE",
            "GRAPH_FEDERATION_CONFIG",
        ):
            os.environ.pop(var, None)
        yield
        for var in (
            "COMMUNITYOVERVIEW_TENANT_ID",
            "COMMUNITYOVERVIEW_TENANT_NAME",
            "COMMUNITYOVERVIEW_ENVIRONMENT",
            "COMMUNITYOVERVIEW_TENANT_CONFIG_DIR",
            "SCHEMA_FILE",
            "GRAPH_SCHEMA_CONFIG",
            "FEDERATION_FILE",
            "GRAPH_FEDERATION_CONFIG",
        ):
            os.environ.pop(var, None)

    def test_defaults_when_env_vars_unset(self):
        """Tenant context returns safe standalone defaults when no env vars are set."""
        from backend.config import config_loader

        result = config_loader.get_tenant_context()

        assert result == {
            "tenant_id": "",
            "tenant_name": "",
            "environment": "local",
        }

    def test_tenant_id_env_override(self):
        """COMMUNITYOVERVIEW_TENANT_ID overrides the tenant_id field."""
        from backend.config import config_loader

        os.environ["COMMUNITYOVERVIEW_TENANT_ID"] = "acme-corp"
        result = config_loader.get_tenant_context()

        assert result["tenant_id"] == "acme-corp"
        assert result["tenant_name"] == ""
        assert result["environment"] == "local"

    def test_tenant_name_env_override(self):
        """COMMUNITYOVERVIEW_TENANT_NAME overrides the tenant_name field."""
        from backend.config import config_loader

        os.environ["COMMUNITYOVERVIEW_TENANT_NAME"] = "Acme Corporation"
        result = config_loader.get_tenant_context()

        assert result["tenant_name"] == "Acme Corporation"

    def test_environment_env_override(self):
        """COMMUNITYOVERVIEW_ENVIRONMENT overrides the environment field."""
        from backend.config import config_loader

        os.environ["COMMUNITYOVERVIEW_ENVIRONMENT"] = "production"
        result = config_loader.get_tenant_context()

        assert result["environment"] == "production"

    def test_all_fields_overridden(self):
        """All three fields can be overridden simultaneously."""
        from backend.config import config_loader

        os.environ["COMMUNITYOVERVIEW_TENANT_ID"] = "t-123"
        os.environ["COMMUNITYOVERVIEW_TENANT_NAME"] = "Test Tenant"
        os.environ["COMMUNITYOVERVIEW_ENVIRONMENT"] = "staging"

        result = config_loader.get_tenant_context()

        assert result == {
            "tenant_id": "t-123",
            "tenant_name": "Test Tenant",
            "environment": "staging",
        }

    def test_response_shape_has_exactly_three_keys(self):
        """Response shape is exactly {tenant_id, tenant_name, environment}."""
        from backend.config import config_loader

        result = config_loader.get_tenant_context()

        assert set(result.keys()) == {"tenant_id", "tenant_name", "environment"}


class TestConfigContext:
    """Tests for tenant-aware config path layering introspection."""

    @pytest.fixture(autouse=True)
    def clean_env(self):
        for var in (
            "COMMUNITYOVERVIEW_TENANT_ID",
            "COMMUNITYOVERVIEW_TENANT_NAME",
            "COMMUNITYOVERVIEW_ENVIRONMENT",
            "COMMUNITYOVERVIEW_TENANT_CONFIG_DIR",
            "SCHEMA_FILE",
            "GRAPH_SCHEMA_CONFIG",
            "FEDERATION_FILE",
            "GRAPH_FEDERATION_CONFIG",
        ):
            os.environ.pop(var, None)
        yield
        for var in (
            "COMMUNITYOVERVIEW_TENANT_ID",
            "COMMUNITYOVERVIEW_TENANT_NAME",
            "COMMUNITYOVERVIEW_ENVIRONMENT",
            "COMMUNITYOVERVIEW_TENANT_CONFIG_DIR",
            "SCHEMA_FILE",
            "GRAPH_SCHEMA_CONFIG",
            "FEDERATION_FILE",
            "GRAPH_FEDERATION_CONFIG",
        ):
            os.environ.pop(var, None)

    def test_defaults_to_public_default_paths(self):
        from backend.config import config_loader

        result = config_loader.get_config_context()

        assert result["tenant_config_dir_configured"] is False
        assert result["schema_config_source"] == "default"
        assert result["federation_config_source"] == "default"
        assert "tenant_config_dir" not in result
        assert "schema_config_path" not in result
        assert "federation_config_path" not in result

    def test_resolves_schema_and_federation_from_tenant_config_dir(
        self, tmp_path: Path
    ):
        from backend.config import config_loader

        tenant_dir = tmp_path / "tenant-config"
        tenant_dir.mkdir()
        (tenant_dir / "schema_config.json").write_text("{}", encoding="utf-8")
        (tenant_dir / "federation_config.json").write_text(
            '{"federation": {}}', encoding="utf-8"
        )

        os.environ["COMMUNITYOVERVIEW_TENANT_CONFIG_DIR"] = str(tenant_dir)

        result = config_loader.get_config_context()

        assert result["tenant_config_dir_configured"] is True
        assert result["schema_config_source"] == "tenant_config_dir"
        assert result["federation_config_source"] == "tenant_config_dir"
        assert "tenant_config_dir" not in result
        assert "schema_config_path" not in result
        assert "federation_config_path" not in result

    def test_explicit_file_env_vars_override_tenant_config_dir(self, tmp_path: Path):
        from backend.config import config_loader

        tenant_dir = tmp_path / "tenant-config"
        tenant_dir.mkdir()
        explicit_schema = tmp_path / "explicit-schema.json"
        explicit_federation = tmp_path / "explicit-federation.json"
        explicit_schema.write_text("{}", encoding="utf-8")
        explicit_federation.write_text('{"federation": {}}', encoding="utf-8")

        os.environ["COMMUNITYOVERVIEW_TENANT_CONFIG_DIR"] = str(tenant_dir)
        os.environ["SCHEMA_FILE"] = str(explicit_schema)
        os.environ["FEDERATION_FILE"] = str(explicit_federation)

        result = config_loader.get_config_context()

        assert result["tenant_config_dir_configured"] is True
        assert result["schema_config_source"] == "explicit_env"
        assert result["federation_config_source"] == "explicit_env"
        assert "tenant_config_dir" not in result
        assert "schema_config_path" not in result
        assert "federation_config_path" not in result


class TestBuildSessionUrl:
    """Canonical ?session=<id> deep-link construction (contract §5)."""

    @pytest.fixture(autouse=True)
    def _clear_base_url(self):
        from backend.config.config_loader import PUBLIC_BASE_URL_ENV

        os.environ.pop(PUBLIC_BASE_URL_ENV, None)
        yield
        os.environ.pop(PUBLIC_BASE_URL_ENV, None)

    def test_none_when_unconfigured(self):
        from backend.config.config_loader import build_session_url

        assert build_session_url("1234-5678-9012-3456") is None

    def test_bare_origin_gets_root_path(self):
        from backend.config.config_loader import PUBLIC_BASE_URL_ENV, build_session_url

        os.environ[PUBLIC_BASE_URL_ENV] = "https://app.example.test"
        assert (
            build_session_url("1234-5678")
            == "https://app.example.test/?session=1234-5678"
        )

    def test_trailing_slash_is_not_doubled(self):
        from backend.config.config_loader import PUBLIC_BASE_URL_ENV, build_session_url

        os.environ[PUBLIC_BASE_URL_ENV] = "https://app.example.test/"
        assert (
            build_session_url("1234-5678")
            == "https://app.example.test/?session=1234-5678"
        )

    def test_base_path_is_preserved(self):
        from backend.config.config_loader import PUBLIC_BASE_URL_ENV, build_session_url

        os.environ[PUBLIC_BASE_URL_ENV] = "https://example.test/app"
        assert (
            build_session_url("1234-5678")
            == "https://example.test/app?session=1234-5678"
        )

    def test_empty_session_id_is_none(self):
        from backend.config.config_loader import PUBLIC_BASE_URL_ENV, build_session_url

        os.environ[PUBLIC_BASE_URL_ENV] = "https://app.example.test"
        assert build_session_url("") is None
