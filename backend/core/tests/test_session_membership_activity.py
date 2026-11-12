# covers: backend/core/session_activity.py, backend/core/session_store.py
"""Membership retention does not consume undo history."""

from backend.core import session_activity
from backend.core.session_store import SessionStore
from backend.core.tests.test_session_activity import _store


def test_membership_caps_preserve_undo_and_user_attribution(monkeypatch):
    """Membership has a separate cap, bounded ids, no inverse or duplicate ids."""
    monkeypatch.setattr(session_activity, "DEFAULT_MAX_MEMBERSHIP_RECORDS", 3)
    store = _store(max_activity_records=1)
    session = store.create()
    store.apply_state_op(
        session,
        {
            "op": "node_moved",
            "node_id": "old",
            "position": {"x": 1, "y": 2},
            "client_id": "user",
        },
    )
    undoable = session.activity_log[0]
    ids = [f"n{i}" for i in range(session_activity.MAX_ACTIVITY_NODE_IDS + 5)]
    for op in ["nodes_added", "nodes_removed", "nodes_added", "nodes_removed"]:
        store.apply_state_op(
            session, {"op": op, "node_ids": ids + ids, "client_id": "user"}
        )
    store.apply_state_op(
        session, {"op": "nodes_removed", "node_ids": ids, "client_id": "other"}
    )
    assert len(session.activity_log) == 4
    assert store.find_latest_undoable(session, "user") == undoable
    for record in session.activity_log[1:]:
        assert record["actor"] == "user"
        assert record["affected"] == {
            "kind": "nodes",
            "count": len(ids),
            "ids": ids[: session_activity.MAX_ACTIVITY_NODE_IDS],
        }
        assert record["before"] is None and record["after"] is None
        assert record["inverse_op"] is None and "forward_op" not in record
    store.persist(session)
    assert (
        SessionStore(store._backend).get(session.id).activity_log
        == session.activity_log
    )
