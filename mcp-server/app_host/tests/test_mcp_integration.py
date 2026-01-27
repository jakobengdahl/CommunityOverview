"""
MCP integration tests for app_host.

Tests that MCP tools return the same data as REST API endpoints,
ensuring consistency across both interfaces.
"""

import pytest
import json
from fastapi.testclient import TestClient

from app_host import create_app, AppConfig


class TestMCPToolsConsistency:
    """Tests that MCP tools return same data as REST API."""

    def test_search_consistency(self, test_app: TestClient):
        """Search via MCP tool returns same results as REST API."""
        # Search via REST API
        rest_response = test_app.post("/api/search", json={"query": "test"})
        rest_data = rest_response.json()

        # Search via execute_tool (MCP tool)
        mcp_response = test_app.post(
            "/execute_tool",
            json={"tool_name": "search_graph", "arguments": {"query": "test"}}
        )
        mcp_data = mcp_response.json()

        # Compare results
        assert rest_response.status_code == 200
        assert mcp_response.status_code == 200
        assert len(rest_data["nodes"]) == len(mcp_data["nodes"])

        # Compare node IDs
        rest_node_ids = {n["id"] for n in rest_data["nodes"]}
        mcp_node_ids = {n["id"] for n in mcp_data["nodes"]}
        assert rest_node_ids == mcp_node_ids

    def test_get_node_details_consistency(self, test_app: TestClient):
        """Get node details returns same data via REST and MCP."""
        # Get via REST
        rest_response = test_app.get("/api/nodes/node-1")
        rest_data = rest_response.json()

        # Get via MCP tool
        mcp_response = test_app.post(
            "/execute_tool",
            json={"tool_name": "get_node_details", "arguments": {"node_id": "node-1"}}
        )
        mcp_data = mcp_response.json()

        # Compare results
        assert rest_data["node"]["id"] == mcp_data["node"]["id"]
        assert rest_data["node"]["name"] == mcp_data["node"]["name"]
        assert rest_data["node"]["type"] == mcp_data["node"]["type"]

    def test_stats_consistency(self, test_app: TestClient):
        """Stats are consistent between REST and MCP."""
        # Get via REST
        rest_response = test_app.get("/api/stats")
        rest_data = rest_response.json()

        # Get via MCP tool
        mcp_response = test_app.post(
            "/execute_tool",
            json={"tool_name": "get_graph_stats", "arguments": {}}
        )
        mcp_data = mcp_response.json()

        # Compare results
        assert rest_data["total_nodes"] == mcp_data["total_nodes"]
        assert rest_data["total_edges"] == mcp_data["total_edges"]

    def test_similarity_consistency(self, test_app: TestClient):
        """Similarity search returns consistent results."""
        params = {"name": "Test", "threshold": 0.3}

        # Search via REST
        rest_response = test_app.post("/api/similar", json=params)
        rest_data = rest_response.json()

        # Search via MCP tool
        mcp_response = test_app.post(
            "/execute_tool",
            json={"tool_name": "find_similar_nodes", "arguments": params}
        )
        mcp_data = mcp_response.json()

        # Compare results
        assert rest_response.status_code == 200
        assert mcp_response.status_code == 200
        # Both should return similar_nodes key
        assert "similar_nodes" in rest_data
        assert "similar_nodes" in mcp_data


class TestMCPToolsAvailability:
    """Tests that all expected MCP tools are registered."""

    def test_search_graph_tool_available(self, test_app: TestClient):
        """search_graph tool is available."""
        response = test_app.post(
            "/execute_tool",
            json={"tool_name": "search_graph", "arguments": {"query": "x"}}
        )
        # Should not return 404 (tool not found)
        assert response.status_code != 404

    def test_get_node_details_tool_available(self, test_app: TestClient):
        """get_node_details tool is available."""
        response = test_app.post(
            "/execute_tool",
            json={"tool_name": "get_node_details", "arguments": {"node_id": "node-1"}}
        )
        assert response.status_code != 404

    def test_add_nodes_tool_available(self, test_app: TestClient):
        """add_nodes tool is available."""
        response = test_app.post(
            "/execute_tool",
            json={
                "tool_name": "add_nodes",
                "arguments": {
                    "nodes": [{"type": "Actor", "name": "X", "communities": []}],
                    "edges": []
                }
            }
        )
        assert response.status_code != 404

    def test_delete_nodes_tool_available(self, test_app: TestClient):
        """delete_nodes tool is available."""
        response = test_app.post(
            "/execute_tool",
            json={
                "tool_name": "delete_nodes",
                "arguments": {"node_ids": ["x"], "confirmed": False}
            }
        )
        # Should return 200 with error message, not 404
        assert response.status_code != 404

    def test_find_similar_nodes_tool_available(self, test_app: TestClient):
        """find_similar_nodes tool is available."""
        response = test_app.post(
            "/execute_tool",
            json={
                "tool_name": "find_similar_nodes",
                "arguments": {"name": "test"}
            }
        )
        assert response.status_code != 404

    def test_get_graph_stats_tool_available(self, test_app: TestClient):
        """get_graph_stats tool is available."""
        response = test_app.post(
            "/execute_tool",
            json={"tool_name": "get_graph_stats", "arguments": {}}
        )
        assert response.status_code != 404


class TestMCPEndpointAvailability:
    """Tests that MCP endpoint is mounted and accessible."""

    def test_mcp_endpoint_mounted(self, test_app: TestClient):
        """MCP endpoint is mounted at /mcp."""
        # Verify MCP is properly configured by checking app state
        # FastMCP may not respond to GET / but should be mounted
        assert hasattr(test_app.app.state, "mcp")
        assert test_app.app.state.mcp is not None
        assert hasattr(test_app.app.state, "tools_map")
        assert len(test_app.app.state.tools_map) > 0


class TestMCPToolsWithEdgeCases:
    """Tests MCP tools with edge cases and error conditions."""

    def test_search_empty_query(self, test_app: TestClient):
        """Search with empty query should work or return error."""
        response = test_app.post(
            "/execute_tool",
            json={"tool_name": "search_graph", "arguments": {"query": ""}}
        )
        # Should return some response (empty results or error), not crash
        assert response.status_code in [200, 400]

    def test_get_nonexistent_node(self, test_app: TestClient):
        """Get details for non-existent node returns appropriate response."""
        response = test_app.post(
            "/execute_tool",
            json={
                "tool_name": "get_node_details",
                "arguments": {"node_id": "nonexistent-id"}
            }
        )
        data = response.json()
        # Should indicate node not found
        assert data.get("success") is False or "error" in data or "node" in data

    def test_add_invalid_node(self, test_app: TestClient):
        """Add node with invalid type returns error."""
        response = test_app.post(
            "/execute_tool",
            json={
                "tool_name": "add_nodes",
                "arguments": {
                    "nodes": [{"type": "InvalidType", "name": "Bad Node"}],
                    "edges": []
                }
            }
        )
        # Should handle gracefully - either return error or success=False
        assert response.status_code in [200, 400, 500]
        if response.status_code == 200:
            data = response.json()
            # If 200, should indicate error in response
            assert data.get("success") is False or "error" in data

    def test_delete_without_confirmation(self, test_app: TestClient):
        """Delete without confirmation returns appropriate message."""
        response = test_app.post(
            "/execute_tool",
            json={
                "tool_name": "delete_nodes",
                "arguments": {"node_ids": ["node-1"], "confirmed": False}
            }
        )
        data = response.json()
        # Should indicate confirmation required
        assert data.get("success") is False or "confirm" in str(data).lower()
