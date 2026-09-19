"""Focused tests for MCP visualization push delivery reporting."""

from unittest.mock import MagicMock, Mock

from backend.core import GraphStorage
from backend.core.session_manager import SessionManager
from backend.core.session_store import InMemorySessionPersistenceBackend, SessionStore
from backend.service import GraphService, register_mcp_tools


def _tools_with_manager(tmp_path):
    storage = GraphStorage(json_path=str(tmp_path / "g.json"))
    service = GraphService(storage)
    manager = SessionManager(SessionStore(InMemorySessionPersistenceBackend()))

    mock_mcp = Mock()
    mock_mcp.tool = MagicMock(return_value=lambda f: f)
    tools = register_mcp_tools(mock_mcp, service, session_manager=manager)
    tools["add_nodes"](
        nodes=[{"id": "node-1", "type": "Actor", "name": "Visible Node"}],
        edges=[],
    )
    return tools, manager


def test_search_graph_warns_when_visualization_session_is_unwatched(tmp_path):
    tools, manager = _tools_with_manager(tmp_path)
    session = manager.create_session()

    result = tools["search_graph"](
        query="Visible Node", visualization_session_id=session.id
    )

    assert result["total"] == 1
    delivery = result["visualization_delivery"]
    assert delivery["requested"] is True
    assert delivery["delivered"] is False
    assert delivery["status"] == "not_delivered"
    assert delivery["live_consumers"] == 0
    assert "live visualization consumer" in delivery["warning"]


def test_search_graph_reports_delivery_to_live_visualization_consumer(tmp_path):
    tools, manager = _tools_with_manager(tmp_path)
    session = manager.create_session()
    subscription, _ = manager.connect(session.id, "browser-client", "Browser")

    result = tools["search_graph"](
        query="Visible Node", visualization_session_id=session.id
    )

    delivery = result["visualization_delivery"]
    assert delivery["delivered"] is True
    assert delivery["status"] == "delivered"
    assert delivery["live_consumers"] == 1
    assert "warning" not in delivery

    events = []
    while not subscription.queue.empty():
        events.append(subscription.queue.get_nowait())
    assert any(
        event.get("type") == "command" and event["command"]["tool"] == "search_graph"
        for event in events
    )
