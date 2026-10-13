"""Protocol shape of the interactive graph widget delivered over MCP.

The widget reaches ChatGPT and Claude through one mechanism, the MCP Apps
extension (spec 2026-01-26): a tool's ``_meta`` names a ``ui://`` resource and
the host reads that resource as ``text/html;profile=mcp-app``. These tests run
a real FastMCP server through an in-memory client session, so they check what
a host actually receives on the wire rather than what the code intends.
"""

import os
from unittest.mock import MagicMock, Mock

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.shared.memory import create_connected_server_and_client_session

from backend.core import GraphStorage
from backend.service import GraphService, register_mcp_tools
from backend.service.mcp_widget import (
    MCP_APP_MIME_TYPE,
    WIDGET_RESOURCE_URI,
    build_widget_html,
)


def _service(tmp_path):
    storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
    service = GraphService(storage)
    service.add_nodes(
        nodes=[
            {"id": "a1", "type": "Actor", "name": "Alpha Agency"},
            {"id": "i1", "type": "Initiative", "name": "Alpha Programme"},
        ],
        edges=[{"source": "a1", "target": "i1", "type": "RELATES_TO"}],
    )
    return service


@pytest.fixture
def widget_dist(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    # A bundle containing "</script" must not close the inline tag early.
    (dist / "widget.iife.js").write_text('var s="</script>";window.__w=1;')
    (dist / "style.css").write_text(".widget-container{color:red}")
    return dist


@pytest.fixture
def server(tmp_path, widget_dist):
    mcp = FastMCP("widget-test")
    register_mcp_tools(mcp, _service(tmp_path), widget_dist_path=widget_dist)
    return mcp


async def _session(server):
    return create_connected_server_and_client_session(server._mcp_server)


@pytest.mark.asyncio
async def test_widget_tool_meta_points_every_host_key_at_the_ui_resource(server):
    async with await _session(server) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}

    meta = tools["show_graph_widget"].meta
    assert meta["ui"]["resourceUri"] == WIDGET_RESOURCE_URI
    assert meta["ui/resourceUri"] == WIDGET_RESOURCE_URI
    assert meta["openai/outputTemplate"] == WIDGET_RESOURCE_URI
    assert WIDGET_RESOURCE_URI.startswith("ui://")


@pytest.mark.asyncio
async def test_only_the_render_tool_carries_ui_meta(server):
    # A UI link on the data tools would make hosts render a widget for every
    # search an agent runs.
    async with await _session(server) as client:
        tools = (await client.list_tools()).tools

    ui_keys = {"ui", "ui/resourceUri", "openai/outputTemplate"}
    with_ui = [t.name for t in tools if t.meta and ui_keys & set(t.meta)]
    assert with_ui == ["show_graph_widget"]


@pytest.mark.asyncio
async def test_ui_resource_is_listed_and_read_as_mcp_app_html(server):
    async with await _session(server) as client:
        listed = {str(r.uri): r for r in (await client.list_resources()).resources}
        read = await client.read_resource(WIDGET_RESOURCE_URI)

    # The literal, not the constant: hosts match this exact profile string.
    assert MCP_APP_MIME_TYPE == "text/html;profile=mcp-app"
    assert listed[WIDGET_RESOURCE_URI].mimeType == "text/html;profile=mcp-app"
    (content,) = read.contents
    assert content.mimeType == "text/html;profile=mcp-app"
    assert content.meta["ui"]["csp"] == {"connectDomains": [], "resourceDomains": []}
    assert content.meta["ui"]["prefersBorder"] is True
    assert content.text.startswith("<!DOCTYPE html>")
    assert '<div id="widget-root"></div>' in content.text
    assert "window.__w=1" in content.text
    assert ".widget-container{color:red}" in content.text


@pytest.mark.asyncio
async def test_render_tool_returns_nodes_and_edges_as_structured_content(server):
    async with await _session(server) as client:
        result = await client.call_tool("show_graph_widget", {"query": "Alpha"})

    assert not result.isError
    # FastMCP wraps a Dict-annotated return under "result"; the widget's
    # unwrapToolResult (frontend/widget/src/mcpClient.js) relies on this shape.
    payload = result.structuredContent["result"]
    assert payload["query"] == "Alpha"
    assert {n["id"] for n in payload["nodes"]} == {"a1", "i1"}
    assert [(e["source"], e["target"]) for e in payload["edges"]] == [("a1", "i1")]


@pytest.mark.asyncio
async def test_render_tool_passes_its_arguments_to_the_search(widget_dist):
    service = Mock()
    service.search_graph.return_value = {"nodes": [], "edges": []}
    mcp = FastMCP("widget-args")
    register_mcp_tools(mcp, service, widget_dist_path=widget_dist)

    async with await _session(mcp) as client:
        result = await client.call_tool(
            "show_graph_widget",
            {"query": "data strategy", "node_types": ["Actor"], "limit": 7},
        )

    service.search_graph.assert_called_once_with(
        query="data strategy", node_types=["Actor"], limit=7
    )
    assert result.structuredContent["result"]["query"] == "data strategy"


def test_render_tool_stays_off_the_in_app_tools_map(tmp_path, widget_dist):
    mock_mcp = Mock()
    mock_mcp.tool = MagicMock(return_value=lambda f: f)
    tools_map = register_mcp_tools(
        mock_mcp, _service(tmp_path), widget_dist_path=widget_dist
    )
    assert "show_graph_widget" not in tools_map
    assert "search_graph" in tools_map


def test_inlined_bundle_cannot_close_its_script_or_style_tag(tmp_path):
    dist = tmp_path / "d"
    dist.mkdir()
    (dist / "widget.iife.js").write_text('a="</script><b>";c="</SCRIPT >";')
    (dist / "style.css").write_text('x::after{content:"</style>"}y{content:"</STYLE>"}')

    html = build_widget_html(dist)

    # HTML matches end tags case-insensitively, so every casing must be escaped.
    assert html.lower().count("</script") == 1
    assert html.lower().count("</style") == 2  # the base style block plus the bundle's
    assert 'a="<\\/script><b>";c="<\\/SCRIPT >";' in html


def test_missing_bundle_yields_a_placeholder_document(tmp_path):
    html = build_widget_html(tmp_path / "absent")
    assert html.startswith("<!DOCTYPE html>")
    assert "npm run build:widget" in html
    assert "<script" not in html
