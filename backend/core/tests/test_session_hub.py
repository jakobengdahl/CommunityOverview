"""
Tests for the ephemeral realtime layer (step 2 + step 3 claims) in
``backend/core/session_hub.py``: broadcast fan-out, slow-consumer resync,
presence roster/colour assignment, and selection-claim TTL/release.
"""

import asyncio
import threading

import pytest

from backend.core.session_hub import ClaimMap, InProcessEventBus, PresenceRegistry


class TestInProcessEventBus:
    """publish() is called from within SessionManager's async methods in
    production, so a loop is always running; these tests run under
    pytest-asyncio for the same reason (the no-loop / cross-thread paths are
    covered separately below)."""

    pytestmark = pytest.mark.asyncio

    async def test_broadcast_to_all_subscribers(self):
        bus = InProcessEventBus()
        a = bus.subscribe("1234-5678")
        b = bus.subscribe("1234-5678")
        bus.publish("1234-5678", {"type": "op", "n": 1})
        assert a.queue.get_nowait() == {"type": "op", "n": 1}
        assert b.queue.get_nowait() == {"type": "op", "n": 1}

    async def test_isolated_per_session(self):
        bus = InProcessEventBus()
        a = bus.subscribe("1111-1111")
        bus.subscribe("2222-2222")
        bus.publish("2222-2222", {"x": 1})
        assert a.queue.empty()

    async def test_unsubscribe_stops_delivery(self):
        bus = InProcessEventBus()
        a = bus.subscribe("1234-5678")
        bus.unsubscribe(a)
        bus.publish("1234-5678", {"x": 1})
        assert a.queue.empty()
        assert bus.subscriber_count("1234-5678") == 0

    async def test_slow_consumer_gets_resync(self):
        bus = InProcessEventBus(queue_max=2)
        a = bus.subscribe("1234-5678")
        for i in range(5):
            bus.publish("1234-5678", {"type": "op", "n": i})
        drained = []
        while not a.queue.empty():
            drained.append(a.queue.get_nowait())
        assert a.needs_resync is True
        assert drained[-1] == {"type": "resync"}


class TestInProcessEventBusThreadSafety:
    """A cross-thread caller must not touch asyncio.Queue directly (unsafe)."""

    def test_publish_from_worker_thread_uses_call_soon_threadsafe(self):
        async def scenario():
            bus = InProcessEventBus()
            bus.set_event_loop(asyncio.get_running_loop())
            sub = bus.subscribe("1234-5678")

            def publish_from_thread():
                bus.publish("1234-5678", {"type": "op", "n": 1})

            thread = threading.Thread(target=publish_from_thread)
            thread.start()
            thread.join(timeout=2)
            event = await asyncio.wait_for(sub.queue.get(), timeout=2)
            assert event == {"type": "op", "n": 1}

        asyncio.run(scenario())

    def test_publish_from_worker_thread_without_registered_loop_is_noop(self):
        bus = InProcessEventBus()
        sub = bus.subscribe("1234-5678")

        thread = threading.Thread(target=lambda: bus.publish("1234-5678", {"type": "op"}))
        thread.start()
        thread.join(timeout=2)

        assert sub.queue.empty()


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
