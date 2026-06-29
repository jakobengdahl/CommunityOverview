"""
Tests for /sessions/{session_id}/state and /sessions/{session_id}/stream endpoints.

Covers:
- State upload (PATCH) and retrieval via session registry
- MCP session tools: connect_to_visualization_session and get_visualization_session_state
- Invalid session ID rejection
"""

from fastapi.testclient import TestClient


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
        test_app.patch(
            f"/sessions/{session_id}/state",
            json={"visible_node_ids": ["n1"], "node_count": 1},
        )

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


class TestVisualizationSessionIdPush:
    """MCP tools push result to session queue when visualization_session_id is set."""

    def test_search_graph_with_session_id_enqueues_command(self, test_app: TestClient):
        """search_graph should push to the session when visualization_session_id is set."""
        session_id = "7777-8888"
        test_app.patch(f"/sessions/{session_id}/state", json={"visible_node_ids": []})

        response = test_app.post(
            "/execute_tool",
            json={
                "tool_name": "search_graph",
                "arguments": {
                    "query": "test",
                    "action": "add_to_visualization",
                    "visualization_session_id": session_id,
                },
            },
        )
        assert response.status_code == 200

        # The session queue should have received a command
        registry = test_app.app.state.session_registry
        assert not registry._sessions[session_id]["queue"].empty()
        cmd = registry._sessions[session_id]["queue"].get_nowait()
        assert cmd["type"] == "tool_result"
        assert cmd["tool"] == "search_graph"

    def test_get_related_nodes_with_session_id_enqueues_command(
        self, test_app: TestClient
    ):
        """get_related_nodes with visualization_session_id should push an additive command."""
        session_id = "4444-5555"
        test_app.patch(f"/sessions/{session_id}/state", json={"visible_node_ids": []})

        response = test_app.post(
            "/execute_tool",
            json={
                "tool_name": "get_related_nodes",
                "arguments": {
                    "node_id": "node-1",
                    "visualization_session_id": session_id,
                },
            },
        )
        assert response.status_code == 200

        # Enqueued command should have action=add_to_visualization (injected by default)
        registry = test_app.app.state.session_registry
        assert not registry._sessions[session_id]["queue"].empty()
        cmd = registry._sessions[session_id]["queue"].get_nowait()
        assert cmd["type"] == "tool_result"
        assert cmd["tool"] == "get_related_nodes"
        # Default action must be additive (not replace)
        assert cmd["result"].get("action") == "add_to_visualization"

    def test_get_saved_view_with_session_id_enqueues_command(self, test_app: TestClient):
        """get_saved_view with visualization_session_id should push to the session."""
        session_id = "6666-7777"
        test_app.patch(f"/sessions/{session_id}/state", json={"visible_node_ids": []})

        response = test_app.post(
            "/execute_tool",
            json={
                "tool_name": "get_saved_view",
                "arguments": {
                    "name": "nonexistent-view",
                    "visualization_session_id": session_id,
                },
            },
        )
        assert response.status_code == 200

        # The session queue receives a command regardless of whether the view exists
        registry = test_app.app.state.session_registry
        assert not registry._sessions[session_id]["queue"].empty()
        cmd = registry._sessions[session_id]["queue"].get_nowait()
        assert cmd["type"] == "tool_result"
        assert cmd["tool"] == "get_saved_view"

    def test_session_state_body_size_limit(self, test_app: TestClient):
        """PATCH /sessions/{id}/state should reject oversized bodies."""
        session_id = "1111-9999"
        # ~390 KB of JSON — above the 256 KB cap (50k items ≈ 195 KB, 100k ≈ 390 KB)
        large_payload = {"visible_node_ids": ["x"] * 100000}
        response = test_app.patch(
            f"/sessions/{session_id}/state", json=large_payload
        )
        assert response.status_code == 413

    def test_session_count_cap_returns_503(self, test_app: TestClient):
        """PATCH /state must return 503 when the session registry is full."""
        registry = test_app.app.state.session_registry
        # Fill registry to the cap with dummy entries (bypassing the normal path
        # so we don't need 10 000 HTTP requests).
        from backend.api_host.server import _SESSION_MAX_COUNT

        original = dict(registry._sessions)
        try:
            for i in range(_SESSION_MAX_COUNT - len(registry._sessions)):
                fake_id = f"{i:04d}-{i:04d}"
                registry._sessions[fake_id] = {
                    "queue": __import__("asyncio").Queue(),
                    "state": {},
                    "created_at": 0,
                    "last_seen": 0,
                }
            assert registry.session_count >= _SESSION_MAX_COUNT

            # A new session ID that is not already in the registry
            response = test_app.patch("/sessions/8888-9999/state", json={})
            assert response.status_code == 503
        finally:
            registry._sessions.clear()
            registry._sessions.update(original)
