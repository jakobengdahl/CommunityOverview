"""
Tests for the per-session activity log (``backend/core/session_activity.py``)
and its integration into ``SessionStore.apply_state_op``: record shape,
actor scoping, retention (max records / max age) and persistence.
"""

from datetime import datetime, timedelta, timezone

from backend.core import session_activity
from backend.core.session_activity import (
    build_activity_record,
    current_snapshot_for,
    find_latest_undoable,
    prune_activity_log,
    undo_conflict_reason,
)
from backend.core.session_store import (
    FileSessionPersistenceBackend,
    InMemorySessionPersistenceBackend,
    SessionStore,
)


def _store(**kwargs) -> SessionStore:
    return SessionStore(InMemorySessionPersistenceBackend(), **kwargs)


def _record(**overrides):
    base = dict(
        op_type="annotation_updated",
        actor="a1",
        session_id="1234-5678",
        seq=1,
        correlation_id=None,
        affected={"kind": "annotation", "id": "note-1"},
        before={"id": "note-1", "text": "old"},
        after={"id": "note-1", "text": "new"},
        inverse_op={
            "op": "annotation_updated",
            "annotation": {"id": "note-1", "text": "old"},
        },
    )
    base.update(overrides)
    return build_activity_record(**base)


class TestPruneActivityLog:
    def test_keeps_all_within_both_caps(self):
        records = [_record(seq=i) for i in range(5)]
        kept = prune_activity_log(records, max_records=500, max_age_days=7)
        assert kept == records

    def test_trims_to_max_records_keeping_newest(self):
        records = [_record(seq=i) for i in range(10)]
        kept = prune_activity_log(records, max_records=3, max_age_days=None)
        assert [r["seq"] for r in kept] == [7, 8, 9]

    def test_trims_by_age(self):
        records = [_record(seq=i) for i in range(3)]
        stale_cutoff = (datetime.now(timezone.utc) - timedelta(days=10)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        records[0]["occurred_at"] = stale_cutoff
        kept = prune_activity_log(records, max_records=None, max_age_days=7)
        assert [r["seq"] for r in kept] == [1, 2]

    def test_unparseable_timestamp_is_kept(self):
        records = [_record(seq=0)]
        records[0]["occurred_at"] = "not-a-timestamp"
        kept = prune_activity_log(records, max_records=None, max_age_days=7)
        assert kept == records

    def test_disabled_retention_is_a_no_op(self):
        records = [_record(seq=i) for i in range(600)]
        kept = prune_activity_log(records, max_records=None, max_age_days=None)
        assert kept == records

    def test_age_filter_matches_parsing_every_timestamp(self, monkeypatch):
        """The string-compare shortcut keeps exactly what parsing keeps,
        including a timestamp inside the cutoff's own second, an unpadded one
        that sorts after the cutoff but is older, and an out-of-range one."""
        now = datetime(2026, 10, 20, 12, 0, 0, 500000, tzinfo=timezone.utc)

        class _FixedNow(datetime):
            @classmethod
            def now(cls, tz=None):
                return now

        monkeypatch.setattr(session_activity, "datetime", _FixedNow)
        cutoff = now - timedelta(days=7)
        stamps = [
            "2026-10-13T12:00:00Z",  # same second as the cutoff, 0.5 s older
            "2026-10-13T12:00:01Z",
            "2026-9-30T00:00:00Z",  # unpadded: sorts after the cutoff, is older
            "2026-10-01T00:00:00Z",
            "2026-13-01T00:00:00Z",  # out of range: unparseable, kept
            "2026-10-20T11:00:00Z",
            "not-a-timestamp",
        ]
        records = [_record(seq=i) for i in range(len(stamps))]
        for record, stamp in zip(records, stamps):
            record["occurred_at"] = stamp

        def parsed_within_age(stamp):
            try:
                ts = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                return True
            return ts.replace(tzinfo=timezone.utc) >= cutoff

        kept = prune_activity_log(records, max_records=None, max_age_days=7)
        assert [r["seq"] for r in kept] == [
            r["seq"] for r in records if parsed_within_age(r["occurred_at"])
        ]
        assert [r["seq"] for r in kept] == [1, 4, 5, 6]

    def _frozen(self, monkeypatch, now):
        class _FixedNow(datetime):
            @classmethod
            def now(cls, tz=None):
                return now

        monkeypatch.setattr(session_activity, "datetime", _FixedNow)

    def test_a_record_exactly_at_a_whole_second_cutoff_is_kept(self, monkeypatch):
        self._frozen(monkeypatch, datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc))
        records = [_record(seq=0), _record(seq=1)]
        records[0]["occurred_at"] = "2026-10-13T12:00:00Z"  # == cutoff
        records[1]["occurred_at"] = "2026-10-13T11:59:59Z"
        kept = prune_activity_log(records, max_records=None, max_age_days=7)
        assert [r["seq"] for r in kept] == [0]

    def test_a_lowercase_older_timestamp_is_parsed_not_shortcut(self, monkeypatch):
        """strptime accepts a lowercase ``t``/``z``, and ``t`` sorts after
        ``T``: an older lowercase timestamp on the cutoff's own day must
        still be parsed and pruned."""
        self._frozen(monkeypatch, datetime(2026, 10, 20, 12, 0, 0, tzinfo=timezone.utc))
        records = [_record(seq=0)]
        records[0]["occurred_at"] = "2026-10-13t00:00:00z"
        assert prune_activity_log(records, max_records=None, max_age_days=7) == []

    def test_age_filter_runs_before_the_record_cap(self):
        """An expired record in the tail must not take a slot from the cap."""
        records = [_record(seq=i) for i in range(4)]
        records[-1]["occurred_at"] = "2000-01-01T00:00:00Z"
        kept = prune_activity_log(records, max_records=3, max_age_days=7)
        assert [r["seq"] for r in kept] == [0, 1, 2]


class TestFindLatestUndoable:
    def test_returns_newest_matching_actor(self):
        records = [
            _record(seq=1, actor="a1"),
            _record(seq=2, actor="a2"),
            _record(seq=3, actor="a1"),
        ]
        found = find_latest_undoable(records, "a1")
        assert found["seq"] == 3

    def test_skips_already_undone(self):
        records = [_record(seq=1, actor="a1"), _record(seq=2, actor="a1")]
        records[1]["undone"] = True
        found = find_latest_undoable(records, "a1")
        assert found["seq"] == 1

    def test_skips_records_without_an_inverse_op(self):
        records = [_record(seq=1, actor="a1", inverse_op=None)]
        assert find_latest_undoable(records, "a1") is None

    def test_no_match_returns_none(self):
        records = [_record(seq=1, actor="a1")]
        assert find_latest_undoable(records, "someone-else") is None


class TestConflictDetection:
    def test_no_conflict_when_state_matches_after_snapshot(self):
        state = {"annotations": [{"id": "note-1", "text": "new"}]}
        record = _record()
        assert current_snapshot_for(state, record) == record["after"]
        assert undo_conflict_reason(state, record) is None

    def test_conflict_when_annotation_changed_since(self):
        state = {"annotations": [{"id": "note-1", "text": "someone-else-edited"}]}
        record = _record()
        assert undo_conflict_reason(state, record) is not None

    def test_conflict_when_annotation_deleted_since(self):
        state = {"annotations": []}
        record = _record()
        assert undo_conflict_reason(state, record) is not None


class TestSessionStoreActivityIntegration:
    def test_annotation_created_appends_an_activity_record(self):
        store = _store()
        session = store.create()
        applied = store.apply_state_op(
            session,
            {
                "op": "annotation_created",
                "annotation": {"id": "note-1", "type": "note"},
                "client_id": "actor-1",
            },
        )
        assert applied is not None
        assert len(session.activity_log) == 1
        record = session.activity_log[0]
        assert record["op"] == "annotation_created"
        assert record["actor"] == "actor-1"
        assert record["affected"] == {"kind": "annotation", "id": "note-1"}
        assert record["before"] is None
        assert record["after"]["id"] == "note-1"
        assert record["inverse_op"] == {
            "op": "annotation_deleted",
            "annotation_id": "note-1",
        }

    def test_annotation_updated_activity_record_omits_unused_affected_fields(self):
        store = _store()
        session = store.create()
        store.apply_state_op(
            session,
            {
                "op": "annotation_created",
                "annotation": {"id": "note-1", "type": "note", "text": "old"},
                "client_id": "actor-1",
            },
        )
        applied = store.apply_state_op(
            session,
            {
                "op": "annotation_updated",
                "annotation": {"id": "note-1", "type": "note", "text": "new"},
                "client_id": "actor-1",
            },
        )
        assert applied is not None
        record = session.activity_log[-1]
        assert record["op"] == "annotation_updated"
        assert record["affected"] == {"kind": "annotation", "id": "note-1"}
        assert record["before"]["text"] == "old"
        assert record["after"]["text"] == "new"

    def test_annotation_created_upsert_activity_record_omits_unused_affected_fields(
        self,
    ):
        store = _store()
        session = store.create()
        store.apply_state_op(
            session,
            {
                "op": "annotation_created",
                "annotation": {"id": "note-1", "type": "note", "text": "old"},
                "client_id": "actor-1",
            },
        )
        applied = store.apply_state_op(
            session,
            {
                "op": "annotation_created",
                "annotation": {"id": "note-1", "type": "note", "text": "new"},
                "client_id": "actor-1",
            },
        )
        assert applied is not None
        record = session.activity_log[-1]
        assert record["op"] == "annotation_created"
        assert record["affected"] == {"kind": "annotation", "id": "note-1"}
        assert record["before"]["text"] == "old"
        assert record["after"]["text"] == "new"

    def test_no_actor_no_activity_record(self):
        store = _store()
        session = store.create()
        store.apply_state_op(
            session,
            {
                "op": "annotation_created",
                "annotation": {"id": "note-1", "type": "note"},
            },
        )
        assert session.activity_log == []

    def test_record_activity_false_suppresses_logging(self):
        store = _store()
        session = store.create()
        store.apply_state_op(
            session,
            {
                "op": "annotation_created",
                "annotation": {"id": "note-1", "type": "note"},
                "client_id": "actor-1",
            },
            record_activity=False,
        )
        assert session.activity_log == []

    def test_non_undoable_op_does_not_log(self):
        store = _store()
        session = store.create()
        store.apply_state_op(
            session,
            {"op": "nodes_added", "node_ids": ["n1"], "client_id": "actor-1"},
        )
        assert session.activity_log == []

    def test_retention_trims_by_max_records(self):
        store = _store(max_activity_records=3, ring_size=50)
        session = store.create()
        for i in range(6):
            store.apply_state_op(
                session,
                {
                    "op": "annotation_created",
                    "annotation": {"id": f"note-{i}", "type": "note"},
                    "client_id": "actor-1",
                },
            )
        assert len(session.activity_log) == 3
        ids = [r["affected"]["id"] for r in session.activity_log]
        assert ids == ["note-3", "note-4", "note-5"]

    def test_activity_log_persists_and_reloads(self, tmp_path):
        store = SessionStore(FileSessionPersistenceBackend(tmp_path / "sessions"))
        session = store.create()
        store.apply_state_op(
            session,
            {
                "op": "annotation_created",
                "annotation": {"id": "note-1", "type": "note"},
                "client_id": "actor-1",
            },
        )
        store.persist(session)

        reloaded_store = SessionStore(
            FileSessionPersistenceBackend(tmp_path / "sessions")
        )
        reloaded = reloaded_store.get(session.id)
        assert len(reloaded.activity_log) == 1
        assert reloaded.activity_log[0]["actor"] == "actor-1"

    def test_delete_records_the_prior_annotation_for_restore(self):
        store = _store()
        session = store.create()
        store.apply_state_op(
            session,
            {
                "op": "annotation_created",
                "annotation": {"id": "note-1", "type": "note", "text": "hi"},
                "client_id": "actor-1",
            },
        )
        store.apply_state_op(
            session,
            {
                "op": "annotation_deleted",
                "annotation_id": "note-1",
                "client_id": "actor-1",
            },
        )
        delete_record = session.activity_log[-1]
        assert delete_record["op"] == "annotation_deleted"
        assert delete_record["affected"] == {"kind": "annotation", "id": "note-1"}
        assert delete_record["before"]["text"] == "hi"
        assert delete_record["after"] is None
        assert delete_record["inverse_op"]["op"] == "annotation_created"
        assert delete_record["inverse_op"]["annotation"]["text"] == "hi"

    def test_deleting_an_unknown_id_does_not_log(self):
        store = _store()
        session = store.create()
        store.apply_state_op(
            session,
            {
                "op": "annotation_deleted",
                "annotation_id": "ghost",
                "client_id": "actor-1",
            },
        )
        assert session.activity_log == []
