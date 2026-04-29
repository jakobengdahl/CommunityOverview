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
        from backend import config_loader
        config_loader.reset_loader()
        yield
        os.environ.pop("SCHEMA_FILE", None)
        os.environ.pop("COMMUNITYOVERVIEW_RUNTIME_MODE", None)
        os.environ.pop("COMMUNITYOVERVIEW_ENABLED_EXTENSIONS", None)
        config_loader.reset_loader()

    def test_load_default_config(self):
        """Test loading the default configuration file."""
        from backend import config_loader

        schema = config_loader.get_schema()

        # Check that schema has node_types and relationship_types
        assert "node_types" in schema
        assert "relationship_types" in schema

        # Check that static types are present
        assert "SavedView" in schema["node_types"]
        assert "VisualizationView" in schema["node_types"]

        # Check that SavedView is marked as static
        assert schema["node_types"]["SavedView"]["static"] is True

    def test_load_custom_config(self):
        """Test loading a custom configuration file."""
        from backend import config_loader

        # Set custom config path
        test_config_path = str(Path(__file__).parent.parent.parent / "config" / "test" / "schema_config.json")
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
        from backend import config_loader

        presentation = config_loader.get_presentation()

        # Check presentation has expected fields
        assert "title" in presentation
        assert "introduction" in presentation
        assert "colors" in presentation
        assert "prompt_prefix" in presentation
        assert "prompt_suffix" in presentation
        assert "default_language" in presentation
        assert "language_policy" in presentation

    def test_custom_presentation(self):
        """Test presentation from custom config."""
        from backend import config_loader

        # Set custom config path
        test_config_path = str(Path(__file__).parent.parent.parent / "config" / "test" / "schema_config.json")
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

    def test_get_capabilities_defaults_to_empty_list(self):
        """Test capability manifest defaults to an empty list when not configured."""
        from backend import config_loader

        capabilities = config_loader.get_capabilities()

        assert capabilities == {"capabilities": []}

    def test_get_capabilities_from_custom_config(self):
        """Test capability manifest is loaded from custom config."""
        from backend import config_loader

        test_config_path = str(Path(__file__).parent.parent.parent / "config" / "test" / "schema_config.json")
        os.environ["SCHEMA_FILE"] = test_config_path
        config_loader.reset_loader()

        capabilities = config_loader.get_capabilities()

        assert capabilities == {
            "capabilities": [
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
        }

        del os.environ["SCHEMA_FILE"]

    def test_get_runtime_info_defaults_to_standalone(self):
        """Test runtime metadata defaults to standalone mode with no extensions."""
        from backend import config_loader

        runtime_info = config_loader.get_runtime_info()

        assert runtime_info == {
            "runtime_mode": "standalone",
            "enabled_extensions": [],
        }

    def test_get_runtime_info_runtime_mode_env_override(self):
        """Test runtime mode can be overridden through environment configuration."""
        from backend import config_loader

        os.environ["COMMUNITYOVERVIEW_RUNTIME_MODE"] = "hosted"
        config_loader.reset_loader()

        runtime_info = config_loader.get_runtime_info()

        assert runtime_info == {
            "runtime_mode": "hosted",
            "enabled_extensions": [],
        }

    def test_get_runtime_info_enabled_extensions_env_override(self):
        """Test enabled extensions can be overridden through environment configuration."""
        from backend import config_loader

        os.environ["COMMUNITYOVERVIEW_RUNTIME_MODE"] = "hosted"
        os.environ["COMMUNITYOVERVIEW_ENABLED_EXTENSIONS"] = "federation, analytics , federation,"
        config_loader.reset_loader()

        runtime_info = config_loader.get_runtime_info()

        assert runtime_info == {
            "runtime_mode": "hosted",
            "enabled_extensions": ["federation", "analytics"],
        }

    def test_get_node_type_names(self):
        """Test getting list of node type names."""
        from backend import config_loader

        names = config_loader.get_node_type_names()

        assert isinstance(names, list)
        assert "SavedView" in names  # Static type always present

    def test_get_relationship_type_names(self):
        """Test getting list of relationship type names."""
        from backend import config_loader

        names = config_loader.get_relationship_type_names()

        assert isinstance(names, list)
        assert len(names) > 0

    def test_get_node_color(self):
        """Test getting color for a node type."""
        from backend import config_loader

        # SavedView should have gray color
        color = config_loader.get_node_color("SavedView")
        assert color == "#6B7280"

        # Unknown type should get default color
        color = config_loader.get_node_color("UnknownType")
        assert color == "#9CA3AF"

    def test_invalid_config_uses_defaults(self):
        """Test that invalid config file falls back to defaults."""
        from backend import config_loader

        # Create a temp file with invalid JSON
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
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
        from backend import config_loader

        os.environ["SCHEMA_FILE"] = "/nonexistent/path/config.json"
        config_loader.reset_loader()

        # Should still work with defaults
        schema = config_loader.get_schema()
        assert "node_types" in schema
        assert "SavedView" in schema["node_types"]

        del os.environ["SCHEMA_FILE"]

    def test_config_path_getter(self):
        """Test getting the config file path."""
        from backend import config_loader

        path = config_loader.get_config_path()
        assert path is not None
        assert isinstance(path, str)


class TestSchemaIntegration:
    """Integration tests for schema with other backend components."""

    @pytest.fixture(autouse=True)
    def reset_loader(self):
        """Reset the config loader before each test."""
        from backend import config_loader
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
        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
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
            assert capabilities == {"capabilities": []}
        finally:
            os.unlink(temp_path)

    def test_list_node_types_uses_config(self):
        """Test that list_node_types returns config-based types."""
        pytest.importorskip("networkx")
        import tempfile
        from backend.core import GraphStorage
        from backend.service import GraphService

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
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
        from backend import config_loader
        config_loader.reset_loader()
        yield
        # Clean up env var
        if "SCHEMA_FILE" in os.environ:
            del os.environ["SCHEMA_FILE"]
        config_loader.reset_loader()

    def test_extra_node_type_in_custom_config(self):
        """Test that extra node types from custom config are available."""
        from backend import config_loader

        # Use test config with extra types
        test_config_path = str(Path(__file__).parent.parent.parent / "config" / "test" / "schema_config.json")
        os.environ["SCHEMA_FILE"] = test_config_path
        config_loader.reset_loader()

        schema = config_loader.get_schema()

        # Custom types should be present
        assert "CustomActor" in schema["node_types"]
        assert schema["node_types"]["CustomActor"]["color"] == "#FF0000"
        assert schema["node_types"]["CustomActor"]["description"] == "Custom actor type for testing"

        # Static types should also be present
        assert "SavedView" in schema["node_types"]
        assert schema["node_types"]["SavedView"]["static"] is True

    def test_presentation_colors_from_custom_config(self):
        """Test that presentation colors are loaded from custom config."""
        from backend import config_loader

        test_config_path = str(Path(__file__).parent.parent.parent / "config" / "test" / "schema_config.json")
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
        from backend import config_loader

        result = config_loader.get_tenant_context()

        assert result == {
            "tenant_id": "",
            "tenant_name": "",
            "environment": "local",
        }

    def test_tenant_id_env_override(self):
        """COMMUNITYOVERVIEW_TENANT_ID overrides the tenant_id field."""
        from backend import config_loader

        os.environ["COMMUNITYOVERVIEW_TENANT_ID"] = "acme-corp"
        result = config_loader.get_tenant_context()

        assert result["tenant_id"] == "acme-corp"
        assert result["tenant_name"] == ""
        assert result["environment"] == "local"

    def test_tenant_name_env_override(self):
        """COMMUNITYOVERVIEW_TENANT_NAME overrides the tenant_name field."""
        from backend import config_loader

        os.environ["COMMUNITYOVERVIEW_TENANT_NAME"] = "Acme Corporation"
        result = config_loader.get_tenant_context()

        assert result["tenant_name"] == "Acme Corporation"

    def test_environment_env_override(self):
        """COMMUNITYOVERVIEW_ENVIRONMENT overrides the environment field."""
        from backend import config_loader

        os.environ["COMMUNITYOVERVIEW_ENVIRONMENT"] = "production"
        result = config_loader.get_tenant_context()

        assert result["environment"] == "production"

    def test_all_fields_overridden(self):
        """All three fields can be overridden simultaneously."""
        from backend import config_loader

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
        from backend import config_loader

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
        from backend import config_loader

        result = config_loader.get_config_context()

        assert result["tenant_config_dir_configured"] is False
        assert result["schema_config_source"] == "default"
        assert result["federation_config_source"] == "default"
        assert "tenant_config_dir" not in result
        assert "schema_config_path" not in result
        assert "federation_config_path" not in result

    def test_resolves_schema_and_federation_from_tenant_config_dir(self, tmp_path: Path):
        from backend import config_loader

        tenant_dir = tmp_path / "tenant-config"
        tenant_dir.mkdir()
        (tenant_dir / "schema_config.json").write_text("{}", encoding="utf-8")
        (tenant_dir / "federation_config.json").write_text('{"federation": {}}', encoding="utf-8")

        os.environ["COMMUNITYOVERVIEW_TENANT_CONFIG_DIR"] = str(tenant_dir)

        result = config_loader.get_config_context()

        assert result["tenant_config_dir_configured"] is True
        assert result["schema_config_source"] == "tenant_config_dir"
        assert result["federation_config_source"] == "tenant_config_dir"
        assert "tenant_config_dir" not in result
        assert "schema_config_path" not in result
        assert "federation_config_path" not in result

    def test_explicit_file_env_vars_override_tenant_config_dir(self, tmp_path: Path):
        from backend import config_loader

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
