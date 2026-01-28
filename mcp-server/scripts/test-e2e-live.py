#!/usr/bin/env python3
"""
E2E Tests with Live Backend

These tests verify the full stack by making real HTTP requests to a running server.
They test REST API, MCP endpoints, and the execute_tool endpoint.

Usage:
    # Start server first:
    cd mcp-server && uvicorn app_host.server:get_app --factory --port 8000

    # Run tests:
    python scripts/test-e2e-live.py

    # Or with custom URL:
    E2E_SERVER_URL=http://localhost:8080 python scripts/test-e2e-live.py
"""

import os
import sys
import json
import time
import requests
from typing import Optional

SERVER_URL = os.environ.get("E2E_SERVER_URL", "http://localhost:8000")
API_PREFIX = "/api/v1"


def log(message: str, level: str = "INFO"):
    """Log a message with timestamp."""
    timestamp = time.strftime("%H:%M:%S")
    print(f"[{timestamp}] [{level}] {message}")


def check_server_health() -> bool:
    """Check if the server is running and healthy."""
    try:
        response = requests.get(f"{SERVER_URL}/health", timeout=5)
        if response.status_code == 200:
            data = response.json()
            log(f"Server healthy: {data['graph_nodes']} nodes, {data['graph_edges']} edges")
            return True
    except requests.exceptions.RequestException as e:
        log(f"Server health check failed: {e}", "ERROR")
    return False


def test_root_endpoint():
    """Test the root endpoint returns API info."""
    log("Testing root endpoint...")
    response = requests.get(f"{SERVER_URL}/")
    assert response.status_code == 200, f"Expected 200, got {response.status_code}"
    data = response.json()
    assert "name" in data, "Missing 'name' in response"
    assert "endpoints" in data, "Missing 'endpoints' in response"
    log("✓ Root endpoint OK")


def test_health_endpoint():
    """Test the health check endpoint."""
    log("Testing health endpoint...")
    response = requests.get(f"{SERVER_URL}/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "healthy"
    assert "graph_nodes" in data
    assert "graph_edges" in data
    log("✓ Health endpoint OK")


def test_rest_search():
    """Test REST API search endpoint."""
    log("Testing REST search...")
    response = requests.get(
        f"{SERVER_URL}{API_PREFIX}/search",
        params={"query": "", "limit": 5}
    )
    assert response.status_code == 200, f"Search failed: {response.text}"
    data = response.json()
    assert "nodes" in data
    assert "edges" in data
    log(f"✓ REST search returned {len(data['nodes'])} nodes")


def test_rest_get_stats():
    """Test REST API stats endpoint."""
    log("Testing REST stats...")
    response = requests.get(f"{SERVER_URL}{API_PREFIX}/stats")
    assert response.status_code == 200, f"Stats failed: {response.text}"
    data = response.json()
    assert "total_nodes" in data
    assert "total_edges" in data
    assert "node_types" in data
    log(f"✓ REST stats: {data['total_nodes']} nodes, {data['total_edges']} edges")


def test_execute_tool_search():
    """Test the execute_tool endpoint with search_graph."""
    log("Testing execute_tool with search_graph...")
    response = requests.post(
        f"{SERVER_URL}/execute_tool",
        json={
            "tool_name": "search_graph",
            "arguments": {"query": "", "limit": 5}
        }
    )
    assert response.status_code == 200, f"execute_tool failed: {response.text}"
    data = response.json()
    assert "nodes" in data
    log(f"✓ execute_tool search returned {len(data['nodes'])} nodes")


def test_execute_tool_get_stats():
    """Test the execute_tool endpoint with get_graph_stats."""
    log("Testing execute_tool with get_graph_stats...")
    response = requests.post(
        f"{SERVER_URL}/execute_tool",
        json={
            "tool_name": "get_graph_stats",
            "arguments": {}
        }
    )
    assert response.status_code == 200, f"execute_tool failed: {response.text}"
    data = response.json()
    assert "total_nodes" in data
    log(f"✓ execute_tool stats: {data['total_nodes']} nodes")


def test_execute_tool_invalid_tool():
    """Test execute_tool with invalid tool name returns 404."""
    log("Testing execute_tool with invalid tool...")
    response = requests.post(
        f"{SERVER_URL}/execute_tool",
        json={
            "tool_name": "nonexistent_tool",
            "arguments": {}
        }
    )
    assert response.status_code == 404, f"Expected 404, got {response.status_code}"
    data = response.json()
    assert "error" in data
    log("✓ execute_tool correctly returns 404 for invalid tool")


def test_execute_tool_missing_name():
    """Test execute_tool without tool name returns 400."""
    log("Testing execute_tool without tool name...")
    response = requests.post(
        f"{SERVER_URL}/execute_tool",
        json={"arguments": {}}
    )
    assert response.status_code == 400, f"Expected 400, got {response.status_code}"
    log("✓ execute_tool correctly returns 400 for missing tool name")


def test_export_graph():
    """Test the export_graph endpoint."""
    log("Testing export_graph...")
    response = requests.get(f"{SERVER_URL}/export_graph")
    assert response.status_code == 200, f"Export failed: {response.text}"
    data = response.json()
    assert "nodes" in data
    assert "edges" in data
    log(f"✓ export_graph returned {len(data['nodes'])} nodes")


def test_crud_workflow():
    """Test a complete CRUD workflow via execute_tool."""
    log("Testing CRUD workflow...")

    # 1. Add nodes
    log("  Adding test nodes...")
    response = requests.post(
        f"{SERVER_URL}/execute_tool",
        json={
            "tool_name": "add_nodes",
            "arguments": {
                "nodes": [
                    {
                        "id": "e2e-test-node-1",
                        "name": "E2E Test Actor",
                        "type": "Actor",
                        "description": "Created by e2e test",
                        "communities": ["e2e-test"]
                    },
                    {
                        "id": "e2e-test-node-2",
                        "name": "E2E Test Initiative",
                        "type": "Initiative",
                        "description": "Created by e2e test",
                        "communities": ["e2e-test"]
                    }
                ],
                "edges": [
                    {
                        "source": "e2e-test-node-1",
                        "target": "e2e-test-node-2",
                        "type": "IMPLEMENTS"
                    }
                ]
            }
        }
    )
    assert response.status_code == 200, f"Add nodes failed: {response.text}"
    data = response.json()
    assert data.get("nodes_added", 0) >= 1, f"Expected nodes to be added: {data}"
    log(f"  ✓ Added {data.get('nodes_added', 0)} nodes")

    # 2. Get node details
    log("  Getting node details...")
    response = requests.post(
        f"{SERVER_URL}/execute_tool",
        json={
            "tool_name": "get_node_details",
            "arguments": {"node_id": "e2e-test-node-1"}
        }
    )
    assert response.status_code == 200, f"Get details failed: {response.text}"
    data = response.json()
    assert data.get("id") == "e2e-test-node-1" or data.get("node", {}).get("id") == "e2e-test-node-1"
    log("  ✓ Got node details")

    # 3. Update node
    log("  Updating node...")
    response = requests.post(
        f"{SERVER_URL}/execute_tool",
        json={
            "tool_name": "update_node",
            "arguments": {
                "node_id": "e2e-test-node-1",
                "updates": {
                    "description": "Updated by e2e test"
                }
            }
        }
    )
    assert response.status_code == 200, f"Update failed: {response.text}"
    log("  ✓ Updated node")

    # 4. Get related nodes
    log("  Getting related nodes...")
    response = requests.post(
        f"{SERVER_URL}/execute_tool",
        json={
            "tool_name": "get_related_nodes",
            "arguments": {
                "node_id": "e2e-test-node-1",
                "depth": 1
            }
        }
    )
    assert response.status_code == 200, f"Get related failed: {response.text}"
    data = response.json()
    # Should find the connected node
    node_ids = [n.get("id") for n in data.get("nodes", [])]
    assert "e2e-test-node-2" in node_ids, f"Expected related node, got {node_ids}"
    log("  ✓ Got related nodes")

    # 5. Delete nodes (cleanup)
    log("  Deleting test nodes...")
    response = requests.post(
        f"{SERVER_URL}/execute_tool",
        json={
            "tool_name": "delete_nodes",
            "arguments": {
                "node_ids": ["e2e-test-node-1", "e2e-test-node-2"],
                "confirmed": True
            }
        }
    )
    assert response.status_code == 200, f"Delete failed: {response.text}"
    data = response.json()
    assert data.get("deleted_count", 0) >= 1
    log(f"  ✓ Deleted {data.get('deleted_count', 0)} nodes")

    log("✓ CRUD workflow complete")


def test_rest_vs_mcp_parity():
    """Verify REST and MCP return equivalent data for same operations."""
    log("Testing REST vs MCP parity...")

    # Get stats via REST
    rest_response = requests.get(f"{SERVER_URL}{API_PREFIX}/stats")
    rest_stats = rest_response.json()

    # Get stats via MCP tool
    mcp_response = requests.post(
        f"{SERVER_URL}/execute_tool",
        json={"tool_name": "get_graph_stats", "arguments": {}}
    )
    mcp_stats = mcp_response.json()

    # Compare
    assert rest_stats["total_nodes"] == mcp_stats["total_nodes"], \
        f"Node count mismatch: REST={rest_stats['total_nodes']}, MCP={mcp_stats['total_nodes']}"
    assert rest_stats["total_edges"] == mcp_stats["total_edges"], \
        f"Edge count mismatch: REST={rest_stats['total_edges']}, MCP={mcp_stats['total_edges']}"

    log("✓ REST vs MCP parity verified")


def run_all_tests():
    """Run all e2e tests."""
    log("=" * 60)
    log(f"E2E Tests - Server: {SERVER_URL}")
    log("=" * 60)

    if not check_server_health():
        log("Server is not running. Please start the server first:", "ERROR")
        log(f"  cd mcp-server && uvicorn app_host.server:get_app --factory --port 8000", "ERROR")
        sys.exit(1)

    tests = [
        test_root_endpoint,
        test_health_endpoint,
        test_rest_search,
        test_rest_get_stats,
        test_execute_tool_search,
        test_execute_tool_get_stats,
        test_execute_tool_invalid_tool,
        test_execute_tool_missing_name,
        test_export_graph,
        test_crud_workflow,
        test_rest_vs_mcp_parity,
    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            log(f"✗ {test.__name__} FAILED: {e}", "ERROR")
            failed += 1
        except Exception as e:
            log(f"✗ {test.__name__} ERROR: {e}", "ERROR")
            failed += 1

    log("=" * 60)
    log(f"Results: {passed} passed, {failed} failed")
    log("=" * 60)

    sys.exit(0 if failed == 0 else 1)


if __name__ == "__main__":
    run_all_tests()
