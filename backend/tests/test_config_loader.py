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
        test_config_path = str(Path(__file__).parent.parent.parent / "config" / "test_schema_config.json")
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

    def test_custom_presentation(self):
        """Test presentation from custom config."""
        from backend import config_loader

        # Set custom config path
        test_config_path = str(Path(__file__).parent.parent.parent / "config" / "test_schema_config.json")
        os.environ["SCHEMA_FILE"] = test_config_path

        config_loader.reset_loader()

        presentation = config_loader.get_presentation()

        assert presentation["title"] == "Test Knowledge Graph"
        assert presentation["introduction"] == "This is a test instance."
        assert presentation["prompt_prefix"] == "You are a test assistant."

        del os.environ["SCHEMA_FILE"]

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
        from backend.core import models

        # Get valid node types
        valid_types = models.get_node_type_names()
        assert len(valid_types) > 0

        # Check validation function works
        assert models.is_valid_node_type("SavedView") is True

    def test_service_returns_schema(self):
        """Test that GraphService returns schema correctly."""
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
        finally:
            os.unlink(temp_path)

    def test_list_node_types_uses_config(self):
        """Test that list_node_types returns config-based types."""
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
        test_config_path = str(Path(__file__).parent.parent.parent / "config" / "test_schema_config.json")
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

        test_config_path = str(Path(__file__).parent.parent.parent / "config" / "test_schema_config.json")
        os.environ["SCHEMA_FILE"] = test_config_path
        config_loader.reset_loader()

        presentation = config_loader.get_presentation()

        assert presentation["colors"]["CustomActor"] == "#FF0000"
        assert presentation["colors"]["TestNode"] == "#00FF00"
