"""A push carrying ``visualization_session_id`` must report whether it landed.

The motivating scenario: an unattended routine refreshes a canvas every night by
calling ``search_graph`` with a ``visualization_session_id``. Nobody has that
session open, so the command reaches no consumer — and because a push writes no
session state (only ``add_nodes_to_session`` writes ``node_refs``), reading the
session back afterwards shows nothing either. The routine reported a refreshed
view every night while publishing nothing.

So the invariant under test is not "a push succeeds" but "the result says which
of the two delivery paths took it": the legacy single-consumer push registry, or
the shared-session hub with at least one client connected. Both paths run against
the real ``SessionRegistry`` / ``SessionManager`` rather than a stub that answers
the question the assertion asks, and each delivered case drains the consumer it
claims to have reached.

``clear_visualization`` also pushes, but it already refuses unless a browser
holds the legacy channel, so it is self-reporting already and its result shape is
pinned here as unchanged.
"""

import asyncio
import os
from unittest.mock import MagicMock, Mock

import pytest

from backend.core import GraphStorage
from backend.core.session_manager import SessionManager
from backend.core.session_registry import SessionRegistry
from backend.core.session_store import (
    InMemorySessionPersistenceBackend,
    SessionStore,
)
from backend.service import GraphService, register_mcp_tools

# A well-formed id (SESSION_ID_RE) that no session is ever created for, so the
# "nothing knows this id" case is not confused with a formatting rejection.
UNKNOWN_SESSION_ID = "9999-8888-7777-6666"


def _wire(tmp_path, *, with_registry=True, session_manager=None):
    storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
    service = GraphService(storage)
    registry = SessionRegistry() if with_registry else None
    manager = (
        session_manager
        if session_manager is not None
        else SessionManager(SessionStore(InMemorySessionPersistenceBackend()))
    )
    mock_mcp = Mock()
    mock_mcp.tool = MagicMock(return_value=lambda f: f)
    tools = register_mcp_tools(
        mock_mcp, service, session_registry=registry, session_manager=manager
    )
    tools["add_nodes"](
        nodes=[
            {"id": "alpha", "type": "Actor", "name": "Alpha"},
            {"id": "beta", "type": "Actor", "name": "Beta"},
        ],
        edges=[{"source": "alpha", "target": "beta", "type": "RELATES_TO"}],
    )
    return tools, registry, manager


@pytest.fixture
def wired(tmp_path):
    return _wire(tmp_path)


def _new_session(tools):
    return tools["create_visualization_session"]()["session"]["session_id"]


def _drain(queue):
    out = []
    while True:
        try:
            out.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            return out


def _search(tools, session_id):
    return tools["search_graph"](query="Alpha", visualization_session_id=session_id)


# ---------------------------------------------------------------------------
# Undelivered
# ---------------------------------------------------------------------------


def test_stored_session_with_no_consumer_reports_undelivered(wired):
    """The motivating case: a session exists, nobody is watching it.

    The hub accepts the publish because the session is in the store, which is
    exactly why "the hub took it" cannot stand in for delivery: there is no
    subscriber behind it.
    """
    tools, _registry, manager = wired
    session_id = _new_session(tools)

    result = _search(tools, session_id)

    push = result["visualization_push"]
    assert push["delivered"] is False
    assert push["hub_published"] is True
    assert push["connected_clients"] == 0
    assert push["registry_enqueued"] is False
    assert push["warning"]
    assert "add_nodes_to_session" in push["warning"]
    # The push left no trace to read back — the half of the defect that makes
    # the report the only way to notice.
    assert manager.get_session(session_id).state.get("node_refs", []) == []
    # The search itself still succeeded; only the delivery is reported as failed.
    assert result["nodes"]


def test_unknown_session_reports_undelivered_on_both_paths(wired):
    """An id nothing holds: neither path even accepted the command."""
    tools, _registry, _manager = wired

    push = _search(tools, UNKNOWN_SESSION_ID)["visualization_push"]

    assert push["delivered"] is False
    assert push["hub_published"] is False
    assert push["registry_enqueued"] is False
    assert push["connected_clients"] == 0
    assert push["warning"]


def test_undelivered_is_reported_for_every_pushing_read_tool(wired):
    """All three tools taking the parameter report it, not just ``search_graph``."""
    tools, _registry, _manager = wired
    session_id = _new_session(tools)

    tools["save_view"](name="A view")
    calls = {
        "search_graph": lambda: tools["search_graph"](
            query="Alpha", visualization_session_id=session_id
        ),
        "get_related_nodes": lambda: tools["get_related_nodes"](
            node_id="alpha", visualization_session_id=session_id
        ),
        "get_saved_view": lambda: tools["get_saved_view"](
            name="A view", visualization_session_id=session_id
        ),
    }
    for tool_name, call in calls.items():
        push = call().get("visualization_push")
        assert push is not None, f"{tool_name} reported no delivery status"
        assert push["delivered"] is False, tool_name
        assert push["warning"], tool_name


# ---------------------------------------------------------------------------
# Delivered — legacy push registry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_registry_consumer_reports_delivered_and_receives_the_command(wired):
    """A browser holding the legacy channel: reported delivered, and it arrives.

    Async because ``push_command_sync`` enqueues via the running loop — the same
    path FastMCP takes when it calls a sync tool from the event-loop thread.
    """
    tools, registry, _manager = wired
    session_id = _new_session(tools)
    queue = registry.get_or_create(session_id)

    push = _search(tools, session_id)["visualization_push"]

    assert push["registry_enqueued"] is True
    assert push["delivered"] is True
    # call_soon defers the put by one loop iteration.
    await asyncio.sleep(0)
    commands = _drain(queue)
    assert [c["tool"] for c in commands] == ["search_graph"]
    assert commands[0]["result"]["nodes"]


@pytest.mark.asyncio
async def test_delivery_report_is_not_fed_back_into_the_canvas_payload(wired):
    """The consumer receives the search result, never the report about it."""
    tools, registry, _manager = wired
    session_id = _new_session(tools)
    queue = registry.get_or_create(session_id)

    _search(tools, session_id)

    await asyncio.sleep(0)
    (command,) = _drain(queue)
    assert "visualization_push" not in command["result"]


@pytest.mark.asyncio
async def test_registry_delivery_alone_is_enough_without_any_hub_session(tmp_path):
    """A legacy-only browser on an id the hub never stored is still delivered to."""
    tools, registry, _manager = _wire(tmp_path)
    queue = registry.get_or_create(UNKNOWN_SESSION_ID)

    push = _search(tools, UNKNOWN_SESSION_ID)["visualization_push"]

    assert push["hub_published"] is False
    assert push["registry_enqueued"] is True
    assert push["delivered"] is True
    await asyncio.sleep(0)
    assert len(_drain(queue)) == 1


# ---------------------------------------------------------------------------
# Delivered — shared-session hub
# ---------------------------------------------------------------------------


def test_connected_hub_client_reports_delivered_and_receives_the_command(wired):
    """A client on the op stream: reported delivered, and the event reaches it."""
    tools, _registry, manager = wired
    session_id = _new_session(tools)
    subscription, _member = manager.connect(session_id, "client-1", "Tester")
    try:
        push = _search(tools, session_id)["visualization_push"]

        assert push["hub_published"] is True
        assert push["connected_clients"] == 1
        assert push["registry_enqueued"] is False
        assert push["delivered"] is True

        commands = [e for e in _drain(subscription.queue) if e["type"] == "command"]
        assert [c["command"]["tool"] for c in commands] == ["search_graph"]
        assert "visualization_push" not in commands[0]["command"]["result"]
    finally:
        manager.disconnect(session_id, "client-1", subscription)


def test_hub_delivery_stops_being_reported_when_the_client_leaves(wired):
    """The report tracks the live consumer, not a past one.

    Guards the report against being computed once and cached: the same session,
    the same tool call, differs only in whether a client is still connected.
    """
    tools, _registry, manager = wired
    session_id = _new_session(tools)
    subscription, _member = manager.connect(session_id, "client-1", "Tester")
    assert _search(tools, session_id)["visualization_push"]["delivered"] is True

    manager.disconnect(session_id, "client-1", subscription)

    push = _search(tools, session_id)["visualization_push"]
    assert push["connected_clients"] == 0
    assert push["delivered"] is False


# ---------------------------------------------------------------------------
# Shape: additive only
# ---------------------------------------------------------------------------


def test_no_session_id_leaves_the_result_shape_untouched(wired):
    """No push attempted, so no report — the field is not a new constant key."""
    tools, _registry, _manager = wired

    result = tools["search_graph"](query="Alpha")

    assert "visualization_push" not in result


def test_the_report_is_the_only_difference_a_session_id_makes(wired):
    """Contract §9 additive change: one new key, every existing key untouched."""
    tools, _registry, _manager = wired
    session_id = _new_session(tools)

    without = tools["search_graph"](query="Alpha")
    with_push = _search(tools, session_id)

    assert set(with_push) - set(without) == {"visualization_push"}
    assert set(without) - set(with_push) == set()
    for key, value in without.items():
        assert with_push[key] == value, key


def test_clear_visualization_result_shape_is_unchanged(wired):
    """It gates on the registry instead of reporting, and keeps doing so."""
    tools, registry, _manager = wired
    session_id = _new_session(tools)

    refused = tools["clear_visualization"](visualization_session_id=session_id)
    assert refused["success"] is False
    assert "visualization_push" not in refused

    registry.get_or_create(session_id)
    cleared = tools["clear_visualization"](visualization_session_id=session_id)
    assert cleared["success"] is True
    assert "visualization_push" not in cleared


# ---------------------------------------------------------------------------
# Failure containment
# ---------------------------------------------------------------------------


class _BrokenHub(SessionManager):
    """A hub whose push and presence calls raise, as a swapped-in bus can."""

    def push_command(self, session_id, command):
        raise RuntimeError("hub unreachable")

    def connected_count(self, session_id):
        raise RuntimeError("presence unreachable")


@pytest.mark.asyncio
async def test_a_broken_hub_is_reported_not_raised(tmp_path):
    """A hub failure must not break the tool, nor be reported as delivery."""
    broken = _BrokenHub(SessionStore(InMemorySessionPersistenceBackend()))
    tools, registry, _manager = _wire(tmp_path, session_manager=broken)
    queue = registry.get_or_create(UNKNOWN_SESSION_ID)

    push = _search(tools, UNKNOWN_SESSION_ID)["visualization_push"]

    assert push["hub_published"] is False
    assert push["connected_clients"] == 0
    # The legacy path is independent and still delivers.
    assert push["registry_enqueued"] is True
    assert push["delivered"] is True
    await asyncio.sleep(0)
    assert len(_drain(queue)) == 1


def test_no_registry_configured_reports_the_hub_verdict_only(tmp_path):
    """A deployment with no legacy registry still gets a report, not a crash."""
    tools, registry, _manager = _wire(tmp_path, with_registry=False)
    assert registry is None
    session_id = _new_session(tools)

    push = _search(tools, session_id)["visualization_push"]

    assert push["registry_enqueued"] is False
    assert push["hub_published"] is True
    assert push["delivered"] is False
