"""
Tests for the legacy MCP visualization-push channel and the MCP session tools.

The browser no longer uploads canvas state (the ``PATCH /sessions/{id}/state``
shim was removed in step 8). The remaining legacy endpoint is the push stream
``GET /sessions/{id}/stream``; a registry entry simply signals that a browser is
connected to receive MCP pushes. Session *state* is server-owned now (design
§3.8): the MCP query tools read visible nodes from the shared-session store and
the current selection from the advisory claim map.

Covers:
- MCP session tools connect_to_visualization_session / get_visualization_session_state
  reading server-owned state
- clear_visualization gating on browser presence
- MCP push enqueues a command when a browser is connected
- Invalid session ID rejection
"""

from fastapi.testclient import TestClient


def _open_browser(test_app: TestClient, session_id: str) -> None:
    """Simulate a browser holding the legacy push stream open for *session_id*.

    In production the browser opens ``GET /sessions/{id}/stream`` on load, which
    calls ``registry.get_or_create``. Tests can't easily hold an SSE stream open,
    so we materialise the registry entry directly. The file-backed store lives in
    a shared temp dir, so first clear any session a previous run left on disk to
    keep the id a clean slate.
    """
    test_app.app.state.session_manager.delete_session(session_id)
    test_app.app.state.session_registry.get_or_create(session_id)


def _add_nodes(test_app: TestClient, session_id: str, node_ids) -> None:
    """Materialise the store session (as the op stream would) and add node refs."""
    test_app.app.state.session_manager.get_or_create(session_id)
    resp = test_app.post(
        f"/api/sessions/{session_id}/ops",
        json={"client_id": "setup", "ops": [{"op": "nodes_added", "node_ids": list(node_ids)}]},
    )
    assert resp.status_code == 200


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

    def test_returns_connected_for_open_session(self, test_app: TestClient):
        session_id = "5678-1234"
        _open_browser(test_app, session_id)
        _add_nodes(test_app, session_id, ["n1"])

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

    def test_connected_with_empty_store_reports_zero_nodes(self, test_app: TestClient):
        """A browser can be connected before anything is saved server-side."""
        session_id = "5555-6666"
        _open_browser(test_app, session_id)

        data = test_app.post(
            "/execute_tool",
            json={
                "tool_name": "connect_to_visualization_session",
                "arguments": {"session_id": session_id},
            },
        ).json()
        assert data["connected"] is True
        assert data["visible_node_count"] == 0

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
        assert "error" in response.json()

    def test_returns_server_owned_state_for_open_session(self, test_app: TestClient):
        session_id = "2222-3333"
        _open_browser(test_app, session_id)
        _add_nodes(test_app, session_id, ["node-1", "node-2"])
        # The current selection is expressed as an advisory claim (design §3.8).
        test_app.post(
            f"/api/sessions/{session_id}/ops",
            json={"client_id": "setup", "ops": [{"op": "selection_claimed", "element_ids": ["node-1"]}]},
        )

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
        assert data["node_count"] == 2
        assert data["selected_node_ids"] == ["node-1"]

    def test_hidden_nodes_are_excluded_from_visible_node_ids(self, test_app: TestClient):
        session_id = "2222-4444"
        _open_browser(test_app, session_id)
        _add_nodes(test_app, session_id, ["node-1", "node-2"])
        test_app.post(
            f"/api/sessions/{session_id}/ops",
            json={"client_id": "setup", "ops": [{"op": "nodes_hidden", "node_ids": ["node-2"]}]},
        )

        response = test_app.post(
            "/execute_tool",
            json={
                "tool_name": "get_visualization_session_state",
                "arguments": {"session_id": session_id},
            },
        )
        assert response.status_code == 200
        data = response.json()
        assert data["visible_node_ids"] == ["node-1"]
        assert data["node_count"] == 1


class TestClearVisualization:
    """MCP tool: clear_visualization.

    A write tool, so ``/execute_tool`` gates it behind auth — call it directly
    from the registered tools map (exposed on app state) instead.
    """

    def test_clear_unknown_session_errors(self, test_app: TestClient):
        clear = test_app.app.state.tools_map["clear_visualization"]
        data = clear(visualization_session_id="0000-1111")
        assert data["success"] is False
        assert "not found" in data["error"].lower()

    def test_clear_open_session_succeeds(self, test_app: TestClient):
        # The push transport itself is covered by the search_graph push tests; a
        # direct call has no running loop to enqueue on, so assert the tool's own
        # gating: an open session clears successfully.
        session_id = "3333-4444"
        _open_browser(test_app, session_id)

        clear = test_app.app.state.tools_map["clear_visualization"]
        data = clear(visualization_session_id=session_id)
        assert data["success"] is True


class TestVisualizationSessionIdPush:
    """MCP tools push a result to the browser queue when a session is connected."""

    def test_search_graph_with_session_id_enqueues_command(self, test_app: TestClient):
        session_id = "7777-8888"
        _open_browser(test_app, session_id)

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

        registry = test_app.app.state.session_registry
        assert not registry._sessions[session_id]["queue"].empty()
        cmd = registry._sessions[session_id]["queue"].get_nowait()
        assert cmd["type"] == "tool_result"
        assert cmd["tool"] == "search_graph"

    def test_get_related_nodes_with_session_id_enqueues_command(self, test_app: TestClient):
        session_id = "4444-5555"
        _open_browser(test_app, session_id)

        response = test_app.post(
            "/execute_tool",
            json={
                "tool_name": "get_related_nodes",
                "arguments": {"node_id": "node-1", "visualization_session_id": session_id},
            },
        )
        assert response.status_code == 200

        registry = test_app.app.state.session_registry
        cmd = registry._sessions[session_id]["queue"].get_nowait()
        assert cmd["type"] == "tool_result"
        assert cmd["tool"] == "get_related_nodes"
        # Default action must be additive (not replace)
        assert cmd["result"].get("action") == "add_to_visualization"

    def test_get_saved_view_with_session_id_enqueues_command(self, test_app: TestClient):
        session_id = "6666-7777"
        _open_browser(test_app, session_id)

        response = test_app.post(
            "/execute_tool",
            json={
                "tool_name": "get_saved_view",
                "arguments": {"name": "nonexistent-view", "visualization_session_id": session_id},
            },
        )
        assert response.status_code == 200

        registry = test_app.app.state.session_registry
        cmd = registry._sessions[session_id]["queue"].get_nowait()
        assert cmd["type"] == "tool_result"
        assert cmd["tool"] == "get_saved_view"
