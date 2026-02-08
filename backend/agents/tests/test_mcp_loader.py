"""
Tests for MCP loader and tool namespacing.
"""

import pytest
from unittest.mock import MagicMock, patch

from backend.agents.config import MCPIntegration
from backend.agents.mcp_loader import MCPLoader


class TestMCPLoader:
    """Tests for MCPLoader functionality."""

    def test_init_with_integrations(self):
        """Test initializing loader with integrations."""
        integrations = [
            MCPIntegration(id="GRAPH", type="http", url="http://localhost:8000/mcp"),
            MCPIntegration(id="FS", type="stdio", command="node", args=["mcp-fs"]),
        ]

        loader = MCPLoader(integrations)

        assert len(loader._integrations) == 2
        assert "GRAPH" in [i.id for i in loader._integrations]
        assert "FS" in [i.id for i in loader._integrations]

    def test_init_empty(self):
        """Test initializing loader with no integrations."""
        loader = MCPLoader([])

        assert len(loader._integrations) == 0

    def test_get_tool_definitions_empty(self):
        """Test getting tool definitions when no tools discovered."""
        loader = MCPLoader([])

        tools = loader.get_tool_definitions([])

        assert tools == []

    def test_get_tool_definitions_filters_by_integration(self):
        """Test that tool definitions are filtered by requested integrations."""
        loader = MCPLoader([])

        # Manually add some tools to simulate discovery
        loader._tools = {
            "GRAPH": [
                {"name": "search_graph", "description": "Search the graph"},
                {"name": "update_node", "description": "Update a node"},
            ],
            "WEB": [
                {"name": "fetch", "description": "Fetch a URL"},
            ],
        }

        # Request only GRAPH tools
        tools = loader.get_tool_definitions(["GRAPH"])

        assert len(tools) == 2
        # Check namespacing
        names = [t["name"] for t in tools]
        assert "GRAPH.search_graph" in names
        assert "GRAPH.update_node" in names
        assert "WEB.fetch" not in names

    def test_get_tool_definitions_multiple_integrations(self):
        """Test getting tools from multiple integrations."""
        loader = MCPLoader([])

        loader._tools = {
            "GRAPH": [{"name": "search_graph", "description": "Search"}],
            "WEB": [{"name": "fetch", "description": "Fetch"}],
            "FS": [{"name": "read_file", "description": "Read file"}],
        }

        tools = loader.get_tool_definitions(["GRAPH", "WEB"])

        assert len(tools) == 2
        names = [t["name"] for t in tools]
        assert "GRAPH.search_graph" in names
        assert "WEB.fetch" in names
        assert "FS.read_file" not in names


class TestToolNamespacing:
    """Tests for tool namespacing logic."""

    def test_namespace_tool_definition(self):
        """Test that tool definitions are properly namespaced."""
        loader = MCPLoader([])

        loader._tools = {
            "GRAPH": [
                {
                    "name": "search_graph",
                    "description": "Search the knowledge graph",
                    "input_schema": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                    },
                }
            ]
        }

        tools = loader.get_tool_definitions(["GRAPH"])

        assert len(tools) == 1
        assert tools[0]["name"] == "GRAPH.search_graph"
        assert tools[0]["description"] == "Search the knowledge graph"
        assert "input_schema" in tools[0]

    def test_namespace_preserves_schema(self):
        """Test that namespacing preserves the input schema."""
        loader = MCPLoader([])

        original_schema = {
            "type": "object",
            "properties": {
                "node_id": {"type": "string", "description": "Node ID"},
                "updates": {"type": "object"},
            },
            "required": ["node_id"],
        }

        loader._tools = {
            "GRAPH": [
                {
                    "name": "update_node",
                    "description": "Update a node",
                    "input_schema": original_schema,
                }
            ]
        }

        tools = loader.get_tool_definitions(["GRAPH"])

        assert tools[0]["input_schema"] == original_schema


class TestToolExecutor:
    """Tests for tool executor creation."""

    def test_create_tool_executor_graph_integration(self, mock_service):
        """Test creating a tool executor for GRAPH integration."""
        integrations = [
            MCPIntegration(id="GRAPH", type="http", url="http://localhost:8000/mcp")
        ]
        loader = MCPLoader(integrations)

        executor = loader.create_tool_executor(["GRAPH"], mock_service)

        # Should return a callable
        assert callable(executor)

    def test_executor_routes_to_graph_service(self, mock_service):
        """Test that executor routes GRAPH tools to graph service."""
        integrations = [
            MCPIntegration(id="GRAPH", type="http", url="http://localhost:8000/mcp")
        ]
        loader = MCPLoader(integrations)

        executor = loader.create_tool_executor(["GRAPH"], mock_service)

        # Call the executor with a GRAPH tool
        result = executor("GRAPH.search_graph", {"query": "test"})

        # Should have called the mock service
        assert len(mock_service.search_calls) == 1
        assert mock_service.search_calls[0]["query"] == "test"

    def test_executor_unknown_tool_raises(self, mock_service):
        """Test that calling unknown tool raises an error."""
        loader = MCPLoader([])
        executor = loader.create_tool_executor([], mock_service)

        with pytest.raises(ValueError, match="Unknown tool"):
            executor("UNKNOWN.tool", {})

    def test_executor_parses_namespaced_name(self, mock_service):
        """Test that executor correctly parses namespaced tool names."""
        integrations = [
            MCPIntegration(id="GRAPH", type="http", url="http://localhost:8000/mcp")
        ]
        loader = MCPLoader(integrations)

        executor = loader.create_tool_executor(["GRAPH"], mock_service)

        # Test with dotted name
        result = executor("GRAPH.update_node", {"node_id": "node-1", "name": "New Name"})

        assert len(mock_service.update_calls) == 1


class TestMCPLoaderLifecycle:
    """Tests for MCP loader connection lifecycle."""

    def test_connect_all_returns_tool_map(self):
        """Test that connect_all returns a map of tools per integration."""
        integrations = [
            MCPIntegration(id="GRAPH", type="http", url="http://localhost:8000/mcp")
        ]
        loader = MCPLoader(integrations)

        # Mock the internal connection
        with patch.object(loader, "_connect_http", return_value=[
            {"name": "search_graph", "description": "Search"}
        ]):
            result = loader.connect_all()

        assert "GRAPH" in result
        assert len(result["GRAPH"]) == 1

    def test_disconnect_all_clears_tools(self):
        """Test that disconnect_all clears the tools map."""
        loader = MCPLoader([])
        loader._tools = {"GRAPH": [{"name": "test"}]}
        loader._connected = True

        loader.disconnect_all()

        assert loader._tools == {}
        assert loader._connected is False
