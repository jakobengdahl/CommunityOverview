"""A push carrying ``visualization_session_id`` must report whether it landed.

The motivating scenario: an unattended routine refreshes a canvas every night by
calling ``search_graph`` with a ``visualization_session_id``. Nobody has that
session open, so the command reaches no consumer — and because a push writes no
session state (only ``add_nodes_to_session`` writes ``node_refs``), reading the
session back afterwards shows nothing either. The routine reported a refreshed
view every night while publishing nothing.

So the invariant under test is not "a push succeeds" but "the result says which
of the two delivery paths took it". Getting that wrong in the other direction is
worse than the silence it replaces, and the legacy path makes it easy to: a
registry *entry* is not a consumer. An entry is created by ``get_or_create``
(also by ``mint_trigger_token`` and the session auto-add tools, with no browser
anywhere), nothing removes it when the SSE connection closes, and every push
refreshes its TTL — so it outlives its browser indefinitely. The tests here
therefore drive ``SessionRegistry.stream()`` for real rather than standing a bare
queue in for a connected browser, and the nightly scenario above is pinned end to
end: consumer attached, consumer gone, and the push after that.

Both paths run against the real ``SessionRegistry`` / ``SessionManager``, and
each delivered case drains the consumer it claims to have reached.

``clear_visualization`` also pushes, but it already refuses unless the session has
a registry entry, so it is self-reporting already and its result shape is pinned
here as unchanged.
"""

import asyncio
import os
from contextlib import asynccontextmanager
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

DELIVERED_KEYS = {"requested", "delivered", "status", "live_consumers"}
UNDELIVERED_KEYS = DELIVERED_KEYS | {"warning"}


def _wire(tmp_path, *, with_registry=True, with_manager=True, session_manager=None):
    storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
    service = GraphService(storage)
    registry = SessionRegistry() if with_registry else None
    if session_manager is not None:
        manager = session_manager
    elif with_manager:
        manager = SessionManager(SessionStore(InMemorySessionPersistenceBackend()))
    else:
        manager = None
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


def _search(tools, session_id):
    return tools["search_graph"](query="Alpha", visualization_session_id=session_id)


def _delivery(tools, session_id):
    return _search(tools, session_id)["visualization_delivery"]


async def _settle(predicate, turns=500):
    """Yield to the loop until *predicate* holds. No wall-clock sleeping."""
    for _ in range(turns):
        if predicate():
            return True
        await asyncio.sleep(0)
    return predicate()


@asynccontextmanager
async def _legacy_consumer(registry, session_id):
    """Drive ``registry.stream()`` the way the SSE route does.

    This is what "a browser is holding the legacy push channel" means. A bare
    ``get_or_create`` entry is deliberately NOT used for it: that entry is
    exactly the state that outlives the browser, so standing it in for a
    connected browser would make the delivery rule untestable.

    Yields the list of non-ping commands the consumer receives.
    """
    received = []
    gen = registry.stream(session_id)

    async def drain():
        async for command in gen:
            if command.get("type") != "ping":
                received.append(command)

    task = asyncio.create_task(drain())
    assert await _settle(lambda: registry.has_consumer(session_id)), (
        "the stream consumer never registered"
    )
    try:
        yield received
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await gen.aclose()


def _drain_hub(subscription):
    out = []
    while True:
        try:
            out.append(subscription.queue.get_nowait())
        except asyncio.QueueEmpty:
            return out


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

    delivery = result["visualization_delivery"]
    assert delivery["requested"] is True
    assert delivery["delivered"] is False
    assert delivery["status"] == "not_delivered"
    assert delivery["live_consumers"] == 0
    assert delivery["warning"]
    assert "add_nodes_to_session" in delivery["warning"]
    assert set(delivery) == UNDELIVERED_KEYS
    # The warning must name THIS state and not a neighbouring one: there is no
    # registry entry, and the hub was reachable and simply had no listener.
    # Matched on the distinctive reason clauses, since the remedy sentence
    # mentions a registry entry too.
    assert "no connected client" in delivery["warning"]
    assert "has a registry entry but nothing" not in delivery["warning"]
    assert "no stored state" not in delivery["warning"]
    assert "could not be queried" not in delivery["warning"]
    # And it must not send the caller to a check with the same false positive.
    assert "Trust this report" in delivery["warning"]
    # The push left no trace to read back — the half of the defect that makes
    # the report the only way to notice.
    assert manager.get_session(session_id).state.get("node_refs", []) == []
    # The search itself still succeeded; only the delivery is reported as failed.
    assert result["nodes"]


def test_unknown_session_reports_undelivered_on_both_paths(wired):
    """An id nothing holds: neither path even accepted the command."""
    tools, _registry, _manager = wired

    delivery = _delivery(tools, UNKNOWN_SESSION_ID)

    assert delivery["delivered"] is False
    assert delivery["status"] == "not_delivered"
    assert delivery["live_consumers"] == 0
    assert "no browser is holding" in delivery["warning"]
    assert "no connected client" in delivery["warning"]
    # Nothing holds this id, so neither the entry clause nor the presence clause
    # may appear — the branch table's two "quiet" outcomes are the ones a
    # truthiness-only assertion would let drift. Matched on the distinctive
    # clause text, since the remedy sentence mentions a registry entry too.
    assert "has a registry entry but nothing" not in delivery["warning"]
    assert "a client is connected to its op stream" not in delivery["warning"]


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
        delivery = call().get("visualization_delivery")
        assert delivery is not None, f"{tool_name} reported no delivery status"
        assert delivery["delivered"] is False, tool_name
        assert delivery["live_consumers"] == 0, tool_name
        assert delivery["warning"], tool_name


# ---------------------------------------------------------------------------
# A registry entry is not a consumer — the false-positive the report must not make
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_nightly_push_stops_being_delivered_when_the_tab_closes(wired):
    """The whole defect, end to end, on the path most likely to misreport it.

    A browser opens the session, so the push is genuinely delivered. The tab then
    closes. The registry entry survives that — nothing removes it, and every push
    refreshes its TTL — so "an entry exists" would report delivered forever. Two
    successive pushes after the close pin that it does not, including that the
    verdict does not decay back to True once the TTL would have lapsed.
    """
    tools, registry, manager = wired
    session_id = _new_session(tools)

    async with _legacy_consumer(registry, session_id):
        delivery = _delivery(tools, session_id)
        assert delivery["delivered"] is True
        assert delivery["live_consumers"] == 1

    # The tab is gone. The entry is not.
    assert registry.session_exists(session_id) is True
    assert registry.has_consumer(session_id) is False
    assert manager.connected_count(session_id) == 0

    for night in (1, 2):
        delivery = _delivery(tools, session_id)
        assert delivery["delivered"] is False, night
        assert delivery["status"] == "not_delivered", night
        # The stale entry must not be counted as a consumer.
        assert delivery["live_consumers"] == 0, night
        assert delivery["warning"], night
        # The warning must name the state that actually applies, not "no browser
        # is holding the channel" — there IS an entry, with nothing draining it.
        assert "nothing" in delivery["warning"], night
        assert "draining" in delivery["warning"], night


@pytest.mark.asyncio
async def test_a_bare_registry_entry_alone_is_never_delivery(wired):
    """An entry with no consumer — how ``mint_trigger_token`` and auto-add leave one.

    Async so the enqueue actually happens: ``push_command_sync`` needs a running
    loop, which is what FastMCP gives a sync tool in production.
    """
    tools, registry, _manager = wired
    session_id = _new_session(tools)

    registry.get_or_create(session_id)

    delivery = _delivery(tools, session_id)
    assert delivery["delivered"] is False
    assert delivery["live_consumers"] == 0


@pytest.mark.asyncio
async def test_a_session_auto_add_agent_does_not_make_a_push_delivered(tmp_path):
    """Configuring an auto-add agent materialises a registry entry, not a canvas."""
    from backend.core.session_auto_add import SessionAutoAddRegistry

    storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
    service = GraphService(storage)
    registry = SessionRegistry()
    manager = SessionManager(SessionStore(InMemorySessionPersistenceBackend()))
    mock_mcp = Mock()
    mock_mcp.tool = MagicMock(return_value=lambda f: f)
    tools = register_mcp_tools(
        mock_mcp,
        service,
        session_registry=registry,
        session_manager=manager,
        auto_add_registry=SessionAutoAddRegistry(),
    )
    tools["add_nodes"](
        nodes=[{"id": "alpha", "type": "Actor", "name": "Alpha"}], edges=[]
    )
    session_id = _new_session(tools)

    created = tools["create_session_auto_add_agent"](
        visualization_session_id=session_id, node_types=["Actor"]
    )
    assert created["success"] is True
    assert registry.session_exists(session_id) is True

    delivery = _delivery(tools, session_id)
    assert delivery["delivered"] is False
    assert delivery["live_consumers"] == 0


# ---------------------------------------------------------------------------
# Delivered — legacy push channel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_legacy_consumer_reports_delivered_and_receives_the_command(wired):
    """A browser draining the legacy channel: reported delivered, and it arrives."""
    tools, registry, _manager = wired
    session_id = _new_session(tools)

    async with _legacy_consumer(registry, session_id) as received:
        delivery = _delivery(tools, session_id)

        assert delivery["delivered"] is True
        assert delivery["status"] == "delivered"
        assert delivery["live_consumers"] == 1
        assert "warning" not in delivery
        assert set(delivery) == DELIVERED_KEYS

        assert await _settle(lambda: len(received) == 1)
        assert received[0]["tool"] == "search_graph"
        assert received[0]["result"]["nodes"]

        # A second push to the SAME still-attached consumer. Pushing once per
        # consumer would not notice a ref-count that delivery consumes, which
        # would turn the verdict into the mirror-image false negative.
        second = _delivery(tools, session_id)
        assert second["delivered"] is True
        assert second["live_consumers"] == 1
        assert await _settle(lambda: len(received) == 2)


@pytest.mark.asyncio
async def test_delivery_report_is_not_fed_back_into_the_canvas_payload(wired):
    """The consumer receives the search result, never the report about it."""
    tools, registry, _manager = wired
    session_id = _new_session(tools)

    async with _legacy_consumer(registry, session_id) as received:
        _search(tools, session_id)

        assert await _settle(lambda: len(received) == 1)
        assert "visualization_delivery" not in received[0]["result"]


@pytest.mark.asyncio
async def test_legacy_delivery_alone_is_enough_without_any_hub_session(tmp_path):
    """A legacy-only browser on an id the hub never stored is still delivered to."""
    tools, registry, _manager = _wire(tmp_path)

    async with _legacy_consumer(registry, UNKNOWN_SESSION_ID) as received:
        delivery = _delivery(tools, UNKNOWN_SESSION_ID)

        assert delivery["delivered"] is True
        assert delivery["live_consumers"] == 1
        assert "warning" not in delivery
        assert await _settle(lambda: len(received) == 1)


# ---------------------------------------------------------------------------
# Delivered — shared-session hub
# ---------------------------------------------------------------------------


def test_connected_hub_client_reports_delivered_and_receives_the_command(wired):
    """A client on the op stream: reported delivered, and the event reaches it."""
    tools, _registry, manager = wired
    session_id = _new_session(tools)
    subscription, _member = manager.connect(session_id, "client-1", "Tester")
    try:
        delivery = _delivery(tools, session_id)

        assert delivery["delivered"] is True
        assert delivery["status"] == "delivered"
        assert delivery["live_consumers"] == 1
        assert "warning" not in delivery

        commands = [e for e in _drain_hub(subscription) if e["type"] == "command"]
        assert [c["command"]["tool"] for c in commands] == ["search_graph"]
        assert "visualization_delivery" not in commands[0]["command"]["result"]
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
    assert _delivery(tools, session_id)["delivered"] is True

    manager.disconnect(session_id, "client-1", subscription)

    delivery = _delivery(tools, session_id)
    assert delivery["live_consumers"] == 0
    assert delivery["delivered"] is False


def test_presence_without_stored_state_is_not_delivery(wired):
    """A client can be on a session's op stream that the store does not hold.

    ``SessionManager.connect`` registers presence and subscribes without
    materialising the session, while ``push_command`` publishes only for a
    session the store holds. So the hub publishes nothing while a client is
    connected: the client is real and counted, but it received nothing, so the
    push is undelivered and the warning must say which state this is rather than
    claiming nobody is connected.
    """
    tools, _registry, manager = wired

    subscription, _member = manager.connect(UNKNOWN_SESSION_ID, "client-1", "Tester")
    try:
        delivery = _delivery(tools, UNKNOWN_SESSION_ID)

        assert delivery["live_consumers"] == 1
        assert delivery["delivered"] is False
        assert "no stored state" in delivery["warning"]
        # This state is "hub reachable, nothing to publish to", which must not be
        # reported as the hub having failed, nor as nobody being connected.
        assert "could not be queried" not in delivery["warning"]
        assert "no connected client" not in delivery["warning"]
        # Nothing but the join echo reached the subscriber.
        assert [e["type"] for e in _drain_hub(subscription)] == ["presence_joined"]
    finally:
        manager.disconnect(UNKNOWN_SESSION_ID, "client-1", subscription)


def test_two_connections_from_one_client_count_as_one(wired):
    """``live_consumers`` counts clients, not connections, on the hub side.

    Pins the count against the same source ``connect_to_visualization_session``
    reports, so the two tools cannot drift apart on a fast reconnect.
    """
    tools, _registry, manager = wired
    session_id = _new_session(tools)
    first, _ = manager.connect(session_id, "client-1", "Tester")
    second, _ = manager.connect(session_id, "client-1", "Tester")
    try:
        assert _delivery(tools, session_id)["live_consumers"] == 1
        third, _ = manager.connect(session_id, "client-2", "Other")
        try:
            assert _delivery(tools, session_id)["live_consumers"] == 2
        finally:
            manager.disconnect(session_id, "client-2", third)
    finally:
        manager.disconnect(session_id, "client-1", second)
        manager.disconnect(session_id, "client-1", first)


@pytest.mark.asyncio
async def test_both_paths_attached_are_counted_together(wired):
    """A browser on the legacy channel and a client on the op stream are two."""
    tools, registry, manager = wired
    session_id = _new_session(tools)

    async with _legacy_consumer(registry, session_id):
        subscription, _member = manager.connect(session_id, "client-1", "Tester")
        try:
            delivery = _delivery(tools, session_id)
            assert delivery["delivered"] is True
            assert delivery["live_consumers"] == 2
        finally:
            manager.disconnect(session_id, "client-1", subscription)


# ---------------------------------------------------------------------------
# Shape
# ---------------------------------------------------------------------------


def test_no_session_id_leaves_the_result_shape_untouched(wired):
    """No push attempted, so no report — the field is not a new constant key."""
    tools, _registry, _manager = wired

    result = tools["search_graph"](query="Alpha")

    assert "visualization_delivery" not in result


def test_the_report_is_the_only_difference_a_session_id_makes(wired):
    """One added key; every existing key of the payload untouched."""
    tools, _registry, _manager = wired
    session_id = _new_session(tools)

    without = tools["search_graph"](query="Alpha")
    with_push = _search(tools, session_id)

    assert set(with_push) - set(without) == {"visualization_delivery"}
    assert set(without) - set(with_push) == set()
    for key, value in without.items():
        assert with_push[key] == value, key


def test_clear_visualization_result_shape_is_unchanged(wired):
    """It gates on the registry instead of reporting, and keeps doing so."""
    tools, registry, _manager = wired
    session_id = _new_session(tools)

    refused = tools["clear_visualization"](visualization_session_id=session_id)
    assert refused["success"] is False
    assert "visualization_delivery" not in refused

    registry.get_or_create(session_id)
    cleared = tools["clear_visualization"](visualization_session_id=session_id)
    assert cleared["success"] is True
    assert "visualization_delivery" not in cleared


# ---------------------------------------------------------------------------
# Failure containment and partial wiring
# ---------------------------------------------------------------------------


class _BrokenHub(SessionManager):
    """A hub whose publish raises, as a swapped-in bus can."""

    def push_command(self, session_id, command):
        raise RuntimeError("hub unreachable")


class _FullyBrokenHub(_BrokenHub):
    """Presence unreadable as well, so neither hub fact can be established."""

    def connected_count(self, session_id):
        raise RuntimeError("presence unreachable")


@pytest.mark.asyncio
async def test_a_broken_hub_is_reported_not_raised(tmp_path):
    """A hub failure must not break the tool, nor be reported as delivery."""
    broken = _FullyBrokenHub(SessionStore(InMemorySessionPersistenceBackend()))
    tools, registry, _manager = _wire(tmp_path, session_manager=broken)

    async with _legacy_consumer(registry, UNKNOWN_SESSION_ID) as received:
        delivery = _delivery(tools, UNKNOWN_SESSION_ID)

        # The legacy path is independent and still delivers.
        assert delivery["delivered"] is True
        assert delivery["live_consumers"] == 1
        assert await _settle(lambda: len(received) == 1)


def test_a_broken_hub_with_live_presence_is_not_reported_delivered(tmp_path):
    """Bus down, presence up: a connected client did not receive anything."""
    broken = _BrokenHub(SessionStore(InMemorySessionPersistenceBackend()))
    tools, _registry, manager = _wire(tmp_path, session_manager=broken)

    subscription, _member = manager.connect(UNKNOWN_SESSION_ID, "client-1", "Tester")
    try:
        delivery = _delivery(tools, UNKNOWN_SESSION_ID)

        assert delivery["live_consumers"] == 1
        assert delivery["delivered"] is False
        assert "could not be queried" in delivery["warning"]
    finally:
        manager.disconnect(UNKNOWN_SESSION_ID, "client-1", subscription)


def test_a_failed_hub_call_is_not_reported_as_an_empty_session(tmp_path):
    """A hub that raised must not be described as a session with no state.

    The publish failure leaves ``hub_published`` false and, in the fully broken
    case, ``hub_clients`` zero — the same two booleans a quiet empty session
    produces. Saying "the session has no stored state" or "no client is
    connected" there would assert something the code never established, which is
    the class of false statement this warning exists to avoid.
    """
    broken = _BrokenHub(SessionStore(InMemorySessionPersistenceBackend()))
    tools, _registry, manager = _wire(tmp_path, session_manager=broken)
    session_id = _new_session(tools)
    assert manager.get_session(session_id) is not None, "the session IS stored"

    subscription, _member = manager.connect(session_id, "client-1", "Tester")
    try:
        warning = _delivery(tools, session_id)["warning"]
        assert "could not be queried" in warning
        assert "no stored state" not in warning
        assert "no connected client" not in warning
    finally:
        manager.disconnect(session_id, "client-1", subscription)

    # Presence unreadable as well: still the hub-failure reason, never "nobody
    # is connected" — a client is in fact attached.
    fully = _FullyBrokenHub(SessionStore(InMemorySessionPersistenceBackend()))
    tools, _registry, manager = _wire(tmp_path, session_manager=fully)
    subscription, _member = manager.connect(UNKNOWN_SESSION_ID, "client-1", "Tester")
    try:
        warning = _delivery(tools, UNKNOWN_SESSION_ID)["warning"]
        assert "could not be queried" in warning
        assert "no connected client" not in warning
    finally:
        manager.disconnect(UNKNOWN_SESSION_ID, "client-1", subscription)


def test_an_attached_consumer_whose_enqueue_failed_is_not_delivered(tmp_path):
    """Both halves of the legacy path are required, not just the consumer.

    ``push_command_sync`` returns False when it is called off the event-loop
    thread with no injected loop, so a consumer can be attached while the command
    was never enqueued. Claiming delivery there would report a command that was
    dropped.
    """

    class _EnqueueFailsRegistry:
        def is_valid_session_id(self, session_id):
            return True

        def session_exists(self, session_id):
            return True

        def push_command_sync(self, session_id, command):
            return False

        def consumer_count(self, session_id):
            return 1

    storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
    service = GraphService(storage)
    mock_mcp = Mock()
    mock_mcp.tool = MagicMock(return_value=lambda f: f)
    tools = register_mcp_tools(
        mock_mcp, service, session_registry=_EnqueueFailsRegistry()
    )
    tools["add_nodes"](
        nodes=[{"id": "alpha", "type": "Actor", "name": "Alpha"}], edges=[]
    )

    delivery = _delivery(tools, UNKNOWN_SESSION_ID)

    assert delivery["delivered"] is False
    assert delivery["live_consumers"] == 1


def test_no_registry_configured_reports_the_hub_verdict_only(tmp_path):
    """A deployment with no legacy registry still gets a report, not a crash."""
    tools, registry, _manager = _wire(tmp_path, with_registry=False)
    assert registry is None
    session_id = _new_session(tools)

    delivery = _delivery(tools, session_id)

    assert delivery["delivered"] is False
    assert delivery["live_consumers"] == 0
    # An absent channel gets its own clause rather than being described as a
    # browser that is not holding one.
    assert "no legacy push channel is configured" in delivery["warning"]


@pytest.mark.asyncio
async def test_no_manager_configured_reports_the_legacy_verdict_only(tmp_path):
    """The mirror case: no hub, so the legacy path alone decides."""
    tools, registry, manager = _wire(tmp_path, with_manager=False)
    assert manager is None

    undelivered = _delivery(tools, UNKNOWN_SESSION_ID)
    assert undelivered["delivered"] is False
    assert undelivered["live_consumers"] == 0
    assert "no shared-session hub is configured" in undelivered["warning"]
    assert "no connected client" not in undelivered["warning"]

    async with _legacy_consumer(registry, UNKNOWN_SESSION_ID):
        delivery = _delivery(tools, UNKNOWN_SESSION_ID)
        assert delivery["delivered"] is True
        assert delivery["live_consumers"] == 1
        assert "warning" not in delivery


def test_a_registry_that_cannot_report_consumers_makes_no_delivery_claim(tmp_path):
    """An older or foreign registry without the consumer count is not assumed live."""

    class _ConsumerBlindRegistry:
        def __init__(self):
            self.commands = []

        def is_valid_session_id(self, session_id):
            return True

        def session_exists(self, session_id):
            return True

        def push_command_sync(self, session_id, command):
            self.commands.append(command)
            return True

    storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
    service = GraphService(storage)
    blind = _ConsumerBlindRegistry()
    mock_mcp = Mock()
    mock_mcp.tool = MagicMock(return_value=lambda f: f)
    tools = register_mcp_tools(mock_mcp, service, session_registry=blind)
    tools["add_nodes"](
        nodes=[{"id": "alpha", "type": "Actor", "name": "Alpha"}], edges=[]
    )

    delivery = _delivery(tools, UNKNOWN_SESSION_ID)

    assert delivery["delivered"] is False
    assert delivery["live_consumers"] == 0
    # The command was still handed over — only the claim about it is withheld.
    assert len(blind.commands) == 1
