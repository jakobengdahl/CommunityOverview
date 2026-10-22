"""
Tests for ``SessionManager.summon_viewport`` / ``summon_viewport_sync``: the
ephemeral "bring everyone here" broadcast. It reaches exactly the session's
current subscribers, is never persisted, sequenced or replayed, and carries
only the target region and the summoner's display name.
"""

import math

import pytest

from backend.core.session_manager import (
    RateLimited,
    SessionManager,
    SessionNotFound,
    _TokenBucket,
    validate_summon_bounds,
)
from backend.core.session_store import (
    InMemorySessionPersistenceBackend,
    OpError,
    SessionStore,
)

pytestmark = pytest.mark.asyncio

BOUNDS = {"x": 10, "y": -20, "width": 300, "height": 200}


def _manager() -> SessionManager:
    return SessionManager(SessionStore(InMemorySessionPersistenceBackend()))


def _drain(sub):
    out = []
    while not sub.queue.empty():
        out.append(sub.queue.get_nowait())
    return out


def _summons(sub):
    return [e for e in _drain(sub) if e.get("type") == "viewport_summoned"]


class TestFanOut:
    async def test_every_subscriber_of_the_session_and_no_other_session_receives_it(
        self,
    ):
        mgr = _manager()
        s = mgr.create_session()
        other = mgr.create_session()
        sub_a, _ = mgr.connect(s.id, "a", "Alice")
        sub_b, _ = mgr.connect(s.id, "b", "Bob")
        sub_other, _ = mgr.connect(other.id, "c", "Carol")
        for sub in (sub_a, sub_b, sub_other):
            _drain(sub)

        result = mgr.summon_viewport(s.id, "a", BOUNDS)

        expected = {
            "type": "viewport_summoned",
            "client_id": "a",
            "summoned_by": "Alice",
            "bounds": {"x": 10.0, "y": -20.0, "width": 300.0, "height": 200.0},
        }
        assert _summons(sub_a) == [expected]
        assert _summons(sub_b) == [expected]
        assert _drain(sub_other) == []
        assert result["connected_clients"] == 2

    async def test_the_event_carries_only_the_target_and_the_summoners_name(self):
        mgr = _manager()
        s = mgr.create_session()
        sub, _ = mgr.connect(s.id, "b", "Bob")
        _drain(sub)

        mgr.summon_viewport(
            s.id, "a", {**BOUNDS, "message": "click here", "script": "<x>"}
        )

        (event,) = _summons(sub)
        assert set(event) == {"type", "client_id", "summoned_by", "bounds"}
        assert set(event["bounds"]) == {"x", "y", "width", "height"}

    async def test_browser_summoner_is_named_by_its_presence_entry_not_the_request(
        self,
    ):
        mgr = _manager()
        s = mgr.create_session()
        sub, _ = mgr.connect(s.id, "b", "Bob")
        _drain(sub)

        mgr.summon_viewport(s.id, "not-connected", BOUNDS)

        (event,) = _summons(sub)
        assert event["summoned_by"] is None


class TestEphemeral:
    async def test_no_state_seq_ring_or_persistence_change(self):
        mgr = _manager()
        s = mgr.create_session()
        before_state = dict(s.state)
        before_seq = s.seq
        before_updated = s.updated_at
        persisted = []
        mgr.store.persist = lambda *a, **k: persisted.append(a)
        mgr.store.persist_snapshot = lambda *a, **k: persisted.append(a)

        mgr.summon_viewport(s.id, "a", BOUNDS)
        mgr.summon_viewport_sync(s.id, "mcp-agent", BOUNDS, display_name="AI")

        assert s.state == before_state
        assert s.seq == before_seq
        assert s.updated_at == before_updated
        assert persisted == []
        assert mgr.store.ops_since(s.id, before_seq) == []

    async def test_a_client_joining_after_the_summon_is_not_moved(self):
        mgr = _manager()
        s = mgr.create_session()
        mgr.summon_viewport(s.id, "a", BOUNDS)

        late, _ = mgr.connect(s.id, "late", "Late")
        catch_up = mgr.catch_up(s.id, None)

        assert _summons(late) == []
        assert "viewport_summoned" not in str(catch_up)


class TestValidation:
    @pytest.mark.parametrize(
        "bounds",
        [
            None,
            [],
            {"x": 0, "y": 0, "width": 10},
            {**BOUNDS, "x": "1"},
            {**BOUNDS, "x": True},
            {**BOUNDS, "x": math.nan},
            {**BOUNDS, "y": math.inf},
            {**BOUNDS, "width": -math.inf},
            {**BOUNDS, "x": 1e12},
            {**BOUNDS, "width": 0},
            {**BOUNDS, "height": -5},
        ],
    )
    async def test_invalid_bounds_are_refused_and_nothing_is_published(self, bounds):
        mgr = _manager()
        s = mgr.create_session()
        sub, _ = mgr.connect(s.id, "b", "Bob")
        _drain(sub)

        with pytest.raises(OpError):
            mgr.summon_viewport(s.id, "a", bounds)
        with pytest.raises(OpError):
            mgr.summon_viewport_sync(s.id, "mcp-agent", bounds, display_name="AI")

        assert _drain(sub) == []

    async def test_validate_accepts_the_largest_allowed_magnitude(self):
        out = validate_summon_bounds({"x": -1e7, "y": 1e7, "width": 1e7, "height": 1})
        assert out == {"x": -1e7, "y": 1e7, "width": 1e7, "height": 1.0}

    async def test_unknown_session_is_not_created(self):
        mgr = _manager()
        with pytest.raises(SessionNotFound):
            mgr.summon_viewport("1234-5678", "a", BOUNDS)
        with pytest.raises(SessionNotFound):
            mgr.summon_viewport_sync("1234-5678", "a", BOUNDS, display_name="AI")
        with pytest.raises(SessionNotFound):
            mgr.summon_viewport("not-an-id", "a", BOUNDS)
        assert mgr.get_session("1234-5678") is None

    async def test_client_id_is_required(self):
        mgr = _manager()
        s = mgr.create_session()
        with pytest.raises(OpError):
            mgr.summon_viewport(s.id, "", BOUNDS)


class TestRateLimit:
    async def test_browser_summons_spend_the_op_bucket(self):
        mgr = _manager()
        s = mgr.create_session()
        sub, _ = mgr.connect(s.id, "b", "Bob")
        _drain(sub)
        mgr._bucket = _TokenBucket(1.0, 0.0)

        mgr.summon_viewport(s.id, "a", BOUNDS)
        with pytest.raises(RateLimited):
            mgr.summon_viewport(s.id, "a", BOUNDS)

        assert len(_summons(sub)) == 1

    async def test_mcp_summons_spend_the_mcp_bucket_and_name_the_given_summoner(self):
        mgr = _manager()
        s = mgr.create_session()
        sub, _ = mgr.connect(s.id, "b", "Bob")
        _drain(sub)
        mgr._bucket = _TokenBucket(0.0, 0.0)

        mgr.summon_viewport_sync(
            s.id, "mcp-agent", BOUNDS, display_name="Planner", rate_limit_label="t"
        )

        (event,) = _summons(sub)
        assert event["summoned_by"] == "Planner"
        assert event["client_id"] == "mcp-agent"
