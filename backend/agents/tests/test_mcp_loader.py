"""
Tests for MCP loader and tool namespacing.
"""

import ast
import json
import logging
import urllib.parse
from pathlib import Path
from unittest.mock import patch

import httpx2 as httpx
import pytest

from backend.agents import mcp_loader
from backend.agents.config import AgentsSettings, MCPIntegration, MCPTransport
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

    def get(self, *args, **kwargs):
        """Fail loudly rather than leaving an unbound call to look like a product error.

        _execute_search_tool calls httpx.get, which this stub does not route
        through the mock transport. No test reaches it today, but that tool
        wraps everything in a broad `except`, so an AttributeError from a
        missing stub attribute would come back as a plausible-looking
        {"error": ...} dict and the test would read as a product failure
        rather than a harness gap.
        """
        raise NotImplementedError(
            "httpx.get is not routed through this stub's transport; add it to "
            "_StubHttpxModule before testing a walker that calls it"
        )


def _install_transport(monkeypatch, handler, client_kwargs=None):
    """Bind `handler` as mcp_loader's transport and record what it requested.

    The handler is called with (request, index) so a test can script a chain
    by hop position. Returns the list the requested URLs accumulate into.
    With `client_kwargs`, every kwarg dict the walker passes to httpx.Client
    is appended to it, so a test can pin the client's own configuration --
    otherwise nothing stops the timeout from growing without bound.
    """
    seen = []

    def recording(request):
        seen.append(str(request.url))
        return handler(request, len(seen) - 1)

    class _Stub(_StubHttpxModule):
        def Client(self, **kwargs):
            if client_kwargs is not None:
                client_kwargs.append(dict(kwargs))
            return super().Client(**kwargs)

    monkeypatch.setattr(mcp_loader, "httpx", _Stub(httpx.MockTransport(recording)))
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


# Every status that carries a redirect Location. The hop guards sit inside
# `if response.is_redirect:`, which spans the whole 3xx range, so a guard made
# conditional on the status is a one-word edit -- and a suite whose safety
# tests all script 302 cannot see it. The cap tests already vary this; these
# are the tests that assert a hop is REFUSED, which matters more.
# 300/304/305/399 are included because `is_redirect` is status-only over the
# whole 3xx range: each of them reaches the hop guard, and a 300 may carry a
# Location legitimately. Pinning only the five conventional ones left a quarter
# of the range the guard actually spans unexercised.
REDIRECT_STATUSES = [300, 301, 302, 303, 304, 305, 307, 308, 399]

# The context a hop is judged IN, as opposed to the target being judged.
# INTERNAL_TARGETS and REDIRECT_STATUSES vary what the guard looks at; these
# vary the situation it looks from, so a guard made conditional on the current
# scheme or port -- the same one-word edit -- cannot hide behind a suite that
# always starts on plain http and a default port.
START_CONTEXTS = ["http://example.com", "https://example.com:8443"]


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


# The SHAPE of a hop target, not just its scheme. is_safe_url resolves
# whatever urljoin produces, but a guard made conditional on any surface
# property of the Location string -- "does it contain ://", "does it contain
# [", "does it contain @" -- is a one-word edit, and a suite that only ever
# sends one shape cannot see it. The initial-URL tests already vary this
# richly; these are the same forms carried onto the hop axis, which is where
# the far end chooses the string.
INTERNAL_TARGETS = [
    "http://169.254.169.254/latest/meta-data/",
    "https://169.254.169.254/latest/meta-data/",
    # no "://" at all, but urljoin makes it absolute onto the metadata address
    "//169.254.169.254/latest/meta-data/",
    "http://[::1]/latest/meta-data/",
    "https://[fc00::1]/latest/meta-data/",
    # urlparse discards the userinfo; the host is still link-local
    "http://anything@169.254.169.254/latest/meta-data/",
]

# Hostnames INTERNAL_TARGETS resolve to once urljoin has done its work. Keying
# the "never requested" assertion off this rather than off a literal IPv4
# string keeps it honest for the IPv6 and userinfo forms.
INTERNAL_HOSTS = {"169.254.169.254", "::1", "fc00::1"}

# is_safe_url's verdict has two halves -- scheme and address -- and every entry
# in INTERNAL_TARGETS is http or https, so the HOP axis exercised only the
# address half. A guard rebuilt as is_safe_url("https://" + netloc), which
# keeps the host check and discards the scheme, survives that whole set. The
# host here resolves (under the public_dns fixture) precisely so the refusal
# has to come from the scheme: file:// and javascript: have an empty netloc, so
# both the real guard and that mutant refuse them, which is why the
# initial-URL scheme tests do not transfer to this axis.
NON_HTTP_HOP_TARGETS = ["ftp://example.com/x", "gopher://example.com/1"]


class TestReadCapped:
    """Direct unit tests for the shared capped reader.

    The integration tests reach it only through a call site, which decides
    what is observable. Their oversize fixtures are derived from the cap
    constants and assert an upper bound on bytes pulled, so they cannot see
    the boundary (a body of exactly the cap) or how many bytes came back at
    all -- returning b"" on the over-cap path satisfied every one of them.

    A broken join they do catch, but asymmetrically, and only by accident of
    the payloads. Measured with this class deselected: returning only the
    FIRST chunk fails exactly one test (the within-cap info fixture, on
    invalid JSON), while dropping the LAST fails 38 -- 20 of them in
    TestFetchToolSSRFGuard -- because almost every other fixture is a single
    chunk, where dropping "the last" drops the whole body. So the wide
    failure count comes from bodies that are not chunked at all, not from any
    assertion that the bytes were reassembled. This class pins the join
    directly instead.
    """

    class _Stream:
        """Minimal stand-in: _read_capped only calls response.iter_bytes()."""

        def __init__(self, chunks):
            self._chunks = chunks
            self.pulled = 0

        def iter_bytes(self):
            for chunk in self._chunks:
                self.pulled += len(chunk)
                yield chunk

    @pytest.mark.parametrize(
        ("size", "expect_over_cap"),
        [(99, False), (100, False), (101, True), (500, True)],
    )
    def test_the_boundary_belongs_to_the_accepted_side(self, size, expect_over_cap):
        """`>=` instead of `>` would report a body of exactly the cap as over it.

        At the two call sites that means refusing an info document of exactly
        MAX_INFO_BODY_BYTES, and stamping a truncation marker on a page of
        exactly MAX_FETCH_BODY_BYTES. The equivalent boundary in
        skills/loader.py is pinned; this is the one that was not.
        """
        stream = self._Stream([b"x" * size])

        raw, over_cap = mcp_loader._read_capped(stream, 100)

        assert over_cap is expect_over_cap
        assert raw == b"x" * min(size, 100)

    def test_an_over_cap_read_returns_exactly_the_cap(self):
        """Pins the LENGTH, not just the flag.

        A reader that returned b"" with over_cap=True satisfied every existing
        assertion: the fetch tool's test checks the truncation marker and the
        chunk counter, and neither notices that the page itself was discarded.
        """
        stream = self._Stream([b"y" * 40] * 10)

        raw, over_cap = mcp_loader._read_capped(stream, 100)

        assert over_cap is True
        assert len(raw) == 100
        assert raw == b"y" * 100

    def test_a_within_cap_body_is_joined_across_every_chunk(self):
        """Pins the join directly, on the reader rather than through a caller.

        Returning only the first chunk is invisible everywhere else except the
        within-cap info fixture, and there only because a truncated JSON
        document fails to parse -- nothing asserts the bytes were reassembled.
        Dropping the last chunk is caught widely (38 tests, 20 of them in the
        fetch class) but for a reason that says nothing about joining: those
        fixtures are single-chunk, so chunks[:-1] is empty. Multi-chunk
        within-cap bodies exist only here and in the info fixture.
        """
        chunks = [b"a" * 10, b"b" * 10, b"c" * 10, b"d" * 5]
        stream = self._Stream(chunks)

        raw, over_cap = mcp_loader._read_capped(stream, 100)

        assert over_cap is False
        assert raw == b"".join(chunks)
        assert len(raw) == 35

    def test_reading_stops_at_the_crossing_chunk(self):
        """The cap must bound what comes OFF THE WIRE, not just what is returned.

        A reader that drained the stream and then sliced would satisfy the
        length assertions above while still letting the server decide the
        memory.
        """
        stream = self._Stream([b"z" * 30] * 100)

        raw, over_cap = mcp_loader._read_capped(stream, 100)

        assert over_cap is True
        assert len(raw) == 100
        # Four chunks of 30 reach 120, which is the first total past 100.
        assert stream.pulled == 120

    def test_an_empty_body_is_not_over_the_cap(self):
        stream = self._Stream([])

        assert mcp_loader._read_capped(stream, 100) == (b"", False)


class TestBodyCapConstants:
    """The two caps are asserted by value because every test that uses them
    derives its fixture from them, and so cannot notice the numbers changing.

    Swapping them is now also caught by
    test_a_page_between_the_two_caps_is_returned_whole, which fails at its own
    precondition (it asserts the fetch cap is the larger of the two) -- that
    test was added in the same commit as this class and is the reason the
    earlier wording here, "survives the whole suite otherwise", is no longer
    true. What this class adds is the VALUES: without it both constants could
    move together, keeping their ordering and that precondition intact, and
    nothing would notice.
    """

    def test_the_info_cap_is_one_mebibyte(self):
        assert mcp_loader.MAX_INFO_BODY_BYTES == 1024 * 1024

    def test_the_fetch_cap_is_ten_mebibytes(self):
        assert mcp_loader.MAX_FETCH_BODY_BYTES == 10 * 1024 * 1024

    def test_a_web_page_is_allowed_more_than_an_info_document(self):
        """The ordering is the part the rationale actually rests on."""
        assert mcp_loader.MAX_FETCH_BODY_BYTES > mcp_loader.MAX_INFO_BODY_BYTES


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

        The configured URL is no longer address-checked, so is_safe_url is not
        consulted here at all -- httpx refuses the URL when it builds the
        request, and httpx.InvalidURL is not a ValueError subclass. (httpx2
        reads the unclosed bracket as a port, so it reports Invalid port
        ":1" rather than diagnosing the bracket.) _connect_http must still
        degrade to an empty tool list, and the transport asserts no request
        reached the network.
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
        """A second URL shape httpx cannot build a request from.

        Not a second httpx code path: httpx2 rejects this and the unclosed
        bracket above at the same raise site, both as "Invalid port". It is a
        second way an operator can mistype the configured URL, and neither
        consults is_safe_url, which is not applied to that URL. What both pin
        is the httpx.InvalidURL entry in _connect_http's except tuple -- it is
        not a ValueError subclass, so removing it makes the error escape to
        the caller.
        """

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
            "http://127.0.0.1:8000/mcp",
            "http://localhost:8000/mcp/sse",
            "http://10.0.0.1/mcp",
            "http://[::1]:8000/mcp",
        ],
    )
    def test_info_endpoint_operator_configured_internal_address_is_requested(
        self, monkeypatch, url
    ):
        """The CONFIGURED address is deliberately not address-checked.

        An MCP server on localhost or a private network is the normal
        deployment, and the shipped default GRAPH integration is exactly
        that. is_safe_url is the guard against a server steering this
        request somewhere the operator did not choose -- it is not a policy
        about where the operator may run their own server. Checking the
        initial URL here refused the default install's own info endpoint.
        """
        seen = _install_transport(
            monkeypatch,
            lambda request, index: httpx.Response(200, json={"endpoints": ["/mcp"]}),
        )
        integration = self._integration(url)

        tools = MCPLoader([integration])._connect_http(integration)

        assert [tool.original_name for tool in tools]
        assert len(seen) == 1

    def test_the_shipped_default_graph_integration_discovers_through_info(
        self, monkeypatch
    ):
        """The default config must reach tools through /info, not the fallback.

        _connect_http ends with `if not tools and integration.id == "GRAPH"`,
        which would mask a broken discovery path for this one integration and
        for no other. Using a non-GRAPH id with the default's loopback URL
        removes that safety net, so this fails if the walk refuses its own
        configured address again.
        """
        default_graph = next(
            integration
            for integration in AgentsSettings._get_default_integrations()
            if integration.id == "GRAPH"
        )
        # Read the real default rather than restating it. PORT is env-driven,
        # so the expected URL is derived from it below, not written out.
        assert default_graph.url.startswith("http://localhost:"), default_graph.url

        seen = _install_transport(
            monkeypatch,
            lambda request, index: httpx.Response(200, json={"endpoints": ["/mcp"]}),
        )
        integration = self._integration(default_graph.url)

        tools = MCPLoader([integration])._connect_http(integration)

        assert [tool.original_name for tool in tools]
        expected_base = default_graph.url.replace("/sse", "").replace("/mcp", "")
        assert seen == [f"{expected_base}/info"]

    def test_graph_falls_back_to_its_known_tools_when_discovery_fails(
        self, monkeypatch, public_dns
    ):
        """The GRAPH fallback is what keeps the default install working.

        Every other test here uses id="WEB" precisely so the fallback cannot
        mask a discovery regression -- which left the fallback itself with no
        coverage at all. Deleting it would give the shipped GRAPH integration
        zero tools whenever /info is unreachable, which is its normal state
        before the graph service is up.
        """

        def handler(request, index):
            raise httpx.ConnectError("refused", request=request)

        _install_transport(monkeypatch, handler)
        integration = MCPIntegration(
            id="GRAPH",
            name="Graph API",
            transport=MCPTransport.HTTP,
            url="http://localhost:8000/mcp/sse",
        )

        tools = MCPLoader([integration])._connect_http(integration)

        assert [tool.original_name for tool in tools]

    def test_a_connection_is_recorded_even_when_nothing_is_discovered(
        self, monkeypatch, public_dns
    ):
        """_connections is the handle disconnect_all() and is_connected() use.

        An HTTP integration that discovers no tools is still a connection on
        main, so dropping the bookkeeping would silently orphan it.
        """
        _install_transport(
            monkeypatch, lambda request, index: httpx.Response(404, text="nope")
        )
        integration = self._integration()
        loader = MCPLoader([integration])

        assert loader._connect_http(integration) == []
        assert "WEB" in loader._connections

    def test_a_configured_internal_address_still_gets_its_hops_checked(
        self, monkeypatch
    ):
        """Trusting the configured address does not extend to where it sends us."""
        seen = _install_transport(
            monkeypatch, lambda request, index: _redirect(INTERNAL_TARGETS[0])
        )
        integration = self._integration("http://localhost:8000/mcp")

        assert MCPLoader([integration])._connect_http(integration) == []
        assert seen == ["http://localhost:8000/info"]

    def test_info_endpoint_configured_hostname_resolving_internally_is_requested(
        self, monkeypatch
    ):
        """Same decision by name: an operator's internal hostname is configuration.

        The fetch tool's equivalent test asserts the opposite, and that
        difference is the point -- its URL comes from the agent, this one from
        the operator.
        """
        monkeypatch.setattr(
            "backend.core.events.delivery.socket.getaddrinfo",
            lambda *args, **kwargs: _addrinfo("10.0.0.5"),
        )
        seen = _install_transport(
            monkeypatch,
            lambda request, index: httpx.Response(200, json={"endpoints": ["/mcp"]}),
        )
        integration = self._integration("http://mcp.internal.example.com/mcp")

        tools = MCPLoader([integration])._connect_http(integration)

        assert [tool.original_name for tool in tools]
        assert len(seen) == 1

    @pytest.mark.parametrize("status_code", [404, 500])
    def test_info_endpoint_non_200_is_not_trusted_and_is_requested_once(
        self, monkeypatch, public_dns, status_code
    ):
        """An error page that happens to carry an endpoints key is not discovery.

        Two separate ways to get this wrong: widening the status check (>= 200)
        makes a 404 body the tool list, and moving the loop's break inside the
        200 branch re-requests the same error page up to the redirect cap and
        then reports a redirect limit that was never hit.
        """
        seen = _install_transport(
            monkeypatch,
            lambda request, index: httpx.Response(
                status_code, json={"endpoints": ["/mcp"]}
            ),
        )
        integration = self._integration()

        assert MCPLoader([integration])._connect_http(integration) == []
        assert len(seen) == 1

    def test_info_endpoint_client_is_configured_with_the_expected_timeout(
        self, monkeypatch, public_dns
    ):
        """Discovery runs on a short timeout, and httpx must not follow hops.

        MAX_REDIRECTS hops at an unbounded timeout is a hang, not a fetch.
        """
        client_kwargs = []
        _install_transport(
            monkeypatch,
            lambda request, index: httpx.Response(200, json={"endpoints": ["/mcp"]}),
            client_kwargs=client_kwargs,
        )
        integration = self._integration()

        MCPLoader([integration])._connect_http(integration)

        assert len(client_kwargs) == 1
        assert client_kwargs[0]["timeout"] == 5
        assert client_kwargs[0]["follow_redirects"] is False

        # A subset assertion admits anything it does not name, so the keys that
        # would quietly undo the guard are asserted ABSENT rather than merely
        # unmentioned: verify=False drops certificate checking, proxy routes the
        # request somewhere else entirely, and trust_env picks a proxy up from
        # the environment.
        for forbidden in ("verify", "proxy", "proxies", "trust_env"):
            assert forbidden not in client_kwargs[0]

    @pytest.mark.parametrize("hop_target", NON_HTTP_HOP_TARGETS)
    @pytest.mark.parametrize("status_code", REDIRECT_STATUSES)
    def test_info_endpoint_a_non_http_hop_is_refused_and_never_requested(
        self, monkeypatch, public_dns, status_code, hop_target
    ):
        """Pins the SCHEME half of is_safe_url's verdict on the hop axis.

        The host resolves publicly here, so the address half of the guard is
        satisfied and only the scheme can refuse this. That distinguishes the
        real guard from one rebuilt as is_safe_url("https://" + netloc), which
        the whole of INTERNAL_TARGETS cannot tell apart from correct.
        """
        seen = _install_transport(
            monkeypatch,
            lambda request, index: _redirect(hop_target, status_code),
        )
        integration = self._integration()

        assert MCPLoader([integration])._connect_http(integration) == []
        assert len(seen) == 1, "the non-http hop must never be requested"
        assert seen[0] == "http://example.com/info"

    @pytest.mark.parametrize("status_code", REDIRECT_STATUSES)
    def test_info_endpoint_follows_a_hop_on_every_redirect_status(
        self, monkeypatch, public_dns, status_code
    ):
        """The walk must FOLLOW a safe Location, not just refuse an unsafe one.

        Not the sole guard against either mutation it names, and the earlier
        claim that it was ("every other hop test in this class asserts a
        refusal") was simply false. Measured with this test deselected:
        dropping the `continue` fails 61 cases across four sibling functions
        (hop-revalidation, relative-Location, shared-cap, redirect-bodies),
        all of which drive a chain to completion; narrowing `is_redirect` to
        `has_redirect_location` fails 4 --
        test_info_endpoint_redirect_bodies_are_never_pulled_off_the_wire on
        exactly the 300/304/305/399 parameters, which it has because
        REDIRECT_STATUSES was widened to the full 3xx range.

        What this test adds is the assertion, not the axis: the siblings check
        that no body was pulled, or that a refusal happened. This one asserts
        the hop was actually FOLLOWED -- the two-URL chain in order -- across
        every redirect status. A walk that reached the second URL by some other
        route, or in the wrong order, is only visible here.
        """
        seen = _install_transport(
            monkeypatch,
            lambda request, index: (
                _redirect("http://second.example.com/info", status_code)
                if index == 0
                else httpx.Response(200, json={"endpoints": ["/mcp"]})
            ),
        )
        integration = self._integration()

        MCPLoader([integration])._connect_http(integration)

        assert seen == [
            "http://example.com/info",
            "http://second.example.com/info",
        ]

    def test_info_endpoint_body_over_the_cap_is_refused_and_read_stops(
        self, monkeypatch, public_dns
    ):
        """An oversized info body must not be accumulated whole to be rejected.

        Nothing bounded this read before: the body was pulled in entirely and
        then parsed, so a server advertising no length decided how much memory
        discovery spent. The chunk counter is the point of the test -- a cap
        applied after the read would still pass an assertion about the
        returned tools.
        """
        pulled = []
        chunk = b"x" * 64 * 1024
        chunks = (mcp_loader.MAX_INFO_BODY_BYTES // len(chunk)) * 4

        class _Stream(httpx.SyncByteStream):
            def __iter__(self):
                for _ in range(chunks):
                    pulled.append(len(chunk))
                    yield chunk

        _install_transport(
            monkeypatch,
            lambda request, index: httpx.Response(200, stream=_Stream()),
        )
        integration = self._integration()

        assert MCPLoader([integration])._connect_http(integration) == []
        assert sum(pulled) <= mcp_loader.MAX_INFO_BODY_BYTES + len(chunk)
        assert sum(pulled) < chunks * len(chunk), "the whole body was read"

    def test_an_over_cap_info_body_is_refused_even_when_it_would_still_parse(
        self, monkeypatch, public_dns, caplog
    ):
        """The cap refusal must do the refusing, not json.loads by accident.

        The oversize test above sends b"x" * N, which is not JSON at all, so
        with the `if over_cap` refusal removed the parser raises anyway and the
        loader still degrades to []. That makes the refusal dead: it cannot be
        told apart from absent.

        json.loads ignores trailing whitespace, so a complete document padded
        past the cap parses fine once truncated at it. Without the refusal the
        loader would accept an over-cap, TRUNCATED info document and act on it.
        The log assertion closes the other half: the reason must name the cap
        rather than a JSON syntax error.
        """
        padded = json.dumps({"endpoints": ["/mcp"]}).encode() + b" " * (
            mcp_loader.MAX_INFO_BODY_BYTES + 1024
        )
        # Sanity-check the fixture itself: truncating at the cap must still be
        # parseable, or this test would pass for the reason it exists to reject.
        assert json.loads(padded[: mcp_loader.MAX_INFO_BODY_BYTES])["endpoints"]

        _install_transport(
            monkeypatch,
            lambda request, index: httpx.Response(200, content=padded),
        )
        integration = self._integration()

        with caplog.at_level(logging.WARNING):
            assert MCPLoader([integration])._connect_http(integration) == []

        assert "exceeds" in caplog.text
        assert str(mcp_loader.MAX_INFO_BODY_BYTES) in caplog.text

    def test_info_endpoint_body_within_the_cap_is_parsed_unchanged(
        self, monkeypatch, public_dns
    ):
        """The cap must not change what a normal info document discovers.

        Served across SEVERAL chunks on purpose: a reader that returned only
        the first chunk, or dropped the last, yields invalid JSON here and is
        caught. With a single-chunk body -- as this test first had it -- both
        of those were indistinguishable from correct, and the oversize test
        above cannot see them either.
        """
        payload = json.dumps({"endpoints": ["/mcp"], "pad": "y" * 5000}).encode()

        class _ChunkedStream(httpx.SyncByteStream):
            def __iter__(self):
                for start in range(0, len(payload), 512):
                    yield payload[start : start + 512]

        _install_transport(
            monkeypatch,
            lambda request, index: httpx.Response(200, stream=_ChunkedStream()),
        )
        integration = self._integration(url="http://example.com/mcp")
        loader = MCPLoader([integration])

        tools = loader._connect_http(integration)

        assert tools == loader._get_graph_mcp_tools(integration)

    @pytest.mark.parametrize("start", START_CONTEXTS)
    @pytest.mark.parametrize("internal", INTERNAL_TARGETS)
    @pytest.mark.parametrize("internal_hop", [0, 1, 2, MAX_REDIRECTS - 2])
    def test_info_endpoint_every_redirect_hop_is_revalidated(
        self, monkeypatch, public_dns, internal_hop, internal, start
    ):
        """The internal address is refused wherever it sits in the chain.

        Pinning only hops 0 and 1 leaves a guard that checks the first two
        targets and then stops passing the suite while it walks an internal
        address in at hop 2.
        """

        def handler(request, index):
            if index < internal_hop:
                return _redirect(f"{start}/hop{index + 1}/info")
            if index == internal_hop:
                return _redirect(internal)
            raise AssertionError(f"unexpected request {request.url}")

        seen = _install_transport(monkeypatch, handler)
        integration = self._integration(f"{start}/mcp")

        assert MCPLoader([integration])._connect_http(integration) == []
        assert len(seen) == internal_hop + 1
        assert all(
            urllib.parse.urlparse(url).hostname not in INTERNAL_HOSTS for url in seen
        )

    def test_info_endpoint_same_host_hop_is_still_address_checked(self, monkeypatch):
        """DNS rebinding: the name does not change, what it resolves to does.

        Every other hop test turns internal by changing host, so a walk that
        skipped the re-check when the hop kept the hostname -- because it had
        "already seen" that host, or because the configured address is trusted
        -- would pass all of them. Here the host is identical across the hop
        and only the DNS answer moves, which is the actual rebinding shape.

        One answer, not two: the configured URL is no longer address-checked,
        so the hop is the first and only is_safe_url call in this walk.
        """
        answers = iter([_addrinfo("169.254.169.254")])
        monkeypatch.setattr(
            "backend.core.events.delivery.socket.getaddrinfo",
            lambda *args, **kwargs: next(answers),
        )
        seen = _install_transport(
            monkeypatch,
            lambda request, index: _redirect("http://example.com/second"),
        )
        integration = self._integration()

        assert MCPLoader([integration])._connect_http(integration) == []
        assert seen == ["http://example.com/info"]

    def test_info_endpoint_200_without_an_endpoints_key_discovers_nothing(
        self, monkeypatch, public_dns
    ):
        """Discovery is "200 AND endpoints", not "200 AND parseable JSON"."""
        _install_transport(
            monkeypatch,
            lambda request, index: httpx.Response(200, json={"something_else": 1}),
        )
        integration = self._integration()

        assert MCPLoader([integration])._connect_http(integration) == []

    @pytest.mark.parametrize("status_code", [201, 204])
    def test_info_endpoint_other_2xx_is_not_discovery(
        self, monkeypatch, public_dns, status_code
    ):
        """Only 200 is the info endpoint answering; a 201 is something else."""
        _install_transport(
            monkeypatch,
            lambda request, index: httpx.Response(
                status_code, json={"endpoints": ["/mcp"]}
            ),
        )
        integration = self._integration()

        assert MCPLoader([integration])._connect_http(integration) == []

    @pytest.mark.parametrize("status_code", REDIRECT_STATUSES)
    def test_info_endpoint_hop_is_address_checked_on_every_redirect_status(
        self, monkeypatch, public_dns, status_code
    ):
        """The guard cannot depend on WHICH 3xx carried the Location."""
        seen = _install_transport(
            monkeypatch,
            lambda request, index: _redirect(INTERNAL_TARGETS[0], status_code),
        )
        integration = self._integration()

        assert MCPLoader([integration])._connect_http(integration) == []
        assert seen == ["http://example.com/info"]

    def test_a_reused_loader_still_checks_every_hop(self, monkeypatch, public_dns):
        """A loader that has already connected something judges hops the same.

        connect_all() walks the integrations in turn, so by the second one
        _connections is populated -- state no refusal test here would
        otherwise exercise.
        """
        seen = _install_transport(
            monkeypatch, lambda request, index: _redirect(INTERNAL_TARGETS[0])
        )
        integration = self._integration()
        loader = MCPLoader([integration])
        loader._connections["PRIOR"] = object()

        assert loader._connect_http(integration) == []
        assert seen == ["http://example.com/info"]

    @pytest.mark.parametrize("integration_id", ["WEB", "GRAPH"])
    def test_info_endpoint_hop_guard_does_not_trust_the_integration_id(
        self, monkeypatch, public_dns, integration_id
    ):
        """ "Our own graph MCP is trusted" would disable the guard by id.

        GRAPH is the integration that ships by default, so a guard keyed on
        the id would be off exactly where it matters most. Asserted on the
        requests rather than the tool list, since the GRAPH fallback returns
        its known tools whatever discovery does.
        """
        seen = _install_transport(
            monkeypatch, lambda request, index: _redirect(INTERNAL_TARGETS[0])
        )
        integration = MCPIntegration(
            id=integration_id,
            name="n",
            transport=MCPTransport.HTTP,
            url="http://example.com/mcp",
        )

        MCPLoader([integration])._connect_http(integration)

        assert seen == ["http://example.com/info"]

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
    # Every status a server actually redirects with. httpx's is_redirect is
    # status-only and spans the whole 3xx range, so a 300 or 304 reaches the
    # Location check below instead; these five are has_redirect_location's set.
    # A walker narrowed to a subset (say 301/302/307) would silently DROP a 303
    # or 308 hop and report no tools -- and main, which passed
    # follow_redirects=True here, did follow all five.
    @pytest.mark.parametrize("status_code", [301, 302, 303, 307, 308])
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

    @pytest.mark.parametrize("status_code", REDIRECT_STATUSES)
    @pytest.mark.parametrize("headers", [{}, {"location": ""}], ids=["absent", "empty"])
    def test_info_endpoint_redirect_without_a_location_is_refused(
        self, monkeypatch, public_dns, headers, status_code
    ):
        """urljoin(current, "") is current, so an empty Location used to
        re-request the same URL until the cap ran out."""
        seen = _install_transport(
            monkeypatch,
            lambda request, index: httpx.Response(status_code, headers=headers),
        )
        integration = self._integration()

        assert MCPLoader([integration])._connect_http(integration) == []
        assert len(seen) == 1

    @pytest.mark.parametrize("status_code", REDIRECT_STATUSES)
    def test_info_endpoint_redirect_bodies_are_never_pulled_off_the_wire(
        self, monkeypatch, public_dns, status_code
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
                    status_code,
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
            "https://127.0.0.1/admin",
            "http://10.0.0.1/internal",
            "https://10.0.0.1/internal",
            "http://192.168.1.1/router",
            "http://169.254.169.254/latest/meta-data/",
            "https://169.254.169.254/latest/meta-data/",
            "http://100.64.0.1/cgnat",
            "http://[::1]/admin",
            "https://[::1]/admin",
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

    @pytest.mark.parametrize("scheme", ["http", "https"])
    def test_hostname_resolving_into_private_range_is_rejected(
        self, monkeypatch, scheme
    ):
        """A public-looking hostname whose DNS answer is internal is still blocked."""
        monkeypatch.setattr(
            "backend.core.events.delivery.socket.getaddrinfo",
            lambda *args, **kwargs: _addrinfo("10.0.0.5"),
        )

        def handler(request, index):  # pragma: no cover - must not be reached
            raise AssertionError(f"internal address requested: {request.url}")

        seen = _install_transport(monkeypatch, handler)

        result = self._fetch(f"{scheme}://internal.example.com/secrets")

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

    @pytest.mark.parametrize("internal", INTERNAL_TARGETS)
    def test_redirect_into_private_range_is_not_followed(
        self, monkeypatch, public_dns, internal
    ):
        """A public URL that redirects to an internal address must stop at the hop.

        The initial host passes the pre-request check, so only the per-hop
        re-validation can catch this one.
        """
        seen = _install_transport(
            monkeypatch, lambda request, index: _redirect(internal)
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

    @pytest.mark.parametrize("start", START_CONTEXTS)
    @pytest.mark.parametrize("internal", INTERNAL_TARGETS)
    @pytest.mark.parametrize("internal_hop", [0, 1, 2, MAX_REDIRECTS - 2])
    def test_every_redirect_hop_is_revalidated(
        self, monkeypatch, public_dns, internal_hop, internal, start
    ):
        """The internal address is refused wherever it sits in the chain.

        A guard that validated only the first one or two redirect targets
        would pass the single-hop test above and still walk a longer
        public -> public -> internal chain all the way in.
        """

        def handler(request, index):
            if index < internal_hop:
                return _redirect(f"{start}/hop{index + 1}/p")
            if index == internal_hop:
                return _redirect(internal)
            raise AssertionError(f"unexpected request {request.url}")

        seen = _install_transport(monkeypatch, handler)

        result = self._fetch(f"{start}/start")

        assert result == {"error": "Redirected to unsafe URL"}
        assert len(seen) == internal_hop + 1
        assert all(
            urllib.parse.urlparse(url).hostname not in INTERNAL_HOSTS for url in seen
        )

    def test_same_host_hop_is_still_address_checked(self, monkeypatch):
        """DNS rebinding: the name does not change, what it resolves to does."""
        answers = iter([_addrinfo(), _addrinfo("169.254.169.254")])
        monkeypatch.setattr(
            "backend.core.events.delivery.socket.getaddrinfo",
            lambda *args, **kwargs: next(answers),
        )
        seen = _install_transport(
            monkeypatch,
            lambda request, index: _redirect("http://example.com/second"),
        )

        result = self._fetch()

        assert result == {"error": "Redirected to unsafe URL"}
        assert seen == ["http://example.com/start"]

    def test_the_result_reports_the_requested_url_and_the_real_status(
        self, monkeypatch, public_dns
    ):
        """After a redirect the tool answers for the URL it was ASKED for.

        Pinning content and status alone leaves "url" free to drift to the
        final hop, and every other terminal fixture is a 200, so the status
        could be hard-coded and nothing would notice.
        """

        def handler(request, index):
            if index == 0:
                return _redirect("http://example.com/final")
            return httpx.Response(201, text="made")

        _install_transport(monkeypatch, handler)

        assert self._fetch() == {
            "url": "http://example.com/start",
            "status": 201,
            "content": "made",
        }

    def test_the_default_max_length_bounds_the_content(self, monkeypatch, public_dns):
        """max_length is the only thing bounding what an agent is handed."""
        _install_transport(
            monkeypatch, lambda request, index: httpx.Response(200, text="x" * 10_001)
        )

        result = self._fetch()

        assert result["content"] == "x" * 10_000 + "... (truncated)"

    def test_a_body_of_exactly_max_length_is_not_truncated(
        self, monkeypatch, public_dns
    ):
        """The cap is "longer than", not "at least" -- pins the boundary."""
        _install_transport(
            monkeypatch, lambda request, index: httpx.Response(200, text="x" * 10)
        )

        assert self._fetch(max_length=10)["content"] == "x" * 10

    def test_extra_tool_arguments_do_not_relax_the_initial_guard(
        self, monkeypatch, public_dns
    ):
        """input_args is whatever the model emits, so it must not steer the guard."""

        def handler(request, index):  # pragma: no cover - must not be reached
            raise AssertionError(f"internal address requested: {request.url}")

        seen = _install_transport(monkeypatch, handler)

        result = self._fetch(INTERNAL_TARGETS[0], max_length=500)

        assert "error" in result
        assert "content" not in result
        assert seen == []

    def test_a_reused_loader_still_checks_every_hop(self, monkeypatch, public_dns):
        """A loader with connections already cached judges hops the same way."""
        seen = _install_transport(
            monkeypatch, lambda request, index: _redirect(INTERNAL_TARGETS[0])
        )
        loader = MCPLoader([])
        loader._connections["PRIOR"] = object()

        result = loader._execute_fetch_tool(
            "fetch", {"url": "http://example.com/start"}
        )

        assert result == {"error": "Redirected to unsafe URL"}
        assert seen == ["http://example.com/start"]

    @pytest.mark.parametrize("status_code", REDIRECT_STATUSES)
    def test_hop_is_address_checked_on_every_redirect_status(
        self, monkeypatch, public_dns, status_code
    ):
        """The guard cannot depend on WHICH 3xx carried the Location.

        This walker hands the body back to the agent, so a status-conditional
        guard here leaks the internal response rather than merely fetching it.
        """
        seen = _install_transport(
            monkeypatch,
            lambda request, index: _redirect(INTERNAL_TARGETS[0], status_code),
        )

        result = self._fetch()

        assert result == {"error": "Redirected to unsafe URL"}
        assert seen == ["http://example.com/start"]

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
    # Every status a server actually redirects with. httpx's is_redirect is
    # status-only and spans the whole 3xx range, so a 300 or 304 reaches the
    # Location check below instead; these five are has_redirect_location's set.
    # A walker narrowed to a subset (say 301/302/307) would treat a 303 or 308
    # as terminal, where raise_for_status turns it into an error rather than
    # following it. main hand-walked THIS walker too (follow_redirects=False),
    # so that narrowing is a regression against main here as well -- unlike
    # _connect_http, whose main version did pass follow_redirects=True.
    @pytest.mark.parametrize("status_code", [301, 302, 303, 307, 308])
    def test_redirect_chain_stops_at_the_shared_cap_with_an_explicit_error(
        self, monkeypatch, public_dns, location, status_code
    ):
        """An exhausted redirect chain reports the limit it hit, on every flavour.

        The chain here is endless but every hop is public, so the SSRF check
        never fires; only the cap can end it. Parametrising the Location form
        and the status code catches a counter that advanced on one flavour of
        hop and left the others unbounded. The count is taken against
        delivery.py's constant, the one all five walkers import.
        """
        seen = _install_transport(
            monkeypatch, lambda request, index: _redirect(location, status_code)
        )

        result = self._fetch()

        assert result == {"error": f"Too many redirects (limit {MAX_REDIRECTS})"}
        assert len(seen) == delivery.MAX_REDIRECTS

    @pytest.mark.parametrize("status_code", REDIRECT_STATUSES)
    @pytest.mark.parametrize("headers", [{}, {"location": ""}], ids=["absent", "empty"])
    def test_a_redirect_without_a_location_is_refused_not_spun_to_the_cap(
        self, monkeypatch, public_dns, headers, status_code
    ):
        """urljoin of an empty Location is the current URL, so it used to be
        re-requested until the redirect cap ran out."""
        seen = _install_transport(
            monkeypatch,
            lambda request, index: httpx.Response(status_code, headers=headers),
        )

        result = self._fetch()

        assert result == {"error": "Redirect without a Location header"}
        assert len(seen) == 1

    @pytest.mark.parametrize("status_code", [404, 500])
    def test_a_terminal_error_status_is_an_error_not_content(
        self, monkeypatch, public_dns, status_code
    ):
        """raise_for_status still runs, so an error page is never handed back.

        Without it the tool answers {"status": 404, "content": "<error page>"}
        and an agent reads the error page as if it were the page it asked for.
        """
        _install_transport(
            monkeypatch,
            lambda request, index: httpx.Response(status_code, text="<html>nope"),
        )

        result = self._fetch()

        assert "error" in result
        assert "content" not in result

    def test_fetch_client_is_configured_with_the_expected_timeout(
        self, monkeypatch, public_dns
    ):
        client_kwargs = []
        _install_transport(
            monkeypatch,
            lambda request, index: httpx.Response(200, text="ok"),
            client_kwargs=client_kwargs,
        )

        self._fetch()

        assert len(client_kwargs) == 1
        assert client_kwargs[0]["timeout"] == 30
        assert client_kwargs[0]["follow_redirects"] is False

        # A subset assertion admits anything it does not name, so the keys that
        # would quietly undo the guard are asserted ABSENT rather than merely
        # unmentioned: verify=False drops certificate checking, proxy routes the
        # request somewhere else entirely, and trust_env picks a proxy up from
        # the environment.
        for forbidden in ("verify", "proxy", "proxies", "trust_env"):
            assert forbidden not in client_kwargs[0]

    def test_body_over_max_length_is_truncated(self, monkeypatch, public_dns):
        """The size cap on the returned content still applies after the rewrite."""
        _install_transport(
            monkeypatch, lambda request, index: httpx.Response(200, text="x" * 50)
        )

        result = self._fetch(max_length=10)

        assert result["content"] == "x" * 10 + "... (truncated)"

    @pytest.mark.parametrize("hop_target", NON_HTTP_HOP_TARGETS)
    @pytest.mark.parametrize("status_code", REDIRECT_STATUSES)
    def test_a_non_http_hop_is_refused_and_never_requested(
        self, monkeypatch, public_dns, status_code, hop_target
    ):
        """Pins the SCHEME half of is_safe_url's verdict on the hop axis.

        The host resolves publicly, so only the scheme can refuse this -- the
        same gap the sibling walker's test closes, and the reason a guard
        rebuilt as is_safe_url("https://" + netloc) survived the whole of
        INTERNAL_TARGETS.
        """
        seen = _install_transport(
            monkeypatch,
            lambda request, index: _redirect(hop_target, status_code),
        )

        result = self._fetch()

        assert result == {"error": "Redirected to unsafe URL"}
        assert len(seen) == 1, "the non-http hop must never be requested"

    def test_body_over_the_ingest_cap_stops_reading_and_marks_truncation(
        self, monkeypatch, public_dns
    ):
        """The page is bounded as it arrives, not truncated after it all lands.

        max_length truncated a body that had already been read in full, so a
        server advertising no length decided how much memory this tool spent.
        The chunk counter is the assertion that matters; the marker is there so
        a caller is not handed a short page as if it were the whole document.
        """
        pulled = []
        chunk = b"x" * 256 * 1024
        chunks = (mcp_loader.MAX_FETCH_BODY_BYTES // len(chunk)) * 2

        class _Stream(httpx.SyncByteStream):
            def __iter__(self):
                for _ in range(chunks):
                    pulled.append(len(chunk))
                    yield chunk

        _install_transport(
            monkeypatch,
            lambda request, index: httpx.Response(200, stream=_Stream()),
        )

        result = self._fetch(max_length=mcp_loader.MAX_FETCH_BODY_BYTES * 4)

        assert result["content"].endswith("... (truncated)")
        assert sum(pulled) <= mcp_loader.MAX_FETCH_BODY_BYTES + len(chunk)
        assert sum(pulled) < chunks * len(chunk), "the whole body was read"

    def test_a_page_between_the_two_caps_is_returned_whole(
        self, monkeypatch, public_dns
    ):
        """Pins WHICH cap this call site passes, not just the caps' values.

        TestBodyCapConstants pins both constants by value, and the oversize
        test derives its fixture from MAX_FETCH_BODY_BYTES and asserts only an
        UPPER bound on bytes pulled -- which a tighter cap satisfies strictly.
        So handing this call site MAX_INFO_BODY_BYTES instead cut every page in
        the band between the two caps down to a tenth, stamped it truncated,
        and passed the whole suite. Every other within-cap body here is about a
        kilobyte, three orders of magnitude below either cap.

        This body sits above the info cap and far below the fetch one, so only
        the correct constant returns it whole.
        """
        size = mcp_loader.MAX_INFO_BODY_BYTES + 64 * 1024
        assert size < mcp_loader.MAX_FETCH_BODY_BYTES
        body = "p" * size
        _install_transport(
            monkeypatch, lambda request, index: httpx.Response(200, text=body)
        )

        result = self._fetch(max_length=size * 2)

        assert result["content"] == body

    def test_a_body_within_the_cap_is_returned_whole(self, monkeypatch, public_dns):
        """The cap must not truncate or mark an ordinary page.

        A reader that dropped its final chunk, or one that flagged every
        response truncated, would still satisfy the oversize test above.
        """
        body = "y" * 1000
        _install_transport(
            monkeypatch, lambda request, index: httpx.Response(200, text=body)
        )

        result = self._fetch(max_length=10_000)

        assert result["content"] == body

    def test_a_declared_charset_is_decoded_the_way_response_text_would(
        self, monkeypatch, public_dns
    ):
        """Reading the body by hand must not quietly become a utf-8 assumption.

        response.text decoded through the charset in Content-Type; the capped
        read has to do the same or a latin-1 page comes back mojibake. The
        undecodable-byte test below is the only other non-ASCII body in this
        class, and it declares utf-8, so a hardcoded utf-8 agrees with it --
        this is the only test here that can tell the two apart. Measured:
        hardcoding utf-8 fails this test alone.
        """
        text = "café"
        _install_transport(
            monkeypatch,
            lambda request, index: httpx.Response(
                200,
                content=text.encode("latin-1"),
                headers={"content-type": "text/html; charset=latin-1"},
            ),
        )

        assert self._fetch(max_length=10_000)["content"] == text

    def test_an_undecodable_byte_is_replaced_the_way_response_text_would(
        self, monkeypatch, public_dns
    ):
        """Pins the error POLICY, not just the codec.

        response.text decodes with errors="replace". Switching the manual
        decode to "strict" turns one bad byte into
        {"error": "Fetch failed: ...codec can't decode..."} -- swallowed by the
        surrounding except and returned as a plausible product error -- and
        "ignore" drops the byte silently. The charset test above cannot see
        either, because its body decodes cleanly. Asserted against httpx's own
        answer rather than a hardcoded string, so the two cannot drift.
        """
        body = b"a\xffb"
        expected = httpx.Response(
            200, content=body, headers={"content-type": "text/html; charset=utf-8"}
        ).text
        _install_transport(
            monkeypatch,
            lambda request, index: httpx.Response(
                200, content=body, headers={"content-type": "text/html; charset=utf-8"}
            ),
        )

        assert self._fetch(max_length=10_000)["content"] == expected
        assert expected == "a\ufffdb"

    @pytest.mark.parametrize("status_code", REDIRECT_STATUSES)
    def test_redirect_bodies_are_never_pulled_off_the_wire(
        self, monkeypatch, public_dns, status_code
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
                    status_code,
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
    """One cap for the five hop-validating paths, so they cannot drift again."""

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

    @pytest.mark.parametrize(
        "module",
        [mcp_loader, image_ingest, skills_loader, delivery],
        ids=lambda module: module.__name__.rsplit(".", 1)[-1],
    )
    def test_every_walker_loop_ranges_over_the_imported_name(self, module):
        """Per-module is not enough once a module holds two walkers.

        mcp_loader imports the cap for its fetch tool, so a SECOND walker in
        the same file could write range(10) and the import test above would
        still pass -- the module imports the name and assigns it nowhere, and
        a literal 10 satisfies the count assertions today. Requiring the
        argument to be a bare name closes that per-call-site hole.

        Scoped to the `for`/`async for` statements that iterate a range(),
        rather than every range() call anywhere in the module. What that buys
        is narrow and worth stating exactly, because the obvious reading is
        wrong: a plain `for i in range(3)` STILL fails here, deliberately.
        It is a loop over a literal, and the assertion below cannot exempt it
        without also exempting a walk written `range(10)` -- which is the one
        thing this test exists to catch. What the narrowing does exempt is a
        range() that is not a loop's iterable at all: `list(range(3))`,
        `sum(range(3))`, `random.choice(range(3))`. Under the old
        whole-module ban each of those failed with a message about redirect
        walks, and the cheapest way to silence that is to weaken or delete
        the test, which is how the per-call-site protection gets lost.

        Known cost of the narrowing: one level of indirection now slips past
        where the whole-module ban caught it -- `hops = range(10)` then
        `for _ in hops`, or `for _ in reversed(range(10))`. A `while` counter
        was never covered either way. Both are contrived next to the thing
        being prevented, which is a second walker in an already-importing
        module quietly writing the number.
        """
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))

        walk_ranges = [
            node.iter
            for node in ast.walk(tree)
            if isinstance(node, (ast.For, ast.AsyncFor))
            and isinstance(node.iter, ast.Call)
            and isinstance(node.iter.func, ast.Name)
            and node.iter.func.id == "range"
        ]

        assert walk_ranges, "expected at least one redirect walk in this module"
        for node in walk_ranges:
            assert len(node.args) == 1
            assert isinstance(node.args[0], ast.Name), ast.dump(node)
            assert node.args[0].id == "MAX_REDIRECTS"
