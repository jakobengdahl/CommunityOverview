"""
Tests for /sessions/{session_id}/state and /sessions/{session_id}/stream endpoints.

Covers:
- State upload (PATCH) and retrieval via session registry
- MCP session tools: connect_to_visualization_session and get_visualization_session_state
- Invalid session ID rejection
"""

import pytest
import json
import os
from unittest.mock import patch

from fastapi.testclient import TestClient
from backend.api_host import create_app, AppConfig


class TestSessionStateEndpoint:
    """PATCH /sessions/{session_id}/state"""

    def test_patch_state_creates_session_and_stores_state(self, test_app: TestClient):
        session_id = "1234-5678"
        state = {"visible_node_ids": ["a", "b"], "node_count": 2}

        response = test_app.patch(f"/sessions/{session_id}/state", json=state)
        assert response.status_code == 200
        assert response.json()["ok"] is True

    def test_patch_state_invalid_format_rejected(self, test_app: TestClient):
        response = test_app.patch("/sessions/badformat/state", json={})
        assert response.status_code == 400
        assert "invalid" in response.json()["error"].lower()

    def test_patch_state_non_object_body_rejected(self, test_app: TestClient):
        response = test_app.patch(
            "/sessions/1234-5678/state",
            content=b'"just a string"',
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 400

    def test_state_retrievable_via_mcp_session_tool(self, test_app: TestClient):
        """State uploaded via PATCH should be visible to the MCP session tool."""
        session_id = "4321-8765"
        state = {"visible_node_ids": ["x", "y", "z"], "node_count": 3}
        test_app.patch(f"/sessions/{session_id}/state", json=state)

        response = test_app.post(
            "/execute_tool",
            json={
                "tool_name": "get_visualization_session_state",
                "arguments": {"session_id": session_id},
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["session_id"] == session_id
        assert data["visible_node_ids"] == ["x", "y", "z"]
        assert data["node_count"] == 3


class TestConnectToVisualizationSession:
    """MCP tool: connect_to_visualization_session"""

    def test_returns_not_connected_for_unknown_session(self, test_app: TestClient):
        response = test_app.post(
            "/execute_tool",
            json={
                "tool_name": "connect_to_visualization_session",
                "arguments": {"session_id": "9999-9999"},
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["connected"] is False
        assert "not found" in data["message"].lower()

    def test_returns_connected_for_active_session(self, test_app: TestClient):
        session_id = "5678-1234"
        test_app.patch(f"/sessions/{session_id}/state", json={"visible_node_ids": ["n1"], "node_count": 1})

        response = test_app.post(
            "/execute_tool",
            json={
                "tool_name": "connect_to_visualization_session",
                "arguments": {"session_id": session_id},
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["connected"] is True
        assert data["session_id"] == session_id
        assert data["visible_node_count"] == 1

    def test_invalid_session_id_format_returns_error(self, test_app: TestClient):
        response = test_app.post(
            "/execute_tool",
            json={
                "tool_name": "connect_to_visualization_session",
                "arguments": {"session_id": "not-valid"},
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["connected"] is False
        assert "Invalid" in data["error"]


class TestGetVisualizationSessionState:
    """MCP tool: get_visualization_session_state"""

    def test_returns_error_for_unknown_session(self, test_app: TestClient):
        response = test_app.post(
            "/execute_tool",
            json={
                "tool_name": "get_visualization_session_state",
                "arguments": {"session_id": "0000-0000"},
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert "error" in data

    def test_returns_state_for_known_session(self, test_app: TestClient):
        session_id = "2222-3333"
        state = {
            "visible_node_ids": ["node-1", "node-2"],
            "selected_node_ids": ["node-1"],
            "node_count": 2,
        }
        test_app.patch(f"/sessions/{session_id}/state", json=state)

        response = test_app.post(
            "/execute_tool",
            json={
                "tool_name": "get_visualization_session_state",
                "arguments": {"session_id": session_id},
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["session_id"] == session_id
        assert data["visible_node_ids"] == ["node-1", "node-2"]
        assert data["selected_node_ids"] == ["node-1"]
