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
  reading server-owned state, for a session with a browser and for one no
  browser has ever opened
- clear_visualization gating on browser presence
- MCP push enqueues a command when a browser is connected
- Invalid session ID rejection
"""

import asyncio

import pytest

from fastapi.testclient import TestClient


def _open_browser(test_app: TestClient, session_id: str) -> None:
    """Simulate a browser holding the legacy push stream open for *session_id*.

    In production the browser opens ``GET /sessions/{id}/stream`` on load, which
    calls ``registry.get_or_create``. Tests can't easily hold an SSE stream open,
    so we materialise the registry entry directly. The file-backed store lives in
    a shared temp dir, so first clear any session a previous run left on disk to
    keep the id a clean slate.

    ``delete_session`` is async (R10: it takes the same per-session lock as
    ``apply_ops`` to avoid a delete/in-flight-batch race) but this helper is
    called from plain sync test functions, so it is run to completion via a
    throwaway event loop rather than awaited.
    """
    asyncio.run(test_app.app.state.session_manager.delete_session(session_id))
    test_app.app.state.session_registry.get_or_create(session_id)


@pytest.fixture
def headless_session(test_app: TestClient):
    """Create sessions the way an unattended agent does: over MCP, no browser.

    Deliberately leaves the push registry untouched — these tests are about a
    session that exists server-side and has never had a client.

    Each call mints a fresh random id in the file-backed store, which is shared
    across test files and runs and counts toward ``max_sessions``, so the
    fixture deletes what it created rather than leaving a session per run
    behind.
    """
    created = []

    def _create() -> str:
        result = test_app.app.state.tools_map["create_visualization_session"]()
        assert result["success"] is True
        session_id = result["session"]["session_id"]
        created.append(session_id)
        return session_id

    yield _create

    for session_id in created:
        asyncio.run(test_app.app.state.session_manager.delete_session(session_id))


def _add_nodes(test_app: TestClient, session_id: str, node_ids) -> None:
    """Materialise the store session (as the op stream would) and add node refs."""
    test_app.app.state.session_manager.get_or_create(session_id)
    resp = test_app.post(
        f"/api/sessions/{session_id}/ops",
        json={
            "client_id": "setup",
            "ops": [{"op": "nodes_added", "node_ids": list(node_ids)}],
        },
    )
    assert resp.status_code == 200


class TestConnectToVisualizationSession:
    """MCP tool: connect_to_visualization_session"""

    def test_returns_not_connected_for_unknown_session(self, test_app: TestClient):
        # Absence assertion against the shared, cross-run store — clear any
        # leftover first (see _open_browser).
        asyncio.run(test_app.app.state.session_manager.delete_session("9999-9999"))
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
        # Reachable through the legacy channel, no op-stream presence: the
        # message must not assert a connected client the count contradicts.
        assert data["connected_clients"] == 0
        assert "legacy push channel" in data["message"]
        assert "connected_clients is 0" in data["message"]

    def test_connected_with_empty_store_reports_zero_nodes(self, test_app: TestClient):
        """A browser can be connected before anything is saved server-side.

        The op stream materialises the store entry lazily (on the first change),
        so this browser is reachable through the legacy push channel while the
        tools that act on stored state still have nothing to act on. The result
        must say so: ``has_stored_state`` false, and no claim that those tools
        work.
        """
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
        assert data["has_stored_state"] is False
        assert "no stored state yet" in data["message"]

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
            json={
                "client_id": "setup",
                "ops": [{"op": "selection_claimed", "element_ids": ["node-1"]}],
            },
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

    def test_hidden_nodes_are_excluded_from_visible_node_ids(
        self, test_app: TestClient
    ):
        session_id = "2222-4444"
        _open_browser(test_app, session_id)
        _add_nodes(test_app, session_id, ["node-1", "node-2"])
        test_app.post(
            f"/api/sessions/{session_id}/ops",
            json={
                "client_id": "setup",
                "ops": [{"op": "nodes_hidden", "node_ids": ["node-2"]}],
            },
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


class TestHeadlessSessionAddressability:
    """A session with no connected client is still a session.

    Both read tools used to gate on the legacy push registry, which only records
    that a browser holds the SSE stream open. A session created and populated
    over MCP — the unattended-agent path — therefore reported "not found" while
    its state was there and ``get_visualization_layout`` read it fine.
    """

    def test_connect_finds_a_session_no_browser_has_ever_opened(
        self, test_app: TestClient, headless_session
    ):
        session_id = headless_session()
        _add_nodes(test_app, session_id, ["node-1"])

        data = test_app.post(
            "/execute_tool",
            json={
                "tool_name": "connect_to_visualization_session",
                "arguments": {"session_id": session_id},
            },
        ).json()

        assert data["connected"] is True
        assert data["session_id"] == session_id
        assert data["visible_node_count"] == 1
        # Addressable, but nobody is watching: the caller can tell the two apart.
        assert data["connected_clients"] == 0

    def test_state_reads_back_for_a_session_no_browser_has_ever_opened(
        self, test_app: TestClient, headless_session
    ):
        session_id = headless_session()
        _add_nodes(test_app, session_id, ["node-1", "node-2"])

        data = test_app.post(
            "/execute_tool",
            json={
                "tool_name": "get_visualization_session_state",
                "arguments": {"session_id": session_id},
            },
        ).json()

        assert data["session_id"] == session_id
        assert data["visible_node_ids"] == ["node-1", "node-2"]
        assert data["node_count"] == 2
        assert data["selected_node_ids"] == []

    def test_session_stays_addressable_after_its_client_goes_away(
        self, test_app: TestClient, headless_session
    ):
        """A disconnected client is not a deleted session.

        The registry entry is evicted when the browser stops holding the stream
        open; the stored session outlives it and must keep resolving.
        """
        session_id = headless_session()
        _add_nodes(test_app, session_id, ["node-1"])
        registry = test_app.app.state.session_registry
        registry.get_or_create(session_id)
        registry._sessions[session_id]["last_seen"] -= 10_000
        registry.cleanup_stale()
        assert registry.session_exists(session_id) is False

        data = test_app.post(
            "/execute_tool",
            json={
                "tool_name": "connect_to_visualization_session",
                "arguments": {"session_id": session_id},
            },
        ).json()

        assert data["connected"] is True
        assert data["visible_node_count"] == 1

    def test_headless_session_reports_stored_state_and_no_client(
        self, test_app: TestClient, headless_session
    ):
        """The two facts are reported separately because they fail apart.

        A session created over MCP has stored state and no canvas; a browser's
        unchanged session has a canvas and no stored state. Collapsing them into
        one boolean gives the caller advice that is wrong in one of the two.
        """
        session_id = headless_session()

        data = test_app.post(
            "/execute_tool",
            json={
                "tool_name": "connect_to_visualization_session",
                "arguments": {"session_id": session_id},
            },
        ).json()

        assert data["has_stored_state"] is True
        assert data["connected_clients"] == 0
        assert "no client connected" in data["message"]

    def test_a_client_on_the_op_stream_counts_as_connected(
        self, test_app: TestClient, headless_session
    ):
        """Presence, not a legacy registry entry, is what makes a canvas live.

        A browser that has moved to the op stream reports presence and holds no
        registry entry, so a result reported off the registry alone would call
        this session unwatched.
        """
        session_id = headless_session()
        manager = test_app.app.state.session_manager
        manager.presence.join(session_id, "client-1", "Tester")

        data = test_app.post(
            "/execute_tool",
            json={
                "tool_name": "connect_to_visualization_session",
                "arguments": {"session_id": session_id},
            },
        ).json()

        assert data["connected_clients"] == 1
        assert data["has_stored_state"] is True
        assert "1 connected client(s)" in data["message"]

    def test_a_session_that_does_not_exist_is_still_reported_not_found(
        self, test_app: TestClient
    ):
        """The relaxed gate must not turn any well-formed id into a hit."""
        unknown = "1010-2020-3030-4040"
        # The file-backed store is shared across test files and runs, and this
        # test asserts the *absence* of a session — so clear any leftover first
        # (same hazard _open_browser documents).
        asyncio.run(test_app.app.state.session_manager.delete_session(unknown))

        connect = test_app.post(
            "/execute_tool",
            json={
                "tool_name": "connect_to_visualization_session",
                "arguments": {"session_id": unknown},
            },
        ).json()
        state = test_app.post(
            "/execute_tool",
            json={
                "tool_name": "get_visualization_session_state",
                "arguments": {"session_id": unknown},
            },
        ).json()

        assert connect["connected"] is False
        assert "not found" in connect["message"].lower()
        assert "not found" in state["error"].lower()


class TestClearVisualization:
    """MCP tool: clear_visualization.

    A write tool, so ``/execute_tool`` gates it behind auth — call it directly
    from the registered tools map (exposed on app state) instead.
    """

    def test_clear_unknown_session_errors(self, test_app: TestClient):
        """An id that names no session is not-found, per contract §8 — not
        "exists but unwatched"."""
        clear = test_app.app.state.tools_map["clear_visualization"]
        asyncio.run(test_app.app.state.session_manager.delete_session("0000-1111"))

        data = clear(visualization_session_id="0000-1111")

        assert data["success"] is False
        assert "not found" in data["error"].lower()

    def test_clear_reports_the_missing_push_channel_for_a_stored_session(
        self, test_app: TestClient, headless_session
    ):
        """A session that exists but has no legacy push channel is a different
        failure from one that does not exist, and says so."""
        clear = test_app.app.state.tools_map["clear_visualization"]
        session_id = headless_session()

        data = clear(visualization_session_id=session_id)

        assert data["success"] is False
        assert "not found" not in data["error"].lower()
        assert "legacy push channel" in data["error"].lower()

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

    def test_get_related_nodes_with_session_id_enqueues_command(
        self, test_app: TestClient
    ):
        session_id = "4444-5555"
        _open_browser(test_app, session_id)

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

        registry = test_app.app.state.session_registry
        cmd = registry._sessions[session_id]["queue"].get_nowait()
        assert cmd["type"] == "tool_result"
        assert cmd["tool"] == "get_related_nodes"
        # Default action must be additive (not replace)
        assert cmd["result"].get("action") == "add_to_visualization"

    def test_get_saved_view_with_session_id_enqueues_command(
        self, test_app: TestClient
    ):
        session_id = "6666-7777"
        _open_browser(test_app, session_id)

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

        registry = test_app.app.state.session_registry
        cmd = registry._sessions[session_id]["queue"].get_nowait()
        assert cmd["type"] == "tool_result"
        assert cmd["tool"] == "get_saved_view"
