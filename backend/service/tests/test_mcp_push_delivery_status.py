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

``clear_visualization`` also pushes, but it already refuses unless a live client
is reachable, so it is self-reporting already and its result shape is pinned
here as unchanged.
"""

import asyncio
import itertools
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
from backend.service.mcp_tools import _undelivered_push_warning

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
    # And it must name this report, not a pre-push check, as the verdict.
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


@pytest.mark.asyncio
async def test_clear_visualization_result_shape_is_unchanged(wired):
    """It gates before pushing instead of reporting, and keeps doing so."""
    tools, registry, _manager = wired
    session_id = _new_session(tools)

    refused = tools["clear_visualization"](visualization_session_id=session_id)
    assert refused["success"] is False
    assert "visualization_delivery" not in refused

    async with _legacy_consumer(registry, session_id):
        cleared = tools["clear_visualization"](visualization_session_id=session_id)
    assert cleared["success"] is True
    assert "visualization_delivery" not in cleared


@pytest.mark.asyncio
async def test_clear_visualization_sends_the_clear_to_the_attached_canvas(wired):
    """A reported clear must reach the canvas, on either channel it holds.

    The tool returns success from its gate alone, so a clear that was never
    pushed would still say the canvas was cleared.
    """
    tools, registry, manager = wired
    session_id = _new_session(tools)
    subscription, _member = manager.connect(session_id, "client-1", "Tester")
    try:
        async with _legacy_consumer(registry, session_id) as received:
            cleared = tools["clear_visualization"](visualization_session_id=session_id)
            assert cleared["success"] is True
            assert await _settle(lambda: len(received) == 1)

        assert received[0]["tool"] == "clear_visualization"
        assert received[0]["result"]["action"] == "clear_visualization"
        commands = [e for e in _drain_hub(subscription) if e["type"] == "command"]
        assert [c["command"]["result"]["action"] for c in commands] == [
            "clear_visualization"
        ]
    finally:
        manager.disconnect(session_id, "client-1", subscription)


@pytest.mark.asyncio
async def test_one_browser_during_page_load_handover_counts_as_two(wired):
    """One browser holds both channels while the op stream comes up.

    The frontend keeps the legacy ``EventSource`` open until the op stream is
    ready, so for that window a single tab is two consumer connections and
    ``live_consumers`` sums them. Both receive the same push under one
    ``command_id``, which is what lets the browser apply it once. After the
    legacy channel is released the op stream alone still delivers.
    """
    tools, registry, manager = wired
    session_id = _new_session(tools)

    subscription = None
    try:
        async with _legacy_consumer(registry, session_id) as legacy_received:
            before = _delivery(tools, session_id)
            assert before["live_consumers"] == 1
            assert await _settle(lambda: len(legacy_received) == 1)

            subscription, _member = manager.connect(session_id, "browser-1", "Tester")
            during = _delivery(tools, session_id)
            assert during["delivered"] is True
            assert during["live_consumers"] == 2
            assert await _settle(lambda: len(legacy_received) == 2)
            hub_commands = [
                e for e in _drain_hub(subscription) if e["type"] == "command"
            ]
            assert [c["command"]["command_id"] for c in hub_commands] == [
                legacy_received[1]["command_id"]
            ]

        assert registry.has_consumer(session_id) is False
        after = _delivery(tools, session_id)
        assert after["delivered"] is True
        assert after["live_consumers"] == 1
    finally:
        if subscription is not None:
            manager.disconnect(session_id, "browser-1", subscription)


# ---------------------------------------------------------------------------
# The pre-push reachability check agrees with the delivery report
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_stops_claiming_a_reachable_canvas_when_the_tab_closes(wired):
    """The entry left behind by a closed tab is not a legacy push channel.

    Before the fix ``connect_to_visualization_session`` said a browser was
    holding the channel open, and that pushes reach it, in the very state the
    delivery report calls undelivered. The two must now agree.
    """
    tools, registry, _manager = wired
    session_id = _new_session(tools)

    async with _legacy_consumer(registry, session_id):
        live = tools["connect_to_visualization_session"](session_id=session_id)
        assert live["connected"] is True
        assert "draining its legacy push channel" in live["message"]
        assert _delivery(tools, session_id)["delivered"] is True

    assert registry.session_exists(session_id) is True
    assert registry.has_consumer(session_id) is False

    stale = tools["connect_to_visualization_session"](session_id=session_id)
    assert stale["connected"] is True
    assert stale["has_stored_state"] is True
    assert stale["connected_clients"] == 0
    assert "legacy push channel" not in stale["message"]
    assert "with no client connected" in stale["message"]
    assert "reaches nobody" in stale["message"]
    assert _delivery(tools, session_id)["delivered"] is False


@pytest.mark.asyncio
async def test_clear_visualization_refuses_a_session_whose_tab_closed(wired):
    """``clear_visualization`` gates on a consumer, not on the leftover entry."""
    tools, registry, _manager = wired
    session_id = _new_session(tools)

    async with _legacy_consumer(registry, session_id):
        cleared = tools["clear_visualization"](visualization_session_id=session_id)
        assert cleared["success"] is True

    assert registry.session_exists(session_id) is True
    refused = tools["clear_visualization"](visualization_session_id=session_id)
    assert refused["success"] is False
    assert "exists, but no client" in refused["error"]
    assert "draining its legacy push channel" in refused["error"]


@pytest.mark.parametrize(
    "leave_entry",
    [
        pytest.param(lambda reg, sid: reg.get_or_create(sid), id="get_or_create"),
        pytest.param(lambda reg, sid: reg.mint_trigger_token(sid), id="trigger"),
    ],
)
def test_a_bare_entry_with_no_stored_state_resolves_as_not_found(wired, leave_entry):
    """With nothing stored and nothing draining, there is no session to report.

    An entry left by ``mint_trigger_token`` (or any other ``get_or_create``) is
    not a client, so the tools must not describe the id as "open in a client".
    """
    tools, registry, _manager = wired
    leave_entry(registry, UNKNOWN_SESSION_ID)
    assert registry.session_exists(UNKNOWN_SESSION_ID) is True
    assert registry.has_consumer(UNKNOWN_SESSION_ID) is False

    connect = tools["connect_to_visualization_session"](session_id=UNKNOWN_SESSION_ID)
    assert connect["connected"] is False
    assert "not found" in connect["message"]

    state = tools["get_visualization_session_state"](session_id=UNKNOWN_SESSION_ID)
    assert "not found" in state["error"]

    refused = tools["clear_visualization"](visualization_session_id=UNKNOWN_SESSION_ID)
    assert refused["success"] is False
    assert "not found" in refused["error"]


@pytest.mark.asyncio
async def test_a_live_consumer_without_stored_state_is_still_found(wired):
    """The legacy-only case the gate exists for keeps resolving."""
    tools, registry, _manager = wired

    async with _legacy_consumer(registry, UNKNOWN_SESSION_ID):
        connect = tools["connect_to_visualization_session"](
            session_id=UNKNOWN_SESSION_ID
        )
        cleared = tools["clear_visualization"](
            visualization_session_id=UNKNOWN_SESSION_ID
        )

    assert connect["connected"] is True
    assert connect["has_stored_state"] is False
    assert "no stored state yet" in connect["message"]
    assert cleared["success"] is True


def test_a_registry_that_cannot_report_consumers_is_not_a_push_target(tmp_path):
    """An older or foreign registry without ``has_consumer`` gates as no consumer."""

    class _ConsumerBlindRegistry:
        def is_valid_session_id(self, session_id):
            return True

        def session_exists(self, session_id):
            return True

        def push_command_sync(self, session_id, command):
            return True

    storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
    service = GraphService(storage)
    mock_mcp = Mock()
    mock_mcp.tool = MagicMock(return_value=lambda f: f)
    tools = register_mcp_tools(
        mock_mcp, service, session_registry=_ConsumerBlindRegistry()
    )

    refused = tools["clear_visualization"](visualization_session_id=UNKNOWN_SESSION_ID)
    assert refused["success"] is False
    connect = tools["connect_to_visualization_session"](session_id=UNKNOWN_SESSION_ID)
    assert connect["connected"] is False


def test_clear_visualization_accepts_op_stream_presence_without_registry(tmp_path):
    tools, _registry, manager = _wire(tmp_path, with_registry=False)
    session_id = _new_session(tools)
    manager.presence.join(session_id, "client-1", "Tester")

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


class _PresenceBlindHub(SessionManager):
    """The inverse partial failure: presence unreadable, the bus working.

    The two hub calls reach different collaborators — ``connected_count`` the
    presence registry, ``push_command`` the store and the bus — so this is a real
    state, and the one in which a published command has no readable audience.
    """

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
        assert delivery["status"] == "not_delivered"
        assert "the publish to the shared-session hub failed" in delivery["warning"]
    finally:
        manager.disconnect(UNKNOWN_SESSION_ID, "client-1", subscription)


def test_a_failed_publish_is_not_reported_as_an_empty_session(tmp_path):
    """A publish that raised must not be described as a session with no state.

    The failure leaves ``hub_published`` false — the same boolean a session the
    store does not hold produces. Saying "the session has no stored state" there
    would assert something the code never established.
    """
    broken = _BrokenHub(SessionStore(InMemorySessionPersistenceBackend()))
    tools, _registry, manager = _wire(tmp_path, session_manager=broken)
    session_id = _new_session(tools)
    assert manager.get_session(session_id) is not None, "the session IS stored"

    subscription, _member = manager.connect(session_id, "client-1", "Tester")
    try:
        delivery = _delivery(tools, session_id)
        # Nothing reached the hub, so this IS a non-delivery — knowably so.
        assert delivery["status"] == "not_delivered"
        assert delivery["warning"].startswith("Nothing received this push")
        assert "the publish to the shared-session hub failed" in delivery["warning"]
        assert "no stored state" not in delivery["warning"]
        assert "no connected client" not in delivery["warning"]
        # The presence count was readable and is reported, so the warning must not
        # claim it could not be read.
        assert delivery["live_consumers"] == 1
        assert "could not be read" not in delivery["warning"]
    finally:
        manager.disconnect(session_id, "client-1", subscription)


def test_a_publish_that_landed_is_never_reported_as_a_non_delivery(tmp_path):
    """The mirror failure: presence unreadable, publish fine.

    ``connected_count`` and ``push_command`` fail independently, so the command
    can reach the hub's subscribers while their number is unknown. Reporting that
    as ``not_delivered`` denied a push that had in fact landed — the mirror of the
    false positive this whole report exists to remove — so it is ``unknown``.
    """
    blind = _PresenceBlindHub(SessionStore(InMemorySessionPersistenceBackend()))
    tools, _registry, manager = _wire(tmp_path, session_manager=blind)
    session_id = _new_session(tools)
    subscription, _member = manager.connect(session_id, "client-1", "Tester")
    try:
        delivery = _delivery(tools, session_id)

        # The subscriber really did get it — that is what makes "not_delivered"
        # a lie rather than a conservative guess.
        events = [e["type"] for e in _drain_hub(subscription)]
        assert "command" in events

        assert delivery["status"] == "unknown"
        # delivered still claims only an ESTABLISHED delivery — that pairing is
        # why a fourth status value was needed rather than relaxing delivered.
        assert delivery["delivered"] is False
        assert delivery["warning"].startswith(
            "It is not known whether anything received this push"
        )
        assert "presence count could not be read" in delivery["warning"]
        assert "no connected client" not in delivery["warning"]
        assert "no stored state" not in delivery["warning"]
    finally:
        manager.disconnect(session_id, "client-1", subscription)


def test_an_unreadable_presence_count_never_claims_nobody_is_connected(tmp_path):
    """Presence unreadable while the publish returns falsy without raising.

    That is the ordinary "session not in the store" shape, so the publish returns
    False rather than raising — which means this state does not reach the
    publish-failed clause, and an unreadable count of 0 must not be reported as a
    real zero. Nothing was published, so it IS a non-delivery; the clause just may
    not claim the op stream is empty.
    """
    blind = _PresenceBlindHub(SessionStore(InMemorySessionPersistenceBackend()))
    tools, _registry, manager = _wire(tmp_path, session_manager=blind)

    subscription, _member = manager.connect(UNKNOWN_SESSION_ID, "client-1", "Tester")
    try:
        delivery = _delivery(tools, UNKNOWN_SESSION_ID)

        assert delivery["status"] == "not_delivered"
        assert "presence count could not be read" in delivery["warning"]
        assert "no stored state" in delivery["warning"]
        # The count was never read, so this must not be asserted.
        assert "no connected client" not in delivery["warning"]
    finally:
        manager.disconnect(UNKNOWN_SESSION_ID, "client-1", subscription)


def test_both_hub_calls_failing_is_still_a_non_delivery(tmp_path):
    """Nothing was published, so the outcome is known even with presence blind."""
    fully = _FullyBrokenHub(SessionStore(InMemorySessionPersistenceBackend()))
    tools, _registry, manager = _wire(tmp_path, session_manager=fully)
    subscription, _member = manager.connect(UNKNOWN_SESSION_ID, "client-1", "Tester")
    try:
        delivery = _delivery(tools, UNKNOWN_SESSION_ID)
        assert delivery["status"] == "not_delivered"
        assert "the publish to the shared-session hub failed" in delivery["warning"]
        assert "no connected client" not in delivery["warning"]
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


# ---------------------------------------------------------------------------
# The whole warning input space at once
# ---------------------------------------------------------------------------

# Each clause fragment paired with the condition its inputs must satisfy for the
# clause to be a statement the function actually established.
_CLAUSE_PRECONDITIONS = {
    "no connected client": lambda s: s["hub_presence_read"] and s["hub_clients"] == 0,
    "presence count could not be read": lambda s: not s["hub_presence_read"],
    "a client is connected to its op stream": (
        lambda s: s["hub_presence_read"] and s["hub_clients"] > 0
    ),
    "no stored state": lambda s: not s["hub_published"] and not s["hub_publish_failed"],
    "the publish to the shared-session hub failed": lambda s: s["hub_publish_failed"],
    "no shared-session hub is configured": lambda s: not s["hub_configured"],
    "no legacy push channel is configured": lambda s: not s["registry_configured"],
    "has a registry entry but nothing": (
        lambda s: (
            s["registry_configured"]
            and s["legacy_enqueued"]
            and s["legacy_consumers"] == 0
        )
    ),
    "no browser is holding": (
        lambda s: (
            s["registry_configured"]
            and not (s["legacy_enqueued"] and s["legacy_consumers"] == 0)
        )
    ),
}


def _reachable_undelivered_states():
    """Every input combination `_push_to_session` can hand the warning builder.

    The skipped combinations are ones the caller cannot produce: an absent hub
    leaves every hub fact at its default, a raise leaves ``hub_published`` false,
    a failed presence read forces the count to zero, and an absent registry
    leaves the legacy facts at theirs.
    """
    for (
        registry_configured,
        legacy_enqueued,
        legacy_consumers,
        hub_configured,
        hub_publish_failed,
        hub_presence_read,
        hub_published,
        hub_clients,
    ) in itertools.product(
        [True, False],
        [True, False],
        [0, 1],
        [True, False],
        [True, False],
        [True, False],
        [True, False],
        [0, 2],
    ):
        state = {
            "registry_configured": registry_configured,
            "legacy_enqueued": legacy_enqueued,
            "legacy_consumers": legacy_consumers,
            "hub_configured": hub_configured,
            "hub_publish_failed": hub_publish_failed,
            "hub_presence_read": hub_presence_read,
            "hub_published": hub_published,
            "hub_clients": hub_clients,
        }
        if not hub_configured and (
            hub_publish_failed or hub_published or hub_clients or not hub_presence_read
        ):
            continue
        if hub_publish_failed and hub_published:
            continue
        if not hub_presence_read and hub_clients:
            continue
        if not registry_configured and (legacy_enqueued or legacy_consumers):
            continue
        delivered = (legacy_enqueued and legacy_consumers > 0) or (
            hub_published and hub_clients > 0
        )
        if delivered:
            continue  # no warning is built for a delivery
        yield state


def test_no_warning_clause_ever_asserts_an_unestablished_state():
    """The invariant the per-state tests keep discovering one case at a time.

    Every blocking finding on this change after the mechanism settled was a
    clause true of a neighbouring state but not the one it was emitted for. Rather
    than pin those case by case, this enumerates the builder's whole input space
    and checks each emitted clause against the condition that would make it a
    statement the code established — so a future branch that lets an unreadable
    count read as a real zero, or describes a failed publish as an empty session,
    fails here regardless of which state it slips through.
    """
    checked = 0
    for state in _reachable_undelivered_states():
        outcome_unknown = state["hub_published"] and not state["hub_presence_read"]
        warning = _undelivered_push_warning(outcome_unknown=outcome_unknown, **state)
        reason = warning.split(". A push")[0]
        checked += 1
        for fragment, established in _CLAUSE_PRECONDITIONS.items():
            if fragment in reason:
                assert established(state), (
                    f"clause {fragment!r} is not established by {state}"
                )
        if outcome_unknown:
            assert reason.startswith("It is not known whether anything received"), state
        else:
            assert reason.startswith("Nothing received this push"), state
    # Guards the enumeration itself: a tightened precondition that silently
    # skipped every state would otherwise pass vacuously.
    assert checked == 36, f"expected 36 reachable undelivered states, got {checked}"
