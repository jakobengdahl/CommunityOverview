"""
Tests for MCP loader and tool namespacing.
"""

import ast
from pathlib import Path
from unittest.mock import patch

import httpx2 as httpx
import pytest

from backend.agents import mcp_loader
from backend.agents.config import MCPIntegration, MCPTransport
from backend.agents.mcp_loader import MAX_REDIRECTS, MCPLoader, NamespacedTool
from backend.core import image_ingest
from backend.core.events import delivery
from backend.skills import loader as skills_loader


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

    def test_execute_fs_tool_read_file_blocks_symlink_escape(self, tmp_path):
        """Test that read_file blocks symlinks pointing outside the workspace."""
        loader = MCPLoader([])
        workspace = Path("/tmp/agent-workspace")
        workspace.mkdir(exist_ok=True)
        outside_file = tmp_path / "outside.txt"
        outside_file.write_text("secret", encoding="utf-8")
        symlink_path = workspace / f"outside-read-{tmp_path.name}.txt"
        symlink_path.symlink_to(outside_file)

        try:
            result = loader._execute_fs_tool("read_file", {"path": symlink_path.name})
        finally:
            symlink_path.unlink(missing_ok=True)

        assert result == {"error": "Path must be within agent workspace"}

    def test_execute_fs_tool_write_file_blocks_symlink_escape(self, tmp_path):
        """Test that write_file blocks symlinks pointing outside the workspace."""
        loader = MCPLoader([])
        workspace = Path("/tmp/agent-workspace")
        workspace.mkdir(exist_ok=True)
        outside_file = tmp_path / "outside.txt"
        outside_file.write_text("original", encoding="utf-8")
        symlink_path = workspace / f"outside-write-{tmp_path.name}.txt"
        symlink_path.symlink_to(outside_file)

        try:
            result = loader._execute_fs_tool(
                "write_file",
                {"path": symlink_path.name, "content": "modified"},
            )
        finally:
            symlink_path.unlink(missing_ok=True)

        assert result == {"error": "Path must be within agent workspace"}
        assert outside_file.read_text(encoding="utf-8") == "original"


class _StubHttpxModule:
    """Stands in for the module-level `httpx` name in mcp_loader.py.

    Both redirect walkers build their own `httpx.Client()`, so replacing the
    module attribute is the only way to bind a mock transport without opening
    a real socket. Binding it here rather than @patch-ing the shared `httpx2`
    module object keeps one test from mutating global state another worker is
    reading under pytest-xdist (mirrors backend/core/tests/test_image_ingest.py
    and the webhook tests in backend/core/events/tests/test_delivery.py).

    `RequestError` and `InvalidURL` are forwarded because `_connect_http`
    names them in its `except` clause; a stub that dropped them would turn a
    caught error into an AttributeError. `HTTPError` is not named anywhere in
    mcp_loader.py -- it is forwarded only so the stub stays usable if a future
    walker reaches for it.
    """

    RequestError = httpx.RequestError
    InvalidURL = httpx.InvalidURL
    HTTPError = httpx.HTTPError

    def __init__(self, transport):
        self._transport = transport

    def Client(self, **kwargs):
        return httpx.Client(transport=self._transport, **kwargs)


def _install_transport(monkeypatch, handler):
    """Bind `handler` as mcp_loader's transport and record what it requested.

    The handler is called with (request, index) so a test can script a chain
    by hop position. Returns the list the requested URLs accumulate into.
    """
    seen = []

    def recording(request):
        seen.append(str(request.url))
        return handler(request, len(seen) - 1)

    monkeypatch.setattr(
        mcp_loader, "httpx", _StubHttpxModule(httpx.MockTransport(recording))
    )
    return seen


def _addrinfo(*ips):
    """getaddrinfo answers; defaults to a single public address."""
    return [(None, None, None, None, (ip, 0)) for ip in (ips or ("93.184.216.34",))]


@pytest.fixture
def public_dns(monkeypatch):
    """Resolve every hostname in these tests to a public address.

    is_safe_url is deliberately not mocked -- mocking it would leave the tests
    passing against a caller that had stopped calling it -- so DNS is stubbed
    instead. Without this a scheme test passes offline for the wrong reason:
    `ftp://example.com/x` is refused because the name does not resolve, not
    because the scheme was rejected, so a guard that had dropped its scheme
    check would still look green.
    """
    monkeypatch.setattr(
        "backend.core.events.delivery.socket.getaddrinfo",
        lambda *args, **kwargs: _addrinfo(),
    )


def _redirect(location, status_code=302):
    return httpx.Response(status_code, headers={"location": location})


def _recording_body(payload, reads, label):
    """A response body that appends `label` to `reads` when it is iterated.

    httpx only iterates a response's stream when the body is actually pulled
    off the wire: client.get() always does, client.stream() only if the
    caller reads it. Building the fixture with `text=`/`content=` instead
    would make `.content` readable either way and measure nothing.
    """

    class _Stream(httpx.SyncByteStream):
        def __iter__(self):
            reads.append(label)
            yield payload

    return _Stream()


INTERNAL = "http://169.254.169.254/latest/meta-data/"


class TestConnectHttpInfoQuery:
    """Tests for the HTTP info-endpoint tool-discovery path."""

    def _integration(self, url="http://example.com/mcp"):
        return MCPIntegration(
            id="WEB",
            name="Some HTTP MCP",
            transport=MCPTransport.HTTP,
            url=url,
        )

    def test_info_endpoint_non_json_body_is_swallowed(self, monkeypatch, public_dns):
        """A 200 response with a non-JSON body must not raise.

        httpx surfaces a JSON decode failure as a plain ValueError, which -- unlike
        requests' JSONDecodeError (a RequestException) -- is not an httpx.RequestError.
        The handler must still swallow-and-log it so tool discovery degrades to an
        empty list instead of propagating out of _connect_http.
        """
        _install_transport(
            monkeypatch, lambda request, index: httpx.Response(200, text="<html>")
        )
        integration = self._integration()

        assert MCPLoader([integration])._connect_http(integration) == []

    def test_info_endpoint_malformed_url_is_swallowed(self, monkeypatch):
        """A malformed configured URL must not escape _connect_http.

        It is now refused by the pre-request is_safe_url check rather than by
        httpx: urlparse raises ValueError on the unclosed bracket and
        is_safe_url catches that fail-closed. Either way _connect_http must
        degrade to an empty tool list, and the transport asserts that no
        request was attempted.
        """

        def handler(request, index):  # pragma: no cover - must not be reached
            raise AssertionError(
                f"a malformed URL must not be requested: {request.url}"
            )

        seen = _install_transport(monkeypatch, handler)
        integration = self._integration("http://[::1/mcp")

        assert MCPLoader([integration])._connect_http(integration) == []
        assert seen == []

    def test_info_endpoint_url_httpx_rejects_is_swallowed(self, monkeypatch):
        """is_safe_url and httpx disagree about which URLs are malformed.

        "http://example.com:abc/info" has a hostname that parses and resolves,
        and is_safe_url never looks at the port, so it returns True and the
        walk proceeds -- only for httpx to raise InvalidURL when it builds the
        request. InvalidURL is not a ValueError subclass, so dropping it from
        _connect_http's except tuple makes that escape to the caller. This
        pins the clause the malformed-IPv6 test above no longer reaches.
        """
        monkeypatch.setattr(
            "backend.core.events.delivery.socket.getaddrinfo",
            lambda *args, **kwargs: _addrinfo(),
        )

        def handler(request, index):  # pragma: no cover - httpx refuses first
            raise AssertionError(
                f"unparseable URL must not be requested: {request.url}"
            )

        seen = _install_transport(monkeypatch, handler)
        integration = self._integration("http://example.com:abc/mcp")

        assert MCPLoader([integration])._connect_http(integration) == []
        assert seen == []

    def test_info_endpoint_discovers_graph_tools_on_a_safe_url(
        self, monkeypatch, public_dns
    ):
        """Positive control: the guard must not break ordinary discovery.

        Without this every refusal test below would still pass against a
        _connect_http that refused everything.
        """
        _install_transport(
            monkeypatch,
            lambda request, index: httpx.Response(200, json={"endpoints": ["/mcp"]}),
        )
        integration = self._integration()

        tools = MCPLoader([integration])._connect_http(integration)

        assert [tool.original_name for tool in tools]

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/mcp",
            "http://10.0.0.1/mcp",
            "http://192.168.1.1/mcp",
            "http://169.254.169.254/mcp",
            "http://100.64.0.1/mcp",
            "http://[::1]/mcp",
            "http://[fc00::1]/mcp",
        ],
    )
    def test_info_endpoint_literal_internal_ip_never_reaches_the_network(
        self, monkeypatch, url
    ):
        """The initial info URL is checked before any request is made."""

        def handler(request, index):  # pragma: no cover - must not be reached
            raise AssertionError(f"internal address requested: {request.url}")

        seen = _install_transport(monkeypatch, handler)
        integration = self._integration(url)

        assert MCPLoader([integration])._connect_http(integration) == []
        assert seen == []

    def test_info_endpoint_hostname_resolving_internally_is_rejected(self, monkeypatch):
        """A public-looking hostname whose DNS answer is internal is blocked."""
        monkeypatch.setattr(
            "backend.core.events.delivery.socket.getaddrinfo",
            lambda *args, **kwargs: _addrinfo("10.0.0.5"),
        )

        def handler(request, index):  # pragma: no cover - must not be reached
            raise AssertionError(f"internal address requested: {request.url}")

        seen = _install_transport(monkeypatch, handler)
        integration = self._integration("http://internal.example.com/mcp")

        assert MCPLoader([integration])._connect_http(integration) == []
        assert seen == []

    @pytest.mark.parametrize("internal_hop", [0, 1, 2, MAX_REDIRECTS - 2])
    def test_info_endpoint_every_redirect_hop_is_revalidated(
        self, monkeypatch, public_dns, internal_hop
    ):
        """The internal address is refused wherever it sits in the chain.

        Pinning only hops 0 and 1 leaves a guard that checks the first two
        targets and then stops passing the suite while it walks an internal
        address in at hop 2.
        """

        def handler(request, index):
            if index < internal_hop:
                return _redirect(f"http://hop{index + 1}.example.com/info")
            if index == internal_hop:
                return _redirect(INTERNAL)
            raise AssertionError(f"unexpected request {request.url}")

        seen = _install_transport(monkeypatch, handler)
        integration = self._integration()

        assert MCPLoader([integration])._connect_http(integration) == []
        assert len(seen) == internal_hop + 1
        assert all("169.254.169.254" not in url for url in seen)

    def test_info_endpoint_relative_location_resolves_against_the_current_url(
        self, monkeypatch, public_dns
    ):
        """urljoin's base is the CURRENT url, not the one discovery started at.

        The chain changes host first, so resolving the relative Location
        against the original URL would request a different, wrong address --
        which a same-host chain could not tell apart.
        """

        def handler(request, index):
            if index == 0:
                return _redirect("http://cdn.example.com/api/")
            if index == 1:
                return _redirect("info", status_code=301)
            return httpx.Response(200, json={"endpoints": ["/mcp"]})

        seen = _install_transport(monkeypatch, handler)
        integration = self._integration()

        tools = MCPLoader([integration])._connect_http(integration)

        assert [tool.original_name for tool in tools]
        assert seen == [
            "http://example.com/info",
            "http://cdn.example.com/api/",
            "http://cdn.example.com/api/info",
        ]

    @pytest.mark.parametrize(
        "location",
        ["http://example.com/info", "/info", "info"],
        ids=["absolute", "root-relative", "path-relative"],
    )
    @pytest.mark.parametrize("status_code", [301, 302, 307])
    def test_info_endpoint_redirect_cap_is_the_shared_limit(
        self, monkeypatch, public_dns, location, status_code
    ):
        """The cap bounds the walk whatever flavour of hop the server sends.

        A counter that only advanced on one flavour -- absolute Locations, or
        one status code -- would be unbounded on the others.
        """
        seen = _install_transport(
            monkeypatch, lambda request, index: _redirect(location, status_code)
        )
        integration = self._integration()

        assert MCPLoader([integration])._connect_http(integration) == []
        assert len(seen) == delivery.MAX_REDIRECTS

    @pytest.mark.parametrize("headers", [{}, {"location": ""}], ids=["absent", "empty"])
    def test_info_endpoint_redirect_without_a_location_is_refused(
        self, monkeypatch, public_dns, headers
    ):
        """urljoin("", current) is current, so an empty Location used to
        re-request the same URL until the cap ran out."""
        seen = _install_transport(
            monkeypatch, lambda request, index: httpx.Response(302, headers=headers)
        )
        integration = self._integration()

        assert MCPLoader([integration])._connect_http(integration) == []
        assert len(seen) == 1

    def test_info_endpoint_redirect_bodies_are_never_pulled_off_the_wire(
        self, monkeypatch, public_dns
    ):
        """A bounded walk still pulls MAX_REDIRECTS bodies if each is buffered.

        Streaming is what keeps them out of memory. The observable is which
        response streams the walk actually iterates: client.get() reads every
        one, client.stream() only the body it goes on to read.
        """
        reads = []

        def handler(request, index):
            if index == 0:
                return httpx.Response(
                    302,
                    headers={"location": "http://cdn.example.com/x"},
                    stream=_recording_body(b"redirect filler", reads, "redirect"),
                )
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                stream=_recording_body(b'{"endpoints": ["/mcp"]}', reads, "terminal"),
            )

        _install_transport(monkeypatch, handler)
        integration = self._integration()

        tools = MCPLoader([integration])._connect_http(integration)

        assert [tool.original_name for tool in tools]
        assert reads == ["terminal"]


class TestFetchToolSSRFGuard:
    """The WEB/fetch tool must not reach addresses the caller cannot reach itself.

    These pin the guard added in PR #597 against the scenarios it was written
    for. The guard itself lives in backend/core/events/delivery.py (is_safe_url)
    and is deliberately not mocked here -- mocking it would leave the tests
    passing against a fetch tool that had stopped calling it.
    """

    def _fetch(self, url="http://example.com/start", **args):
        return MCPLoader([])._execute_fetch_tool("fetch", {"url": url, **args})

    def test_fetches_a_safe_url(self, monkeypatch, public_dns):
        """Positive control: the guard must not break an ordinary fetch."""
        _install_transport(
            monkeypatch, lambda request, index: httpx.Response(200, text="<html>ok")
        )

        assert self._fetch() == {
            "url": "http://example.com/start",
            "status": 200,
            "content": "<html>ok",
        }

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/admin",
            "http://10.0.0.1/internal",
            "http://192.168.1.1/router",
            "http://169.254.169.254/latest/meta-data/",
            "http://100.64.0.1/cgnat",
            "http://[::1]/admin",
            "http://[fc00::1]/internal",
        ],
    )
    def test_literal_internal_ip_is_rejected_before_any_request(self, monkeypatch, url):
        """A literal private/loopback/link-local/CGNAT target never reaches the network."""

        def handler(request, index):  # pragma: no cover - must not be reached
            raise AssertionError(f"internal address requested: {request.url}")

        seen = _install_transport(monkeypatch, handler)

        result = self._fetch(url)

        assert "error" in result
        assert "content" not in result
        assert seen == []

    def test_hostname_resolving_into_private_range_is_rejected(self, monkeypatch):
        """A public-looking hostname whose DNS answer is internal is still blocked."""
        monkeypatch.setattr(
            "backend.core.events.delivery.socket.getaddrinfo",
            lambda *args, **kwargs: _addrinfo("10.0.0.5"),
        )

        def handler(request, index):  # pragma: no cover - must not be reached
            raise AssertionError(f"internal address requested: {request.url}")

        seen = _install_transport(monkeypatch, handler)

        result = self._fetch("http://internal.example.com/secrets")

        assert "error" in result
        assert "content" not in result
        assert seen == []

    def test_hostname_with_any_internal_address_is_rejected(self, monkeypatch):
        """Resolution is fail-closed: one internal address among public ones blocks."""
        monkeypatch.setattr(
            "backend.core.events.delivery.socket.getaddrinfo",
            lambda *args, **kwargs: _addrinfo("93.184.216.34", "fe80::1"),
        )

        def handler(request, index):  # pragma: no cover - must not be reached
            raise AssertionError(f"internal address requested: {request.url}")

        seen = _install_transport(monkeypatch, handler)

        result = self._fetch("http://dual.example.com/page")

        assert "error" in result
        assert seen == []

    @pytest.mark.parametrize(
        "url",
        ["file:///etc/passwd", "ftp://example.com/x", "javascript:alert(1)"],
    )
    def test_non_http_scheme_is_rejected(self, monkeypatch, public_dns, url):
        """Only http(s) is fetchable -- file:// and friends never reach the client.

        DNS is stubbed public (see the fixture), so `ftp://example.com/x` can
        only fail on its scheme. Without that stub it would fail offline
        because the name does not resolve, and a tool that had dropped its
        scheme check would still pass.
        """

        def handler(request, index):  # pragma: no cover - must not be reached
            raise AssertionError(f"non-http scheme requested: {request.url}")

        seen = _install_transport(monkeypatch, handler)

        result = self._fetch(url)

        assert "error" in result
        assert "content" not in result
        assert seen == []

    def test_redirect_into_private_range_is_not_followed(self, monkeypatch, public_dns):
        """A public URL that redirects to an internal address must stop at the hop.

        The initial host passes the pre-request check, so only the per-hop
        re-validation can catch this one.
        """
        seen = _install_transport(
            monkeypatch, lambda request, index: _redirect(INTERNAL)
        )

        result = self._fetch()

        assert result == {"error": "Redirected to unsafe URL"}
        assert seen == ["http://example.com/start"]

    def test_redirect_to_public_address_is_followed(self, monkeypatch, public_dns):
        """The guard must not break ordinary redirects to public addresses."""

        def handler(request, index):
            if index == 0:
                return _redirect("http://example.com/final")
            return httpx.Response(200, text="<html>final page</html>")

        seen = _install_transport(monkeypatch, handler)

        result = self._fetch()

        assert result["content"] == "<html>final page</html>"
        assert result["status"] == 200
        assert len(seen) == 2

    @pytest.mark.parametrize("internal_hop", [0, 1, 2, MAX_REDIRECTS - 2])
    def test_every_redirect_hop_is_revalidated(
        self, monkeypatch, public_dns, internal_hop
    ):
        """The internal address is refused wherever it sits in the chain.

        A guard that validated only the first one or two redirect targets
        would pass the single-hop test above and still walk a longer
        public -> public -> internal chain all the way in.
        """

        def handler(request, index):
            if index < internal_hop:
                return _redirect(f"http://hop{index + 1}.example.com/p")
            if index == internal_hop:
                return _redirect(INTERNAL)
            raise AssertionError(f"unexpected request {request.url}")

        seen = _install_transport(monkeypatch, handler)

        result = self._fetch()

        assert result == {"error": "Redirected to unsafe URL"}
        assert len(seen) == internal_hop + 1
        assert all("169.254.169.254" not in url for url in seen)

    def test_relative_location_resolves_against_the_current_url(
        self, monkeypatch, public_dns
    ):
        """urljoin's base is the CURRENT url, not the one the fetch started at.

        The chain changes host first, so resolving against the original URL
        would request a different address -- which a same-host chain could
        not distinguish.
        """

        def handler(request, index):
            if index == 0:
                return _redirect("http://cdn.example.com/docs/")
            if index == 1:
                return _redirect("final", status_code=301)
            return httpx.Response(200, text="<html>final page</html>")

        seen = _install_transport(monkeypatch, handler)

        result = self._fetch()

        assert result["content"] == "<html>final page</html>"
        assert seen == [
            "http://example.com/start",
            "http://cdn.example.com/docs/",
            "http://cdn.example.com/docs/final",
        ]

    @pytest.mark.parametrize(
        "location",
        ["http://example.com/next", "/next", "next"],
        ids=["absolute", "root-relative", "path-relative"],
    )
    @pytest.mark.parametrize("status_code", [301, 302, 307])
    def test_redirect_chain_stops_at_the_shared_cap_with_an_explicit_error(
        self, monkeypatch, public_dns, location, status_code
    ):
        """An exhausted redirect chain reports the limit it hit, on every flavour.

        The chain here is endless but every hop is public, so the SSRF check
        never fires; only the cap can end it. Parametrising the Location form
        and the status code catches a counter that advanced on one flavour of
        hop and left the others unbounded. The count is taken against
        delivery.py's constant, the one all four walkers import.
        """
        seen = _install_transport(
            monkeypatch, lambda request, index: _redirect(location, status_code)
        )

        result = self._fetch()

        assert result == {"error": f"Too many redirects (limit {MAX_REDIRECTS})"}
        assert len(seen) == delivery.MAX_REDIRECTS

    @pytest.mark.parametrize("headers", [{}, {"location": ""}], ids=["absent", "empty"])
    def test_a_redirect_without_a_location_is_refused_not_spun_to_the_cap(
        self, monkeypatch, public_dns, headers
    ):
        """urljoin of an empty Location is the current URL, so it used to be
        re-requested until the redirect cap ran out."""
        seen = _install_transport(
            monkeypatch, lambda request, index: httpx.Response(302, headers=headers)
        )

        result = self._fetch()

        assert result == {"error": "Redirect without a Location header"}
        assert len(seen) == 1

    def test_body_over_max_length_is_truncated(self, monkeypatch, public_dns):
        """The size cap on the returned content still applies after the rewrite."""
        _install_transport(
            monkeypatch, lambda request, index: httpx.Response(200, text="x" * 50)
        )

        result = self._fetch(max_length=10)

        assert result["content"] == "x" * 10 + "... (truncated)"

    def test_redirect_bodies_are_never_pulled_off_the_wire(
        self, monkeypatch, public_dns
    ):
        """A bounded walk still pulls MAX_REDIRECTS bodies if each is buffered.

        Streaming is what keeps them out of memory. The observable is which
        response streams the walk actually iterates: client.get() reads every
        one, client.stream() only the body it goes on to read. The terminal
        read is asserted too, so a tool that streamed and then never read
        anything could not pass this by returning nothing.
        """
        reads = []

        def handler(request, index):
            if index == 0:
                return httpx.Response(
                    302,
                    headers={"location": "http://example.com/final"},
                    stream=_recording_body(b"y" * 10_000, reads, "redirect"),
                )
            return httpx.Response(
                200,
                stream=_recording_body(b"<html>final page</html>", reads, "terminal"),
            )

        _install_transport(monkeypatch, handler)

        result = self._fetch()

        assert result["content"] == "<html>final page</html>"
        assert reads == ["terminal"]


class TestRedirectCapIsShared:
    """One cap for the four hop-validating paths, so they cannot drift again."""

    def test_every_walker_agrees_on_the_value(self):
        assert MAX_REDIRECTS == delivery.MAX_REDIRECTS
        assert image_ingest.MAX_REDIRECTS == delivery.MAX_REDIRECTS
        assert skills_loader.MAX_REDIRECTS == delivery.MAX_REDIRECTS

    @pytest.mark.parametrize(
        "module",
        [mcp_loader, image_ingest, skills_loader],
        ids=lambda module: module.__name__.rsplit(".", 1)[-1],
    )
    def test_every_walker_imports_the_cap_rather_than_restating_it(self, module):
        """Comparing values alone passes a module that redefined the same number.

        A local `MAX_REDIRECTS = 10` is equal to delivery's today and drifts
        the moment delivery's changes -- exactly the drift the shared constant
        was introduced to end. So this reads the source: the name must arrive
        by import from delivery, and must never be assigned in the module.
        The module is matched on the suffix because image_ingest.py reaches
        delivery by a relative import (`from .events.delivery import ...`).
        """
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))

        imported_from_delivery = any(
            isinstance(node, ast.ImportFrom)
            and (node.module or "").endswith("events.delivery")
            and any(alias.name == "MAX_REDIRECTS" for alias in node.names)
            for node in ast.walk(tree)
        )
        assigned_locally = any(
            isinstance(node, (ast.Assign, ast.AnnAssign))
            and any(
                isinstance(target, ast.Name) and target.id == "MAX_REDIRECTS"
                for target in (
                    node.targets if isinstance(node, ast.Assign) else [node.target]
                )
            )
            for node in ast.walk(tree)
        )

        assert imported_from_delivery
        assert not assigned_locally
