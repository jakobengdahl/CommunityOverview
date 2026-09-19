"""
Tests for MCP loader and tool namespacing.
"""

import json
from unittest.mock import Mock, patch

from backend.agents.config import MCPIntegration, MCPTransport
from backend.agents.mcp_loader import MAX_REDIRECTS, MCPLoader, NamespacedTool


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


class TestConnectHttpInfoQuery:
    """Tests for the HTTP info-endpoint tool-discovery path."""

    @patch("backend.agents.mcp_loader.httpx.get")
    def test_info_endpoint_non_json_body_is_swallowed(self, mock_get):
        """A 200 response with a non-JSON body must not raise.

        httpx surfaces a JSON decode failure as a plain ValueError, which — unlike
        requests' JSONDecodeError (a RequestException) — is not an httpx.RequestError.
        The handler must still swallow-and-log it so tool discovery degrades to an
        empty list instead of propagating out of _connect_http.
        """
        response = Mock()
        response.status_code = 200
        response.json.side_effect = json.JSONDecodeError("no json", "<html>", 0)
        mock_get.return_value = response

        integration = MCPIntegration(
            id="WEB",
            name="Some HTTP MCP",
            transport=MCPTransport.HTTP,
            url="http://localhost:9999/mcp",
        )
        loader = MCPLoader([integration])

        tools = loader._connect_http(integration)

        assert tools == []

    def test_info_endpoint_malformed_url_is_swallowed(self):
        """A malformed configured URL must not escape _connect_http.

        httpx raises httpx.InvalidURL (neither a RequestError nor a ValueError) on
        a malformed URL, where requests folded MissingSchema/InvalidURL into
        RequestException. The handler must still degrade to an empty tool list.
        """
        integration = MCPIntegration(
            id="WEB",
            name="Some HTTP MCP",
            transport=MCPTransport.HTTP,
            url="http://[::1/mcp",  # unclosed IPv6 bracket -> httpx.InvalidURL
        )
        loader = MCPLoader([integration])

        tools = loader._connect_http(integration)

        assert tools == []


def _redirect_response(location, status_code=302):
    """A 3xx response pointing at *location*."""
    response = Mock()
    response.is_redirect = True
    response.status_code = status_code
    response.headers = {"location": location}
    response.text = "<html>redirecting</html>"
    return response


def _ok_response(text="<html>fetched</html>", status_code=200):
    """A terminal 2xx response carrying *text*."""
    response = Mock()
    response.is_redirect = False
    response.status_code = status_code
    response.headers = {}
    response.text = text
    response.raise_for_status = Mock()
    return response


def _client_returning(*responses):
    """A context-manager mock httpx.Client whose .get yields *responses* in order."""
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=None)
    if len(responses) == 1:
        client.get.return_value = responses[0]
    else:
        client.get.side_effect = list(responses)
    return client


def _public_addrinfo(*ips):
    return [(None, None, None, None, (ip, 0)) for ip in (ips or ("93.184.216.34",))]


class TestFetchToolSSRFGuard:
    """The WEB/fetch tool must not reach addresses the caller cannot reach itself.

    These pin the guard added in PR #597 against the scenarios it was written
    for. The guard itself lives in backend/core/events/delivery.py (is_safe_url)
    and is deliberately not mocked here — mocking it would leave the tests
    passing against a fetch tool that had stopped calling it.
    """

    @patch("backend.agents.mcp_loader.httpx.Client")
    def test_literal_internal_ip_is_rejected_before_any_request(self, mock_client_cls):
        """A literal private/loopback/link-local/CGNAT target never reaches the network."""
        loader = MCPLoader([])

        for url in (
            "http://127.0.0.1/admin",
            "http://localhost:8000/mcp",
            "http://10.0.0.1/internal",
            "http://192.168.1.1/router",
            "http://169.254.169.254/latest/meta-data/",
            "http://100.64.0.1/cgnat",
            "http://[::1]/admin",
            "http://[fc00::1]/internal",
        ):
            result = loader._execute_fetch_tool("fetch", {"url": url})

            assert "error" in result, url
            assert "content" not in result, url

        mock_client_cls.assert_not_called()

    @patch("backend.core.events.delivery.socket.getaddrinfo")
    @patch("backend.agents.mcp_loader.httpx.Client")
    def test_hostname_resolving_into_private_range_is_rejected(
        self, mock_client_cls, mock_getaddrinfo
    ):
        """A public-looking hostname whose DNS answer is internal is still blocked."""
        mock_getaddrinfo.return_value = _public_addrinfo("10.0.0.5")
        loader = MCPLoader([])

        result = loader._execute_fetch_tool(
            "fetch", {"url": "http://internal.example.com/secrets"}
        )

        assert "error" in result
        assert "content" not in result
        mock_client_cls.assert_not_called()

    @patch("backend.core.events.delivery.socket.getaddrinfo")
    @patch("backend.agents.mcp_loader.httpx.Client")
    def test_hostname_with_any_internal_address_is_rejected(
        self, mock_client_cls, mock_getaddrinfo
    ):
        """Resolution is fail-closed: one internal address among public ones blocks."""
        mock_getaddrinfo.return_value = _public_addrinfo("93.184.216.34", "fe80::1")
        loader = MCPLoader([])

        result = loader._execute_fetch_tool(
            "fetch", {"url": "http://dual.example.com/page"}
        )

        assert "error" in result
        mock_client_cls.assert_not_called()

    @patch("backend.agents.mcp_loader.httpx.Client")
    def test_non_http_scheme_is_rejected(self, mock_client_cls):
        """Only http(s) is fetchable — file:// and friends never reach the client."""
        loader = MCPLoader([])

        for url in ("file:///etc/passwd", "ftp://example.com/x", "javascript:alert(1)"):
            result = loader._execute_fetch_tool("fetch", {"url": url})

            assert "error" in result, url

        mock_client_cls.assert_not_called()

    @patch("backend.core.events.delivery.socket.getaddrinfo")
    @patch("backend.agents.mcp_loader.httpx.Client")
    def test_redirect_into_private_range_is_not_followed(
        self, mock_client_cls, mock_getaddrinfo
    ):
        """A public URL that redirects to an internal address must stop at the hop.

        The initial host passes the pre-request check, so only the per-hop
        re-validation can catch this one.
        """
        mock_getaddrinfo.return_value = _public_addrinfo()
        client = _client_returning(
            _redirect_response("http://169.254.169.254/latest/meta-data/")
        )
        mock_client_cls.return_value = client
        loader = MCPLoader([])

        result = loader._execute_fetch_tool(
            "fetch", {"url": "http://example.com/start"}
        )

        assert "error" in result
        assert "content" not in result
        # The internal hop was never requested — only the original URL was.
        assert client.get.call_count == 1
        assert client.get.call_args.args[0] == "http://example.com/start"

    @patch("backend.core.events.delivery.socket.getaddrinfo")
    @patch("backend.agents.mcp_loader.httpx.Client")
    def test_redirect_to_public_address_is_followed(
        self, mock_client_cls, mock_getaddrinfo
    ):
        """The guard must not break ordinary redirects to public addresses."""
        mock_getaddrinfo.return_value = _public_addrinfo()
        client = _client_returning(
            _redirect_response("http://example.com/final"),
            _ok_response("<html>final page</html>"),
        )
        mock_client_cls.return_value = client
        loader = MCPLoader([])

        result = loader._execute_fetch_tool(
            "fetch", {"url": "http://example.com/start"}
        )

        assert result["content"] == "<html>final page</html>"
        assert result["status"] == 200
        assert client.get.call_count == 2

    @patch("backend.core.events.delivery.socket.getaddrinfo")
    @patch("backend.agents.mcp_loader.httpx.Client")
    def test_redirect_chain_beyond_cap_errors_instead_of_returning_the_3xx_body(
        self, mock_client_cls, mock_getaddrinfo
    ):
        """An exhausted redirect chain is an error, not fetched content.

        The chain here is endless but every hop is public, so the SSRF check
        never fires; only the cap can end it. Falling out of the loop and
        returning the last 3xx response would hand the agent a redirect page
        as if it had been fetched.
        """
        mock_getaddrinfo.return_value = _public_addrinfo()
        client = _client_returning(_redirect_response("http://example.com/next"))
        mock_client_cls.return_value = client
        loader = MCPLoader([])

        result = loader._execute_fetch_tool(
            "fetch", {"url": "http://example.com/start"}
        )

        assert "error" in result
        assert "redirect" in result["error"].lower()
        assert "content" not in result
        assert "status" not in result
        assert client.get.call_count == MAX_REDIRECTS

    def test_redirect_cap_is_shared_with_the_other_outbound_fetch_paths(self):
        """One cap for every outbound path, so they cannot drift apart again."""
        from backend.core import image_ingest
        from backend.core.events import delivery

        assert MAX_REDIRECTS == delivery.MAX_REDIRECTS
        assert image_ingest.MAX_REDIRECTS == delivery.MAX_REDIRECTS
