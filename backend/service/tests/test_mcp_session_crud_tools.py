# covers: backend/service/mcp_tools.py
"""
Tests for the MCP visualization-session CRUD tools registered in
``backend/service/mcp_tools.py`` (create/list/get/rename/delete).

These implement docs/MCP_SESSION_LIFECYCLE_CONTRACT.md. They are thin,
authorization-gated wrappers over ``SessionManager``; the underlying op
semantics are covered in ``backend/core/tests/test_session_manager.py``. The
sync rename/delete manager methods (the tool path cannot await) are exercised
here too, including their ``LayoutBusy`` guard.
"""

import os
from unittest.mock import MagicMock, Mock

import pytest

from backend.core import GraphStorage
from backend.core.session_manager import LayoutBusy, SessionManager, _TokenBucket
from backend.core.tests.rate_buckets import bucket_attrs
from backend.core.session_store import (
    InMemorySessionPersistenceBackend,
    SessionStore,
)
from backend.config.config_loader import PUBLIC_BASE_URL_ENV
from backend.runtime.authorization import AUTHORIZATION_MODE_ENV
from backend.service import GraphService, register_mcp_tools


@pytest.fixture
def crud_tools(tmp_path):
    """tools_map wired to an in-memory shared-session manager, plus that manager."""
    storage = GraphStorage(json_path=os.path.join(tmp_path, "g.json"))
    service = GraphService(storage)
    manager = SessionManager(SessionStore(InMemorySessionPersistenceBackend()))

    mock_mcp = Mock()
    mock_mcp.tool = MagicMock(return_value=lambda f: f)
    tools_map = register_mcp_tools(mock_mcp, service, session_manager=manager)
    return tools_map, manager


class TestCreate:
    def test_default_name_and_projection_shape(self, crud_tools, monkeypatch):
        tools_map, _ = crud_tools
        # No public base URL configured => session_url is null (see TestSessionUrl
        # for the configured case).
        monkeypatch.delenv(PUBLIC_BASE_URL_ENV, raising=False)
        result = tools_map["create_visualization_session"]()

        assert result["success"] is True
        session = result["session"]
        assert session["name"] == "Untitled session"
        assert session["lifecycle_state"] == "active"
        assert session["owner"] is None and session["workspace"] is None
        assert session["revision"] == 0
        assert session["node_count"] == 0
        # Open core is permissive: full capabilities are granted.
        assert session["capabilities"] == ["read", "rename", "delete", "layout"]
        assert session["session_url"] is None

    def test_explicit_name_is_used(self, crud_tools):
        tools_map, _ = crud_tools
        result = tools_map["create_visualization_session"](name="  Roadmap  ")
        assert result["session"]["name"] == "Roadmap"

    def test_names_are_not_unique(self, crud_tools):
        tools_map, _ = crud_tools
        a = tools_map["create_visualization_session"](name="Same")["session"]
        b = tools_map["create_visualization_session"](name="Same")["session"]
        assert a["session_id"] != b["session_id"]
        assert a["name"] == b["name"] == "Same"


class TestSessionUrl:
    def test_null_when_no_base_url_configured(self, crud_tools, monkeypatch):
        tools_map, _ = crud_tools
        monkeypatch.delenv(PUBLIC_BASE_URL_ENV, raising=False)
        created = tools_map["create_visualization_session"]()["session"]
        assert created["session_url"] is None

    def test_canonical_url_when_base_configured(self, crud_tools, monkeypatch):
        tools_map, _ = crud_tools
        monkeypatch.setenv(PUBLIC_BASE_URL_ENV, "https://app.example.test")
        result = tools_map["create_visualization_session"]()
        sid = result["session"]["session_id"]
        # Keeps the established ?session=<id> form on the configured origin.
        assert (
            result["session"]["session_url"]
            == f"https://app.example.test/?session={sid}"
        )

        # get and list project the same URL.
        got = tools_map["get_visualization_session"](session_id=sid)
        assert (
            got["session"]["session_url"] == f"https://app.example.test/?session={sid}"
        )
        listed = tools_map["list_visualization_sessions"]()["sessions"]
        assert any(
            s["session_url"] == f"https://app.example.test/?session={sid}"
            for s in listed
        )


class TestListAndGet:
    def test_list_newest_first(self, crud_tools):
        tools_map, _ = crud_tools
        first = tools_map["create_visualization_session"](name="first")["session"]
        second = tools_map["create_visualization_session"](name="second")["session"]

        listed = tools_map["list_visualization_sessions"]()
        assert listed["success"] is True
        assert listed["count"] == 2
        ids = [s["session_id"] for s in listed["sessions"]]
        # Both present; the manager orders by updated_at (newest first).
        assert set(ids) == {first["session_id"], second["session_id"]}

    def test_get_reports_node_count(self, crud_tools):
        tools_map, manager = crud_tools
        created = tools_map["create_visualization_session"]()["session"]
        session = manager.get_session(created["session_id"])
        manager.store.apply_state_op(
            session, {"op": "nodes_added", "node_ids": ["a", "b", "c"]}
        )
        manager.store.persist(session)

        result = tools_map["get_visualization_session"](
            session_id=created["session_id"]
        )
        assert result["success"] is True
        assert result["session"]["node_count"] == 3

    def test_get_invalid_id(self, crud_tools):
        tools_map, _ = crud_tools
        result = tools_map["get_visualization_session"](session_id="nope")
        assert result["success"] is False
        assert "Invalid session ID format" in result["error"]

    def test_get_missing(self, crud_tools):
        tools_map, _ = crud_tools
        result = tools_map["get_visualization_session"](
            session_id="1111-2222-3333-4444"
        )
        assert result["success"] is False
        assert "not found" in result["error"]


class TestRename:
    def test_rename_bumps_revision_and_is_op_routed(self, crud_tools):
        tools_map, manager = crud_tools
        created = tools_map["create_visualization_session"](name="old")["session"]
        sid = created["session_id"]

        result = tools_map["rename_visualization_session"](session_id=sid, name="new")
        assert result["success"] is True
        assert result["session"]["name"] == "new"
        assert result["session"]["revision"] == 1

        # Op-routed (contract §4): a client reconnecting via since_seq catch-up
        # sees the rename, so it must be in the ring buffer, not a bare store write.
        missed = manager.store.ops_since(sid, 0)
        assert missed is not None
        assert any(op.get("op") == "session_renamed" for op in missed)

    def test_rename_materialises_unknown_id(self, crud_tools):
        # R7: a rename for an id that only exists client-side must create it,
        # not 404 — otherwise the name is lost when the session later saves.
        tools_map, manager = crud_tools
        sid = "9999-8888-7777-6666"
        result = tools_map["rename_visualization_session"](
            session_id=sid, name="materialised"
        )
        assert result["success"] is True
        assert manager.get_session(sid).name == "materialised"

    def test_rename_clear_name(self, crud_tools):
        tools_map, _ = crud_tools
        created = tools_map["create_visualization_session"](name="temp")["session"]
        result = tools_map["rename_visualization_session"](
            session_id=created["session_id"], name=None
        )
        assert result["success"] is True
        assert result["session"]["name"] is None


class TestDelete:
    def test_delete_requires_confirmation(self, crud_tools):
        tools_map, manager = crud_tools
        created = tools_map["create_visualization_session"]()["session"]
        sid = created["session_id"]

        result = tools_map["delete_visualization_session"](session_id=sid)
        assert result["success"] is False
        assert result["error"] == "confirmation_required"
        # Not confirmed => still there.
        assert manager.get_session(sid) is not None

    def test_delete_confirmed_removes_and_broadcasts(self, crud_tools):
        tools_map, manager = crud_tools
        created = tools_map["create_visualization_session"]()["session"]
        sid = created["session_id"]
        subscription = manager.bus.subscribe(sid)

        result = tools_map["delete_visualization_session"](session_id=sid, confirm=True)
        assert result["success"] is True and result["deleted"] is True
        assert manager.get_session(sid) is None

        # Connected clients are notified (publish is synchronous, so the event is
        # already on the subscriber's queue).
        events = []
        while not subscription.queue.empty():
            events.append(subscription.queue.get_nowait())
        assert any(e.get("type") == "session_deleted" for e in events)

    def test_delete_missing(self, crud_tools):
        tools_map, _ = crud_tools
        result = tools_map["delete_visualization_session"](
            session_id="1111-2222-3333-4444", confirm=True
        )
        assert result["success"] is False
        assert "not found" in result["error"]


class TestAuthorization:
    def test_read_only_mode_blocks_mutations_and_narrows_capabilities(
        self, crud_tools, monkeypatch
    ):
        tools_map, _ = crud_tools
        # Create while permissive.
        created = tools_map["create_visualization_session"](name="x")["session"]
        sid = created["session_id"]

        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "read-only")

        # Reads still work, but capabilities drop the mutate verbs.
        got = tools_map["get_visualization_session"](session_id=sid)
        assert got["success"] is True
        assert got["session"]["capabilities"] == ["read"]

        # Mutations are denied.
        for call in (
            lambda: tools_map["create_visualization_session"](),
            lambda: tools_map["rename_visualization_session"](session_id=sid, name="y"),
            lambda: tools_map["delete_visualization_session"](
                session_id=sid, confirm=True
            ),
        ):
            denied = call()
            assert denied["success"] is False
            assert denied.get("error_code") == "access_denied"

    # Every session-index READ goes here, not just the first one written. A
    # session id is itself the capability — the grouped-digit shape exists so
    # an unauthenticated caller cannot feasibly enumerate live sessions (see
    # session_store.py) — so a tool that lists them must be refused under
    # deny-all. `search_reference_target_sessions` enumerates them with an
    # empty query, and shipped without this coverage: removing its
    # authorization check left the whole backend green, while removing the
    # identical check from `list_visualization_sessions` was killed here
    # immediately (round 1 mutation review, S4).
    @pytest.mark.parametrize(
        "tool_name",
        ["list_visualization_sessions", "search_reference_target_sessions"],
    )
    def test_deny_all_mode_blocks_reads(self, crud_tools, monkeypatch, tool_name):
        tools_map, _ = crud_tools
        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "deny-all")
        result = tools_map[tool_name]()
        assert result["success"] is False
        assert result.get("error_code") == "access_denied"


class TestBusyGuard:
    @pytest.mark.asyncio
    async def test_sync_rename_and_delete_refuse_when_locked(self, crud_tools):
        # A held per-session lock means an apply_ops batch is mid-flight; the sync
        # tool path must refuse rather than assign a seq the batch has not
        # broadcast yet (mirrors apply_layout's LayoutBusy guard).
        tools_map, manager = crud_tools
        created = tools_map["create_visualization_session"]()["session"]
        sid = created["session_id"]

        lock = manager._lock(sid)
        await lock.acquire()
        try:
            with pytest.raises(LayoutBusy):
                manager.rename_session_sync(sid, "blocked")
            with pytest.raises(LayoutBusy):
                manager.delete_session_sync(sid)
        finally:
            lock.release()

        # The tools surface it as a retryable "busy" result.
        assert (
            tools_map["rename_visualization_session"](session_id=sid, name="ok")[
                "success"
            ]
            is True
        )


class _RecordingBucket:
    def __init__(self, allow=True):
        self.allow = allow
        self.consumed = []

    def consume(self, key, n=1.0):
        self.consumed.append((key, n))
        return self.allow


def _record_buckets(manager, allow=True):
    buckets = {attr: _RecordingBucket(allow) for attr in bucket_attrs(manager)}
    for attr, bucket in buckets.items():
        setattr(manager, attr, bucket)
    return buckets


def _charges(buckets):
    return {
        attr: bucket.consumed for attr, bucket in buckets.items() if bucket.consumed
    }


class TestRateLimit:
    """Rename and delete are session writes like every other synchronous MCP
    write: each is charged one token to ``_mcp_bucket`` under its own tool
    label, and refused as ``rate_limited`` when that budget is spent."""

    def test_rename_draws_one_token_from_its_own_mcp_key(self, crud_tools):
        tools_map, manager = crud_tools
        sid = tools_map["create_visualization_session"]()["session"]["session_id"]
        buckets = _record_buckets(manager)

        result = tools_map["rename_visualization_session"](session_id=sid, name="x")

        assert result["success"] is True
        assert _charges(buckets) == {
            "_mcp_bucket": [("mcp-agent:rename_visualization_session", 1.0)]
        }

    def test_confirmed_delete_draws_one_token_from_its_own_mcp_key(self, crud_tools):
        tools_map, manager = crud_tools
        sid = tools_map["create_visualization_session"]()["session"]["session_id"]
        buckets = _record_buckets(manager)

        unconfirmed = tools_map["delete_visualization_session"](session_id=sid)
        assert unconfirmed["error"] == "confirmation_required"
        assert _charges(buckets) == {}

        result = tools_map["delete_visualization_session"](session_id=sid, confirm=True)

        assert result["success"] is True
        assert _charges(buckets) == {
            "_mcp_bucket": [("mcp-agent:delete_visualization_session", 1.0)]
        }

    def test_rename_is_refused_when_its_budget_is_spent(self, crud_tools):
        tools_map, manager = crud_tools
        sid = tools_map["create_visualization_session"](name="Before")["session"][
            "session_id"
        ]
        manager._mcp_bucket = _TokenBucket(1.0, 0.0)

        assert tools_map["rename_visualization_session"](session_id=sid, name="A")[
            "success"
        ]
        result = tools_map["rename_visualization_session"](session_id=sid, name="B")

        assert result["success"] is False
        assert result["error"] == "rate_limited"
        assert manager.get_session(sid).name == "A"
        # Delete has its own key, so a spent rename budget does not block it.
        assert tools_map["delete_visualization_session"](session_id=sid, confirm=True)[
            "success"
        ]

    def test_delete_is_refused_when_its_budget_is_spent(self, crud_tools):
        tools_map, manager = crud_tools
        first = tools_map["create_visualization_session"]()["session"]["session_id"]
        second = tools_map["create_visualization_session"]()["session"]["session_id"]
        manager._mcp_bucket = _TokenBucket(1.0, 0.0)

        assert tools_map["delete_visualization_session"](
            session_id=first, confirm=True
        )["success"]
        result = tools_map["delete_visualization_session"](
            session_id=second, confirm=True
        )

        assert result["success"] is False
        assert result["error"] == "rate_limited"
        assert manager.get_session(second) is not None
        # Rename has its own key, so a spent delete budget does not block it.
        assert tools_map["rename_visualization_session"](session_id=second, name="R")[
            "success"
        ]

    def test_ops_bucket_spent_does_not_refuse_rename_or_delete(self, crud_tools):
        tools_map, manager = crud_tools
        sid = tools_map["create_visualization_session"]()["session"]["session_id"]
        manager._bucket = _TokenBucket(0.0, 0.0)

        assert tools_map["rename_visualization_session"](session_id=sid, name="x")[
            "success"
        ]
        assert tools_map["delete_visualization_session"](session_id=sid, confirm=True)[
            "success"
        ]


class TestSessionActivity:
    def test_deleted_annotation_payload_is_read_without_mutation(self, crud_tools):
        """Reading preserves the prior annotation, inverse op and session state."""
        from copy import deepcopy

        tools, manager = crud_tools
        session = manager.store.create()
        annotation = {"id": "note-1", "type": "note", "text": "Original"}
        manager.store.apply_state_op(
            session,
            {
                "op": "annotation_created",
                "annotation": annotation,
                "client_id": "actor-1",
            },
        )
        annotation = deepcopy(session.state["annotations"][0])
        manager.store.apply_state_op(
            session,
            {
                "op": "annotation_deleted",
                "annotation_id": "note-1",
                "client_id": "actor-1",
            },
        )
        before = deepcopy(session)
        result = tools["get_session_activity"](session.id)
        assert result["session_id"] == session.id
        assert result["activity"] == list(reversed(before.activity_log))
        deleted = result["activity"][0]
        assert deleted["before"] == annotation
        assert deleted["inverse_op"] == {
            "op": "annotation_created",
            "annotation": annotation,
        }
        assert session == before

    def test_action_filter_precedes_paging(self, crud_tools):
        """Pages count and advance through matching actions in newest-first order."""
        tools, manager = crud_tools
        session = manager.store.create()
        session.activity_log = [
            {"id": str(i), "op": "annotation_deleted" if i % 2 else "node_moved"}
            for i in range(6)
        ]
        read = tools["get_session_activity"]
        first = read(session.id, action="annotation_deleted", limit=2)
        assert [r["id"] for r in first["activity"]] == ["5", "3"]
        assert (first["total"], first["next_offset"]) == (3, 2)
        last = read(session.id, action="annotation_deleted", limit=2, offset=2)
        assert [r["id"] for r in last["activity"]] == ["1"]
        assert last["next_offset"] is None
        assert read(session.id, action="unknown")["activity"] == []
        assert read(session.id, offset=6)["activity"] == []

    @pytest.mark.parametrize("mode", ["read-only", "deny-all"])
    def test_activity_uses_read_authorization(self, crud_tools, monkeypatch, mode):
        """Activity reads obey the same read policy as session state."""
        tools, manager = crud_tools
        session = manager.store.create()
        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, mode)
        result = tools["get_session_activity"](session.id)
        if mode == "deny-all":
            assert result["error_code"] == "access_denied"
        else:
            assert result["activity"] == []

    @pytest.mark.parametrize(
        "arguments",
        [
            {"limit": 0},
            {"limit": 501},
            {"offset": -1},
            {"limit": True},
            {"offset": 1.5},
            {"action": []},
        ],
    )
    def test_invalid_paging_is_refused(self, crud_tools, arguments):
        """Invalid boundaries cannot silently change the page requested."""
        tools, manager = crud_tools
        assert "error" in tools["get_session_activity"](
            manager.store.create().id, **arguments
        )

    @pytest.mark.parametrize("session_id", ["invalid", "1111-2222-3333-4444"])
    def test_invalid_or_missing_session_is_refused(self, crud_tools, session_id):
        """Activity lookup neither creates sessions nor accepts malformed IDs."""
        tools, manager = crud_tools
        assert "error" in tools["get_session_activity"](session_id)
        assert manager.get_session(session_id) is None
