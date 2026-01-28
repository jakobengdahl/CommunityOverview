"""
Unit tests for app_host using FastAPI TestClient.

Tests that the FastAPI application is properly configured and
all REST API endpoints function correctly.
"""

import pytest
from fastapi.testclient import TestClient


class TestHealthAndRoot:
    """Tests for health check and root endpoints."""

    def test_health_check(self, test_app: TestClient):
        """Health endpoint returns healthy status."""
        response = test_app.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert "graph_nodes" in data
        assert "graph_edges" in data

    def test_root_endpoint_redirects(self, test_app: TestClient):
        """Root endpoint redirects to /web/."""
        response = test_app.get("/", follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["location"] == "/web/"

    def test_info_endpoint(self, test_app: TestClient):
        """Info endpoint returns API information."""
        response = test_app.get("/info")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Community Knowledge Graph"
        assert "endpoints" in data
        assert "graph_stats" in data
        assert data["endpoints"]["api"] == "/api"
        assert data["endpoints"]["mcp"] == "/mcp"

    def test_health_shows_graph_stats(self, test_app: TestClient):
        """Health endpoint shows correct graph statistics."""
        response = test_app.get("/health")
        data = response.json()
        assert data["graph_nodes"] == 3
        assert data["graph_edges"] == 2


class TestSearchEndpoints:
    """Tests for search-related endpoints."""

    def test_search_graph_basic(self, test_app: TestClient):
        """Basic search returns results."""
        response = test_app.post("/api/search", json={"query": "test"})
        assert response.status_code == 200
        data = response.json()
        assert "nodes" in data
        assert len(data["nodes"]) > 0

    def test_search_graph_with_type_filter(self, test_app: TestClient):
        """Search with node type filter."""
        response = test_app.post(
            "/api/search",
            json={"query": "test", "node_types": ["Actor"]}
        )
        assert response.status_code == 200
        data = response.json()
        for node in data["nodes"]:
            assert node["type"] == "Actor"

    def test_search_graph_with_community_filter(self, test_app: TestClient):
        """Search with community filter."""
        response = test_app.post(
            "/api/search",
            json={"query": "test", "communities": ["TestCommunity"]}
        )
        assert response.status_code == 200
        data = response.json()
        for node in data["nodes"]:
            assert "TestCommunity" in node.get("communities", [])

    def test_search_graph_with_limit(self, test_app: TestClient):
        """Search respects limit parameter."""
        response = test_app.post(
            "/api/search",
            json={"query": "test", "limit": 1}
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["nodes"]) <= 1

    def test_search_returns_edges(self, test_app: TestClient):
        """Search returns related edges."""
        response = test_app.post("/api/search", json={"query": "test"})
        data = response.json()
        assert "edges" in data


class TestNodeEndpoints:
    """Tests for node CRUD endpoints."""

    def test_get_node_details_success(self, test_app: TestClient):
        """Get node details for existing node."""
        response = test_app.get("/api/nodes/node-1")
        assert response.status_code == 200
        data = response.json()
        assert data["node"]["id"] == "node-1"
        assert data["node"]["name"] == "Test Organization"

    def test_get_node_details_not_found(self, test_app: TestClient):
        """Get node details returns 404 for missing node."""
        response = test_app.get("/api/nodes/nonexistent-node")
        assert response.status_code == 404

    def test_get_related_nodes(self, test_app: TestClient):
        """Get related nodes for a node."""
        response = test_app.post(
            "/api/nodes/node-1/related",
            json={"depth": 1}
        )
        assert response.status_code == 200
        data = response.json()
        assert "nodes" in data
        assert "edges" in data

    def test_get_related_nodes_with_depth(self, test_app: TestClient):
        """Get related nodes with increased depth."""
        response = test_app.post(
            "/api/nodes/node-1/related",
            json={"depth": 2}
        )
        assert response.status_code == 200
        data = response.json()
        # Should find more nodes with depth 2
        assert "nodes" in data

    def test_add_nodes(self, test_app_empty_graph: TestClient):
        """Add new nodes to the graph."""
        new_node = {
            "type": "Actor",
            "name": "New Organization",
            "description": "A newly added organization",
            "communities": ["NewCommunity"]
        }
        response = test_app_empty_graph.post(
            "/api/nodes",
            json={"nodes": [new_node]}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert len(data["added_node_ids"]) == 1

    def test_add_nodes_with_edges(self, test_app_empty_graph: TestClient):
        """Add nodes with edges."""
        nodes = [
            {"type": "Actor", "name": "Org A", "communities": []},
            {"type": "Initiative", "name": "Project A", "communities": []}
        ]
        # Note: edges will use generated IDs, so we test without them first
        response = test_app_empty_graph.post(
            "/api/nodes",
            json={"nodes": nodes}
        )
        assert response.status_code == 200
        data = response.json()
        assert len(data["added_node_ids"]) == 2

    def test_update_node(self, test_app: TestClient):
        """Update an existing node."""
        response = test_app.patch(
            "/api/nodes/node-1",
            json={"updates": {"description": "Updated description"}}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

        # Verify update
        get_response = test_app.get("/api/nodes/node-1")
        node_data = get_response.json()
        assert node_data["node"]["description"] == "Updated description"

    def test_update_node_not_found(self, test_app: TestClient):
        """Update non-existent node returns 404."""
        response = test_app.patch(
            "/api/nodes/nonexistent",
            json={"updates": {"description": "test"}}
        )
        assert response.status_code == 404

    def test_delete_nodes_requires_confirmation(self, test_app: TestClient):
        """Delete without confirmation fails."""
        response = test_app.request(
            "DELETE",
            "/api/nodes",
            json={"node_ids": ["node-1"], "confirmed": False}
        )
        assert response.status_code == 400

    def test_delete_nodes_with_confirmation(self, test_app: TestClient):
        """Delete with confirmation succeeds."""
        response = test_app.request(
            "DELETE",
            "/api/nodes",
            json={"node_ids": ["node-3"], "confirmed": True}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True


class TestSimilarityEndpoints:
    """Tests for similarity search endpoints."""

    def test_find_similar_nodes(self, test_app: TestClient):
        """Find similar nodes by name."""
        response = test_app.post(
            "/api/similar",
            json={"name": "Test Org", "threshold": 0.5}
        )
        assert response.status_code == 200
        data = response.json()
        assert "similar_nodes" in data

    def test_find_similar_nodes_with_type_filter(self, test_app: TestClient):
        """Find similar nodes filtered by type."""
        response = test_app.post(
            "/api/similar",
            json={"name": "Test", "node_type": "Actor", "threshold": 0.3}
        )
        assert response.status_code == 200
        data = response.json()
        for node in data.get("similar_nodes", []):
            assert node["type"] == "Actor"

    def test_find_similar_nodes_batch(self, test_app: TestClient):
        """Batch similarity search."""
        response = test_app.post(
            "/api/similar/batch",
            json={"names": ["Test Org", "Test Proj"], "threshold": 0.3}
        )
        assert response.status_code == 200
        data = response.json()
        assert "results" in data


class TestStatisticsEndpoints:
    """Tests for statistics and metadata endpoints."""

    def test_get_graph_stats(self, test_app: TestClient):
        """Get graph statistics."""
        response = test_app.get("/api/stats")
        assert response.status_code == 200
        data = response.json()
        assert "total_nodes" in data
        assert "total_edges" in data
        assert data["total_nodes"] == 3
        assert data["total_edges"] == 2

    def test_get_graph_stats_with_community_filter(self, test_app: TestClient):
        """Get stats filtered by community."""
        response = test_app.get("/api/stats?communities=TestCommunity")
        assert response.status_code == 200
        data = response.json()
        assert data["total_nodes"] == 2  # Only nodes in TestCommunity

    def test_list_node_types(self, test_app: TestClient):
        """List available node types."""
        response = test_app.get("/api/meta/node-types")
        assert response.status_code == 200
        data = response.json()
        assert "node_types" in data
        type_values = [t["type"] for t in data["node_types"]]
        assert "Actor" in type_values
        assert "Initiative" in type_values

    def test_list_relationship_types(self, test_app: TestClient):
        """List available relationship types."""
        response = test_app.get("/api/meta/relationship-types")
        assert response.status_code == 200
        data = response.json()
        assert "relationship_types" in data
        type_values = [t["type"] for t in data["relationship_types"]]
        assert "IMPLEMENTS" in type_values


class TestExportEndpoints:
    """Tests for export endpoints."""

    def test_export_graph(self, test_app: TestClient):
        """Export entire graph."""
        response = test_app.get("/api/export")
        assert response.status_code == 200
        data = response.json()
        assert "nodes" in data
        assert "edges" in data
        assert len(data["nodes"]) == 3
        assert len(data["edges"]) == 2

    def test_export_graph_legacy_endpoint(self, test_app: TestClient):
        """Legacy export_graph endpoint works."""
        response = test_app.get("/export_graph")
        assert response.status_code == 200
        data = response.json()
        assert "nodes" in data


class TestExecuteToolEndpoint:
    """Tests for direct tool execution endpoint."""

    def test_execute_tool_search(self, test_app: TestClient):
        """Execute search_graph tool directly."""
        response = test_app.post(
            "/execute_tool",
            json={
                "tool_name": "search_graph",
                "arguments": {"query": "test"}
            }
        )
        assert response.status_code == 200
        data = response.json()
        assert "nodes" in data

    def test_execute_tool_not_found(self, test_app: TestClient):
        """Execute non-existent tool returns 404."""
        response = test_app.post(
            "/execute_tool",
            json={
                "tool_name": "nonexistent_tool",
                "arguments": {}
            }
        )
        assert response.status_code == 404

    def test_execute_tool_no_name(self, test_app: TestClient):
        """Execute tool without name returns 400."""
        response = test_app.post(
            "/execute_tool",
            json={"arguments": {}}
        )
        assert response.status_code == 400
