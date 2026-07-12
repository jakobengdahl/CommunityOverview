"""
Tests for MCP loader and tool namespacing.
"""

from unittest.mock import patch

from backend.agents.config import MCPIntegration, MCPTransport
from backend.agents.mcp_loader import MCPLoader, NamespacedTool


class TestMCPLoader:
    """Tests for MCPLoader functionality."""

    def test_init_with_integrations(self):
        """Test initializing loader with integrations."""
        integrations = [
            MCPIntegration(
                id="GRAPH",
                name="Graph API",
                transport=MCPTransport.HTTP,
                url="http://localhost:8000/mcp",
            ),
            MCPIntegration(
                id="FS",
                name="FileSystem",
                transport=MCPTransport.STDIO,
                command=["node", "mcp-fs"],
            ),
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
        loader._tools_cache = {
            "GRAPH__search_graph": NamespacedTool(
                integration_id="GRAPH",
                original_name="search_graph",
                namespaced_name="GRAPH__search_graph",
                description="Search the graph",
                input_schema={},
            ),
            "GRAPH__update_node": NamespacedTool(
                integration_id="GRAPH",
                original_name="update_node",
                namespaced_name="GRAPH__update_node",
                description="Update a node",
                input_schema={},
            ),
            "WEB__fetch": NamespacedTool(
                integration_id="WEB",
                original_name="fetch",
                namespaced_name="WEB__fetch",
                description="Fetch a URL",
                input_schema={},
            ),
        }

        # Request only GRAPH tools
        tools = loader.get_tool_definitions(["GRAPH"])

        assert len(tools) == 2
        # Check namespacing
        names = [t["name"] for t in tools]
        assert "GRAPH__search_graph" in names
        assert "GRAPH__update_node" in names
        assert "WEB__fetch" not in names

    def test_get_tool_definitions_multiple_integrations(self):
        """Test getting tools from multiple integrations."""
        loader = MCPLoader([])

        loader._tools_cache = {
            "GRAPH__search_graph": NamespacedTool(
                integration_id="GRAPH",
                original_name="search_graph",
                namespaced_name="GRAPH__search_graph",
                description="Search",
                input_schema={},
            ),
            "WEB__fetch": NamespacedTool(
                integration_id="WEB",
                original_name="fetch",
                namespaced_name="WEB__fetch",
                description="Fetch",
                input_schema={},
            ),
            "FS__read_file": NamespacedTool(
                integration_id="FS",
                original_name="read_file",
                namespaced_name="FS__read_file",
                description="Read file",
                input_schema={},
            ),
        }

        tools = loader.get_tool_definitions(["GRAPH", "WEB"])

        assert len(tools) == 2
        names = [t["name"] for t in tools]
        assert "GRAPH__search_graph" in names
        assert "WEB__fetch" in names
        assert "FS__read_file" not in names


class TestToolNamespacing:
    """Tests for tool namespacing logic."""

    def test_namespace_tool_definition(self):
        """Test that tool definitions are properly namespaced."""
        loader = MCPLoader([])

        loader._tools_cache = {
            "GRAPH__search_graph": NamespacedTool(
                integration_id="GRAPH",
                original_name="search_graph",
                namespaced_name="GRAPH__search_graph",
                description="Search the knowledge graph",
                input_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                },
            )
        }

        tools = loader.get_tool_definitions(["GRAPH"])

        assert len(tools) == 1
        assert tools[0]["name"] == "GRAPH__search_graph"
        # The description in tool definition includes integration ID prefix
        assert tools[0]["description"] == "[GRAPH] Search the knowledge graph"
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

        loader._tools_cache = {
            "GRAPH__update_node": NamespacedTool(
                integration_id="GRAPH",
                original_name="update_node",
                namespaced_name="GRAPH__update_node",
                description="Update a node",
                input_schema=original_schema,
            )
        }

        tools = loader.get_tool_definitions(["GRAPH"])

        assert tools[0]["input_schema"] == original_schema


class TestToolExecutor:
    """Tests for tool executor creation."""

    def test_create_tool_executor_graph_integration(self, mock_service):
        """Test creating a tool executor for GRAPH integration."""
        integrations = [
            MCPIntegration(
                id="GRAPH",
                name="Graph API",
                transport=MCPTransport.HTTP,
                url="http://localhost:8000/mcp",
            )
        ]
        loader = MCPLoader(integrations)

        executor = loader.create_tool_executor(graph_service=mock_service)

        # Should return a callable
        assert callable(executor)

    def test_executor_routes_to_graph_service(self, mock_service):
        """Test that executor routes GRAPH tools to graph service."""
        integrations = [
            MCPIntegration(
                id="GRAPH",
                name="Graph API",
                transport=MCPTransport.HTTP,
                url="http://localhost:8000/mcp",
            )
        ]
        loader = MCPLoader(integrations)

        # Pre-populate cache so executor finds the tool
        loader._tools_cache = {
            "GRAPH__search_graph": NamespacedTool(
                integration_id="GRAPH",
                original_name="search_graph",
                namespaced_name="GRAPH__search_graph",
                description="Search",
                input_schema={},
            )
        }

        executor = loader.create_tool_executor(graph_service=mock_service)

        # Call the executor with a GRAPH tool
        executor("GRAPH__search_graph", {"query": "test"})

        # Should have called the mock service
        assert len(mock_service.search_calls) == 1
        assert mock_service.search_calls[0]["query"] == "test"

    def test_executor_unknown_tool_raises(self, mock_service):
        """Test that calling unknown tool returns error."""
        loader = MCPLoader([])
        executor = loader.create_tool_executor(graph_service=mock_service)

        result = executor("UNKNOWN.tool", {})
        assert "error" in result
        assert "Unknown tool" in result["error"]

    def test_executor_parses_namespaced_name(self, mock_service):
        """Test that executor correctly parses namespaced tool names."""
        integrations = [
            MCPIntegration(
                id="GRAPH",
                name="Graph API",
                transport=MCPTransport.HTTP,
                url="http://localhost:8000/mcp",
            )
        ]
        loader = MCPLoader(integrations)

        # Pre-populate cache
        loader._tools_cache = {
            "GRAPH__update_node": NamespacedTool(
                integration_id="GRAPH",
                original_name="update_node",
                namespaced_name="GRAPH__update_node",
                description="Update",
                input_schema={},
            )
        }

        executor = loader.create_tool_executor(graph_service=mock_service)

        # Test with namespaced name
        executor("GRAPH__update_node", {"node_id": "node-1", "name": "New Name"})

        assert len(mock_service.update_calls) == 1

    def test_executor_routes_delete_edges_to_graph_service(self, mock_service):
        """Bulk edge deletion tool should route to GraphService.delete_edges."""
        integrations = [
            MCPIntegration(
                id="GRAPH",
                name="Graph API",
                transport=MCPTransport.HTTP,
                url="http://localhost:8000/mcp",
            )
        ]
        loader = MCPLoader(integrations)

        loader._tools_cache = {
            "GRAPH__delete_edges": NamespacedTool(
                integration_id="GRAPH",
                original_name="delete_edges",
                namespaced_name="GRAPH__delete_edges",
                description="Delete edges",
                input_schema={},
            )
        }

        executor = loader.create_tool_executor(graph_service=mock_service)
        result = executor("GRAPH__delete_edges", {"edge_ids": ["edge-1", "edge-2"]})

        assert result["success"] is True
        assert len(mock_service.delete_edges_calls) == 1
        assert mock_service.delete_edges_calls[0]["edge_ids"] == ["edge-1", "edge-2"]


class TestMCPLoaderLifecycle:
    """Tests for MCP loader connection lifecycle."""

    def test_connect_graph_includes_get_capabilities_tool(self):
        """GRAPH discovery inventory includes get_capabilities."""
        integration = MCPIntegration(
            id="GRAPH",
            name="Graph API",
            transport=MCPTransport.HTTP,
            url="http://localhost:8000/mcp",
        )
        loader = MCPLoader([integration])

        tools = loader._get_graph_mcp_tools(integration)

        tool_names = [tool.namespaced_name for tool in tools]
        assert "GRAPH__get_capabilities" in tool_names

    def test_connect_graph_includes_get_runtime_info_tool(self):
        """GRAPH discovery inventory includes get_runtime_info."""
        integration = MCPIntegration(
            id="GRAPH",
            name="Graph API",
            transport=MCPTransport.HTTP,
            url="http://localhost:8000/mcp",
        )
        loader = MCPLoader([integration])

        tools = loader._get_graph_mcp_tools(integration)

        tool_names = [tool.namespaced_name for tool in tools]
        assert "GRAPH__get_runtime_info" in tool_names

    def test_connect_graph_includes_get_tenant_context_tool(self):
        """GRAPH discovery inventory includes get_tenant_context."""
        integration = MCPIntegration(
            id="GRAPH",
            name="Graph API",
            transport=MCPTransport.HTTP,
            url="http://localhost:8000/mcp",
        )
        loader = MCPLoader([integration])

        tools = loader._get_graph_mcp_tools(integration)

        tool_names = [tool.namespaced_name for tool in tools]
        assert "GRAPH__get_tenant_context" in tool_names

        tenant_context_tool = next(
            tool
            for tool in tools
            if tool.namespaced_name == "GRAPH__get_tenant_context"
        )
        assert tenant_context_tool.original_name == "get_tenant_context"
        assert tenant_context_tool.input_schema == {"type": "object", "properties": {}}

    def test_connect_graph_includes_get_config_context_tool(self):
        """GRAPH discovery inventory includes get_config_context."""
        integration = MCPIntegration(
            id="GRAPH",
            name="Graph API",
            transport=MCPTransport.HTTP,
            url="http://localhost:8000/mcp",
        )
        loader = MCPLoader([integration])

        tools = loader._get_graph_mcp_tools(integration)

        tool_names = [tool.namespaced_name for tool in tools]
        assert "GRAPH__get_config_context" in tool_names

        config_context_tool = next(
            tool
            for tool in tools
            if tool.namespaced_name == "GRAPH__get_config_context"
        )
        assert config_context_tool.original_name == "get_config_context"
        assert config_context_tool.input_schema == {"type": "object", "properties": {}}

    def test_connect_graph_includes_get_request_actor_tool(self):
        """GRAPH discovery inventory includes get_request_actor."""
        integration = MCPIntegration(
            id="GRAPH",
            name="Graph API",
            transport=MCPTransport.HTTP,
            url="http://localhost:8000/mcp",
        )
        loader = MCPLoader([integration])

        tools = loader._get_graph_mcp_tools(integration)

        actor_tool = next(
            tool for tool in tools if tool.namespaced_name == "GRAPH__get_request_actor"
        )
        assert actor_tool.original_name == "get_request_actor"
        assert set(actor_tool.input_schema["properties"].keys()) == {
            "actor_id",
            "actor_type",
            "auth_source",
        }

    def test_connect_graph_includes_get_request_scope_tool(self):
        """GRAPH discovery inventory includes get_request_scope."""
        integration = MCPIntegration(
            id="GRAPH",
            name="Graph API",
            transport=MCPTransport.HTTP,
            url="http://localhost:8000/mcp",
        )
        loader = MCPLoader([integration])

        tools = loader._get_graph_mcp_tools(integration)

        scope_tool = next(
            tool for tool in tools if tool.namespaced_name == "GRAPH__get_request_scope"
        )
        assert scope_tool.original_name == "get_request_scope"
        assert set(scope_tool.input_schema["properties"].keys()) == {
            "workspace_id",
            "workspace_kind",
            "graph_id",
        }

    def test_connect_graph_includes_get_request_selection_tool(self):
        """GRAPH discovery inventory includes get_request_selection."""
        integration = MCPIntegration(
            id="GRAPH",
            name="Graph API",
            transport=MCPTransport.HTTP,
            url="http://localhost:8000/mcp",
        )
        loader = MCPLoader([integration])

        tools = loader._get_graph_mcp_tools(integration)

        selection_tool = next(
            tool
            for tool in tools
            if tool.namespaced_name == "GRAPH__get_request_selection"
        )
        assert selection_tool.original_name == "get_request_selection"
        assert set(selection_tool.input_schema["properties"].keys()) == {
            "workspace_id",
            "workspace_kind",
            "graph_id",
        }

    def test_connect_all_returns_tool_map(self):
        """Test that connect_all returns a map of tools per integration."""
        integrations = [
            MCPIntegration(
                id="GRAPH",
                name="Graph API",
                transport=MCPTransport.HTTP,
                url="http://localhost:8000/mcp",
            )
        ]
        loader = MCPLoader(integrations)

        # Mock the internal connection
        mock_tools = [
            NamespacedTool(
                integration_id="GRAPH",
                original_name="search_graph",
                namespaced_name="GRAPH__search_graph",
                description="Search",
                input_schema={},
            )
        ]

        with patch.object(loader, "_connect_http", return_value=mock_tools):
            result = loader.connect_all()

        assert "GRAPH" in result
        assert len(result["GRAPH"]) == 1

    def test_disconnect_all_clears_tools(self):
        """Test that disconnect_all clears the tools map."""
        loader = MCPLoader([])
        loader._tools_cache = {
            "GRAPH__test": NamespacedTool(
                integration_id="GRAPH",
                original_name="test",
                namespaced_name="GRAPH__test",
                description="test",
                input_schema={},
            )
        }

        loader.disconnect_all()

        assert loader._tools_cache == {}

    def test_execute_fs_tool_path_traversal(self):
        """Test that _execute_fs_tool blocks path traversal attempts."""
        loader = MCPLoader([])
        # Use an input that exploits prefix startswith vulnerability
        # E.g., if base_path is /tmp/agent-workspace,
        # /tmp/agent-workspace-secret starts with /tmp/agent-workspace
        input_args = {"path": "../agent-workspace-secret/secret.txt"}

        result = loader._execute_fs_tool("read_file", input_args)

        assert "error" in result
        assert result["error"] == "Path must be within agent workspace"
