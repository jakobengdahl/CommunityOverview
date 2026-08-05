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
from backend.core.session_manager import LayoutBusy, SessionManager
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

    def test_deny_all_mode_blocks_reads(self, crud_tools, monkeypatch):
        tools_map, _ = crud_tools
        monkeypatch.setenv(AUTHORIZATION_MODE_ENV, "deny-all")
        result = tools_map["list_visualization_sessions"]()
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
