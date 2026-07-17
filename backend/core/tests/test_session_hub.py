"""
Tests for the ephemeral realtime layer (step 2 + step 3 claims) in
``backend/core/session_hub.py``: broadcast fan-out, slow-consumer resync,
presence roster/colour assignment, and selection-claim TTL/release.
"""

import asyncio
import threading

from backend.core.session_hub import ClaimMap, InProcessEventBus, PresenceRegistry


class TestInProcessEventBus:
    def test_broadcast_to_all_subscribers(self):
        bus = InProcessEventBus()
        a = bus.subscribe("1234-5678")
        b = bus.subscribe("1234-5678")
        bus.publish("1234-5678", {"type": "op", "n": 1})
        assert a.queue.get_nowait() == {"type": "op", "n": 1}
        assert b.queue.get_nowait() == {"type": "op", "n": 1}

    def test_isolated_per_session(self):
        bus = InProcessEventBus()
        a = bus.subscribe("1111-1111")
        bus.subscribe("2222-2222")
        bus.publish("2222-2222", {"x": 1})
        assert a.queue.empty()

    def test_unsubscribe_stops_delivery(self):
        bus = InProcessEventBus()
        a = bus.subscribe("1234-5678")
        bus.unsubscribe(a)
        bus.publish("1234-5678", {"x": 1})
        assert a.queue.empty()
        assert bus.subscriber_count("1234-5678") == 0

    def test_slow_consumer_gets_resync(self):
        bus = InProcessEventBus(queue_max=2)
        a = bus.subscribe("1234-5678")
        for i in range(5):
            bus.publish("1234-5678", {"type": "op", "n": i})
        drained = []
        while not a.queue.empty():
            drained.append(a.queue.get_nowait())
        assert a.needs_resync is True
        assert drained[-1] == {"type": "resync"}

    def test_publish_on_loop_delivers_normally(self):
        """Fast path: publish from the event-loop thread delivers the event."""

        async def _run():
            bus = InProcessEventBus()
            sub = bus.subscribe("loop-session")
            bus.publish("loop-session", {"type": "op", "n": 42})
            event = sub.queue.get_nowait()
            assert event == {"type": "op", "n": 42}

        asyncio.run(_run())

    def test_publish_from_off_loop_thread_delivers(self):
        """Cross-thread path: publish via call_soon_threadsafe reaches subscriber."""
        received: list = []
        ready = threading.Event()
        done = threading.Event()
        bus = InProcessEventBus()

        async def _subscriber():
            sub = bus.subscribe("cross-thread")
            ready.set()  # loop captured; safe for main thread to publish now
            event = await asyncio.wait_for(sub.get(), timeout=2.0)
            received.append(event)
            done.set()

        loop = asyncio.new_event_loop()
        t = threading.Thread(target=loop.run_until_complete, args=(_subscriber(),))
        t.start()

        ready.wait(timeout=2.0)
        # Main thread is not the event-loop thread — this exercises call_soon_threadsafe.
        bus.publish("cross-thread", {"type": "op", "n": 7})

        done.wait(timeout=2.0)
        t.join(timeout=3.0)
        loop.close()

        assert received == [{"type": "op", "n": 7}]

    def test_publish_handles_subscribers_from_different_loops(self):
        """Each subscriber should receive via its own event loop when they differ."""
        bus = InProcessEventBus()
        background_ready = threading.Event()
        background_done = threading.Event()
        results = {"background": [], "main": []}

        async def _background_subscriber():
            sub = bus.subscribe("mixed-loops")
            background_ready.set()
            event = await asyncio.wait_for(sub.get(), timeout=2.0)
            results["background"].append(event)
            background_done.set()

        background_loop = asyncio.new_event_loop()
        background_thread = threading.Thread(
            target=background_loop.run_until_complete,
            args=(_background_subscriber(),),
        )
        background_thread.start()
        background_ready.wait(timeout=2.0)

        async def _main_loop_subscriber():
            sub = bus.subscribe("mixed-loops")
            bus.publish("mixed-loops", {"type": "op", "n": 99})
            event = await asyncio.wait_for(sub.get(), timeout=2.0)
            results["main"].append(event)

        asyncio.run(_main_loop_subscriber())

        background_done.wait(timeout=2.0)
        background_thread.join(timeout=3.0)
        background_loop.close()

        assert results == {
            "background": [{"type": "op", "n": 99}],
            "main": [{"type": "op", "n": 99}],
        }


class TestPresenceRegistry:
    def test_join_assigns_distinct_colors(self):
        p = PresenceRegistry()
        m1 = p.join("1234-5678", "c1", "Alice")
        m2 = p.join("1234-5678", "c2", "Bob")
        assert m1["color"] != m2["color"]
        assert p.count("1234-5678") == 2

    def test_rejoin_keeps_color(self):
        p = PresenceRegistry()
        m1 = p.join("1234-5678", "c1", "Alice")
        m1b = p.join("1234-5678", "c1", "Alice2")
        assert m1["color"] == m1b["color"]

    def test_default_display_name(self):
        p = PresenceRegistry()
        m = p.join("1234-5678", "c1", None)
        assert m["display_name"].startswith("Guest-")

    def test_leave_removes_member(self):
        p = PresenceRegistry()
        p.join("1234-5678", "c1", "Alice")
        p.leave("1234-5678", "c1")
        assert p.count("1234-5678") == 0
        assert p.roster("1234-5678") == []

    def test_reconnect_refcount_leave_returns_none_while_sibling_open(self):
        """Fast reconnect: first leave must not remove the roster entry."""
        p = PresenceRegistry()
        p.join("1234-5678", "c1", "Alice")  # old connection
        p.join("1234-5678", "c1", "Alice")  # new connection (same client_id)
        result = p.leave("1234-5678", "c1")  # old connection closes
        assert result is None  # sibling still open — keep roster
        assert p.count("1234-5678") == 1
        assert any(m["client_id"] == "c1" for m in p.roster("1234-5678"))

    def test_reconnect_refcount_leave_returns_member_when_last(self):
        """Last connection closing must return the member so callers broadcast presence_left."""
        p = PresenceRegistry()
        p.join("1234-5678", "c1", "Alice")
        p.join("1234-5678", "c1", "Alice")
        p.leave("1234-5678", "c1")  # first connection
        member = p.leave("1234-5678", "c1")  # second (last) connection
        assert member is not None
        assert member["client_id"] == "c1"
        assert p.count("1234-5678") == 0
        assert p.roster("1234-5678") == []

    def test_reconnect_color_preserved_across_second_join(self):
        """Color assigned on first join must survive a concurrent join for same client."""
        p = PresenceRegistry()
        m1 = p.join("1234-5678", "c1", "Alice")
        m2 = p.join("1234-5678", "c1", "Alice")
        assert m1["color"] == m2["color"]


class TestClaimMap:
    def test_claim_and_snapshot(self):
        cm = ClaimMap()
        cm.claim("1234-5678", "c1", ["n1", "n2"])
        assert cm.snapshot("1234-5678") == {"n1": "c1", "n2": "c1"}

    def test_claim_takeover_is_lww(self):
        cm = ClaimMap()
        cm.claim("1234-5678", "c1", ["n1"])
        cm.claim("1234-5678", "c2", ["n1"])
        assert cm.snapshot("1234-5678") == {"n1": "c2"}

    def test_release_only_own_claim(self):
        cm = ClaimMap()
        cm.claim("1234-5678", "c1", ["n1"])
        released = cm.release("1234-5678", "c2", ["n1"])
        assert released == []
        assert cm.snapshot("1234-5678") == {"n1": "c1"}
        released = cm.release("1234-5678", "c1", ["n1"])
        assert released == ["n1"]

    def test_release_all_on_disconnect(self):
        cm = ClaimMap()
        cm.claim("1234-5678", "c1", ["n1", "n2"])
        cm.claim("1234-5678", "c2", ["n3"])
        released = cm.release_all("1234-5678", "c1")
        assert set(released) == {"n1", "n2"}
        assert cm.snapshot("1234-5678") == {"n3": "c2"}

    def test_ttl_expiry(self):
        clock = {"t": 0.0}
        cm = ClaimMap(ttl=30.0, time_fn=lambda: clock["t"])
        cm.claim("1234-5678", "c1", ["n1"])
        clock["t"] = 31.0
        assert cm.snapshot("1234-5678") == {}
