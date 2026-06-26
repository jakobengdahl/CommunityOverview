"""
Tests for SessionRegistry.

Covers: session lifecycle, state management, command delivery (sync and async),
invalid ID validation, and TTL eviction.
"""

import asyncio
import time
import pytest

from backend.core.session_registry import SessionRegistry, SESSION_ID_RE


class TestSessionIdValidation:
    """is_valid_session_id behaves correctly for all formats."""

    def test_valid_format_accepted(self):
        assert SessionRegistry.is_valid_session_id("8244-1742")
        assert SessionRegistry.is_valid_session_id("0000-0000")
        assert SessionRegistry.is_valid_session_id("9999-9999")

    def test_too_many_digits_rejected(self):
        assert not SessionRegistry.is_valid_session_id("12345-1234")
        assert not SessionRegistry.is_valid_session_id("1234-12345")

    def test_too_few_digits_rejected(self):
        assert not SessionRegistry.is_valid_session_id("123-1234")
        assert not SessionRegistry.is_valid_session_id("1234-123")

    def test_non_digits_rejected(self):
        assert not SessionRegistry.is_valid_session_id("abcd-1234")
        assert not SessionRegistry.is_valid_session_id("1234-wxyz")

    def test_missing_separator_rejected(self):
        assert not SessionRegistry.is_valid_session_id("12341234")

    def test_empty_string_rejected(self):
        assert not SessionRegistry.is_valid_session_id("")


class TestSessionLifecycle:
    """session creation and existence checks."""

    def test_get_or_create_returns_queue(self):
        reg = SessionRegistry()
        queue = reg.get_or_create("1234-5678")
        assert isinstance(queue, asyncio.Queue)

    def test_get_or_create_same_queue_on_repeat(self):
        reg = SessionRegistry()
        q1 = reg.get_or_create("1234-5678")
        q2 = reg.get_or_create("1234-5678")
        assert q1 is q2

    def test_session_exists_false_before_create(self):
        reg = SessionRegistry()
        assert not reg.session_exists("1234-5678")

    def test_session_exists_true_after_create(self):
        reg = SessionRegistry()
        reg.get_or_create("1234-5678")
        assert reg.session_exists("1234-5678")

    def test_session_count(self):
        reg = SessionRegistry()
        assert reg.session_count == 0
        reg.get_or_create("1111-2222")
        reg.get_or_create("3333-4444")
        assert reg.session_count == 2


class TestStateManagement:
    """update_state / get_state."""

    def test_get_state_returns_none_for_unknown(self):
        reg = SessionRegistry()
        assert reg.get_state("9999-9999") is None

    def test_update_state_returns_false_for_unknown(self):
        reg = SessionRegistry()
        assert reg.update_state("9999-9999", {"visible_node_ids": []}) is False

    def test_update_and_get_state_roundtrip(self):
        reg = SessionRegistry()
        reg.get_or_create("1234-5678")
        state = {"visible_node_ids": ["a", "b"], "node_count": 2}
        assert reg.update_state("1234-5678", state)
        assert reg.get_state("1234-5678") == state

    def test_update_state_replaces_previous(self):
        reg = SessionRegistry()
        reg.get_or_create("1234-5678")
        reg.update_state("1234-5678", {"visible_node_ids": ["a"]})
        reg.update_state("1234-5678", {"visible_node_ids": ["b", "c"]})
        assert reg.get_state("1234-5678") == {"visible_node_ids": ["b", "c"]}


class TestPushCommandSync:
    """push_command_sync via the running event loop."""

    def test_push_returns_false_for_unknown_session(self):
        reg = SessionRegistry()
        assert not reg.push_command_sync("9999-9999", {"type": "test"})

    def test_push_returns_false_without_loop(self):
        reg = SessionRegistry()
        reg.get_or_create("1234-5678")
        # No event loop injected
        assert not reg.push_command_sync("1234-5678", {"type": "test"})

    @pytest.mark.asyncio
    async def test_push_delivers_to_queue(self):
        reg = SessionRegistry()
        loop = asyncio.get_running_loop()
        reg.set_event_loop(loop)
        queue = reg.get_or_create("1234-5678")

        ok = reg.push_command_sync("1234-5678", {"type": "hello", "value": 42})
        assert ok

        # Give the event loop a tick to process call_soon_threadsafe
        await asyncio.sleep(0)
        assert not queue.empty()
        cmd = queue.get_nowait()
        assert cmd == {"type": "hello", "value": 42}


class TestPushCommandAsync:
    """push_command (async variant)."""

    @pytest.mark.asyncio
    async def test_push_delivers_to_queue(self):
        reg = SessionRegistry()
        queue = reg.get_or_create("1234-5678")
        ok = await reg.push_command("1234-5678", {"type": "async-test"})
        assert ok
        cmd = await queue.get()
        assert cmd == {"type": "async-test"}

    @pytest.mark.asyncio
    async def test_push_returns_false_for_unknown_session(self):
        reg = SessionRegistry()
        ok = await reg.push_command("9999-9999", {"type": "x"})
        assert not ok


class TestStream:
    """stream() async generator yields commands and pings."""

    @pytest.mark.asyncio
    async def test_stream_yields_pushed_command(self):
        reg = SessionRegistry()
        reg.get_or_create("1234-5678")
        await reg.push_command("1234-5678", {"type": "cmd", "action": "test"})

        received = []
        async for item in reg.stream("1234-5678"):
            received.append(item)
            break  # stop after first item

        assert received == [{"type": "cmd", "action": "test"}]


class TestTTLEviction:
    """cleanup_stale removes old sessions."""

    def test_cleanup_removes_old_session(self):
        reg = SessionRegistry()
        reg.get_or_create("1234-5678")
        # Manually backdate last_seen to simulate stale session
        reg._sessions["1234-5678"]["last_seen"] = time.monotonic() - 7200
        evicted = reg.cleanup_stale()
        assert evicted == 1
        assert not reg.session_exists("1234-5678")

    def test_cleanup_keeps_fresh_session(self):
        reg = SessionRegistry()
        reg.get_or_create("1234-5678")
        evicted = reg.cleanup_stale()
        assert evicted == 0
        assert reg.session_exists("1234-5678")
