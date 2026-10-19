"""Tests for run-history retention: policy, sweep, sweeper, CLI and read window."""

import json
import logging
from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from backend.agents.config import AgentsSettings
from backend.agents.execution import (
    ExecutionJob,
    ExecutionKind,
    ExecutionState,
    InMemoryExecutionStore,
    RetryPolicy,
    SqliteExecutionStore,
)
from backend.agents.execution.contract import T0
from backend.agents.execution.models import ABANDONED_REASON
from backend.agents.run_history import AgentRunRecorder
from backend.agents.run_history_retention import (
    RETENTION_FLOOR_DAYS,
    RetentionPolicy,
    RetentionSweeper,
    main,
    sweep,
)

SECRET = "secret-content-xyz"


def _store(kind="memory", tmp_path=None):
    policy = RetryPolicy(max_attempts=1)
    if kind == "sqlite":
        return SqliteExecutionStore(tmp_path / "runs.db", retry_policy=policy)
    return InMemoryExecutionStore(retry_policy=policy)


def _run(store, key, *, finished_days_ago, state=ExecutionState.SUCCEEDED):
    """
    A run that reached ``state`` ``finished_days_ago`` days before T0; a run left
    RUNNING was last updated then.
    """
    finished = T0 - timedelta(days=finished_days_ago)
    job = ExecutionJob(
        agent_id="a1",
        kind=ExecutionKind.EVENT,
        idempotency_key=key,
        payload={
            "agent_name": "One",
            "event": {
                "event_type": "node.update",
                "entity": {"kind": "node", "id": "n1", "type": "Actor"},
                "summary": SECRET,
            },
        },
        state=ExecutionState.RUNNING,
        attempts=1,
        correlation_id="corr",
        session_id=SECRET,
        run_at=finished - timedelta(seconds=5),
        created_at=finished - timedelta(seconds=5),
        updated_at=finished,
    )
    stored = store.enqueue(job)
    if state == ExecutionState.SUCCEEDED:
        return store.complete(stored.id, result={"summary": SECRET}, now=finished)
    if state == ExecutionState.DEAD_LETTER:
        return store.fail(stored.id, error=SECRET, now=finished)
    return stored  # left RUNNING


# -- policy ------------------------------------------------------------------


class TestRetentionPolicy:
    def test_unset_environment_keeps_everything(self):
        policy = RetentionPolicy.from_env({})
        assert policy == RetentionPolicy()
        assert not policy.sweeps
        assert policy.read_cutoff(T0) is None

    def test_retention_below_floor_is_raised_to_floor(self):
        policy = RetentionPolicy.from_env({"AGENTS_RUN_HISTORY_RETENTION_DAYS": "1"})
        assert policy.retention_days == RETENTION_FLOOR_DAYS == 7
        assert RetentionPolicy.from_values(retention_days=6).retention_days == 7
        assert policy.to_dict()["retention_floor_days"] == 7

    def test_blank_values_mean_unset(self):
        policy = RetentionPolicy.from_env(
            {
                "AGENTS_RUN_HISTORY_RETENTION_DAYS": "",
                "AGENTS_RUN_HISTORY_EXPORT_MAX_DAYS": "  ",
            }
        )
        assert policy == RetentionPolicy()

    @pytest.mark.parametrize(
        "field",
        [
            "retention_days",
            "redact_after_days",
            "export_max_days",
            "sweep_interval_hours",
        ],
    )
    def test_from_values_rejects_zero(self, field):
        with pytest.raises(ValueError):
            RetentionPolicy.from_values(**{field: 0})

    def test_redact_only_policy_sweeps(self):
        assert RetentionPolicy.from_values(redact_after_days=10).sweeps

    def test_policy_reports_effective_export_window(self):
        assert (
            RetentionPolicy.from_values(retention_days=30).to_dict()["export_max_days"]
            == 30
        )

    def test_redact_and_export_are_clamped_to_retention(self):
        policy = RetentionPolicy.from_env(
            {
                "AGENTS_RUN_HISTORY_RETENTION_DAYS": "30",
                "AGENTS_RUN_HISTORY_REDACT_AFTER_DAYS": "90",
                "AGENTS_RUN_HISTORY_EXPORT_MAX_DAYS": "365",
            }
        )
        assert policy.retention_days == 30
        assert policy.redact_after_days == 30
        assert policy.export_max_days == 30

    def test_read_window_defaults_to_retention(self):
        policy = RetentionPolicy.from_values(retention_days=30)
        assert policy.read_cutoff(T0) == T0 - timedelta(days=30)
        narrower = RetentionPolicy.from_values(retention_days=30, export_max_days=10)
        assert narrower.read_cutoff(T0) == T0 - timedelta(days=10)

    @pytest.mark.parametrize("value", ["0", "-3", "abc", "1.5"])
    @pytest.mark.parametrize(
        "name",
        [
            "AGENTS_RUN_HISTORY_RETENTION_DAYS",
            "AGENTS_RUN_HISTORY_REDACT_AFTER_DAYS",
            "AGENTS_RUN_HISTORY_EXPORT_MAX_DAYS",
            "AGENTS_RUN_HISTORY_SWEEP_INTERVAL_HOURS",
        ],
    )
    def test_invalid_value_raises_naming_the_variable(self, name, value):
        with pytest.raises(ValueError, match=name):
            RetentionPolicy.from_env({name: value})

    @pytest.mark.parametrize(
        "name,value",
        [
            ("AGENTS_RUN_HISTORY_RETENTION_DAYS", "36501"),
            ("AGENTS_RUN_HISTORY_REDACT_AFTER_DAYS", "1000000"),
            ("AGENTS_RUN_HISTORY_EXPORT_MAX_DAYS", "1000000"),
            ("AGENTS_RUN_HISTORY_SWEEP_INTERVAL_HOURS", "3000000"),
        ],
    )
    def test_out_of_range_value_fails_at_load(self, name, value):
        with pytest.raises(ValueError, match=name):
            RetentionPolicy.from_env({name: value})

    def test_sweep_interval_ceiling_is_ten_years(self):
        assert RetentionPolicy.from_values(sweep_interval_hours=87_600)
        with pytest.raises(ValueError):
            RetentionPolicy.from_values(sweep_interval_hours=87_601)

    def test_largest_accepted_retention_keeps_read_api_working(self):
        policy = RetentionPolicy.from_env(
            {"AGENTS_RUN_HISTORY_RETENTION_DAYS": "36500"}
        )
        assert policy.read_cutoff() is not None

    def test_invalid_config_fails_settings_load(self, monkeypatch):
        monkeypatch.setenv("AGENTS_RUN_HISTORY_RETENTION_DAYS", "forever")
        with pytest.raises(ValueError):
            AgentsSettings.from_env()


# -- sweep -------------------------------------------------------------------


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    s = _store(request.param, tmp_path)
    yield s
    if hasattr(s, "close"):
        s.close()


class TestSweep:
    def test_unset_policy_removes_and_redacts_nothing(self, store):
        old = _run(store, "old", finished_days_ago=10_000)
        report = sweep(store, RetentionPolicy(), now=T0)
        assert (report.redacted, report.purged) == (0, 0)
        kept = store.get(old.id)
        assert kept.redacted_at is None
        assert kept.result == {"summary": SECRET}

    def test_purges_only_terminal_runs_older_than_retention(self, store):
        policy = RetentionPolicy.from_values(retention_days=30)
        old_ok = _run(store, "old-ok", finished_days_ago=31)
        old_failed = _run(
            store, "old-failed", finished_days_ago=31, state=ExecutionState.DEAD_LETTER
        )
        young = _run(store, "young", finished_days_ago=29)
        running = _run(
            store, "running", finished_days_ago=29, state=ExecutionState.RUNNING
        )
        report = sweep(store, policy, now=T0)
        assert report.purged == 2
        assert store.get(old_ok.id) is None
        assert store.get(old_failed.id) is None
        assert store.get(young.id) is not None
        assert store.get(running.id).state == ExecutionState.RUNNING

    def test_purge_cutoff_is_exactly_retention_days(self, store):
        policy = RetentionPolicy.from_values(retention_days=30)
        inside = _run(store, "inside", finished_days_ago=30 - 1 / 24)
        outside = _run(store, "outside", finished_days_ago=30 + 1 / 24)
        report = sweep(store, policy, now=T0)
        assert report.purge_cutoff == T0 - timedelta(days=30)
        assert store.get(inside.id) is not None
        assert store.get(outside.id) is None

    def test_redact_cutoff_is_exactly_redact_after_days(self, store):
        policy = RetentionPolicy.from_values(redact_after_days=10)
        inside = _run(store, "inside", finished_days_ago=10 - 1 / 24)
        outside = _run(store, "outside", finished_days_ago=10 + 1 / 24)
        report = sweep(store, policy, now=T0)
        assert report.redact_cutoff == T0 - timedelta(days=10)
        assert store.get(inside.id).redacted_at is None
        assert store.get(outside.id).redacted_at is not None

    def test_dry_run_after_sweep_reports_nothing_left(self, store):
        policy = RetentionPolicy.from_values(redact_after_days=7)
        _run(store, "a", finished_days_ago=8)
        sweep(store, policy, now=T0)
        report = sweep(store, policy, now=T0, dry_run=True)
        assert report.redacted == 0

    def test_redacts_before_purge_and_keeps_accounting(self, store):
        policy = RetentionPolicy.from_values(retention_days=30, redact_after_days=7)
        mid = _run(store, "mid", finished_days_ago=8)
        fresh = _run(store, "fresh", finished_days_ago=6)
        report = sweep(store, policy, now=T0)
        assert (report.redacted, report.purged) == (1, 0)
        red = store.get(mid.id)
        assert red.redacted_at == T0
        assert SECRET not in json.dumps(red.to_dict())
        assert red.state == ExecutionState.SUCCEEDED
        assert red.correlation_id == "corr"
        assert red.finished_at == mid.finished_at
        assert store.get(fresh.id).redacted_at is None

    def test_redact_only_policy_never_purges(self, store):
        policy = RetentionPolicy.from_values(redact_after_days=7)
        old = _run(store, "old", finished_days_ago=10_000)
        report = sweep(store, policy, now=T0)
        assert (report.redacted, report.purged) == (1, 0)
        assert store.get(old.id).redacted_at is not None

    def test_sweep_is_idempotent(self, store):
        policy = RetentionPolicy.from_values(retention_days=30, redact_after_days=7)
        _run(store, "a", finished_days_ago=8)
        _run(store, "b", finished_days_ago=40)
        sweep(store, policy, now=T0)
        before = [j.to_dict() for j in store.list_jobs()]
        again = sweep(store, policy, now=T0)
        assert (again.redacted, again.purged) == (0, 0)
        assert [j.to_dict() for j in store.list_jobs()] == before

    def test_dry_run_counts_without_changing_anything(self, store):
        policy = RetentionPolicy.from_values(retention_days=30, redact_after_days=7)
        _run(store, "a", finished_days_ago=8)
        _run(store, "b", finished_days_ago=40)
        _run(store, "stuck", finished_days_ago=40, state=ExecutionState.RUNNING)
        before = [j.to_dict() for j in store.list_jobs()]
        report = sweep(store, policy, now=T0, dry_run=True)
        assert (report.abandoned, report.redacted, report.purged) == (1, 3, 2)
        assert [j.to_dict() for j in store.list_jobs()] == before

    @pytest.mark.parametrize("redact_after_days", [None, 7])
    def test_dry_run_counts_match_the_real_sweep(self, store, redact_after_days):
        policy = RetentionPolicy.from_values(
            retention_days=30, redact_after_days=redact_after_days
        )
        _run(store, "a", finished_days_ago=8)
        _run(store, "b", finished_days_ago=40)
        _run(store, "stuck", finished_days_ago=40, state=ExecutionState.RUNNING)
        _run(store, "live", finished_days_ago=1, state=ExecutionState.RUNNING)
        dry = sweep(store, policy, now=T0, dry_run=True)
        real = sweep(store, policy, now=T0)
        assert (dry.abandoned, dry.redacted, dry.purged) == (
            real.abandoned,
            real.redacted,
            real.purged,
        )

    def test_audit_record_carries_counts_but_no_content_or_ids(self, store, caplog):
        policy = RetentionPolicy.from_values(retention_days=30, redact_after_days=7)
        a = _run(store, "a", finished_days_ago=8)
        b = _run(store, "b", finished_days_ago=40)
        with caplog.at_level(
            logging.INFO, logger="backend.agents.run_history_retention.audit"
        ):
            sweep(store, policy, now=T0)
        audit = [
            r.getMessage()
            for r in caplog.records
            if r.name == "backend.agents.run_history_retention.audit"
        ]
        assert len(audit) == 1
        record = json.loads(audit[0].split(" ", 3)[3])
        assert record["abandoned"] == 0
        assert record["redacted"] == 2 and record["purged"] == 1
        for leaked in (SECRET, a.id, b.id, "corr", "n1"):
            assert leaked not in audit[0]


class TestAbandonOrphanedRuns:
    def test_running_past_retention_is_abandoned_redacted_and_purged(self, store):
        policy = RetentionPolicy.from_values(retention_days=30, redact_after_days=7)
        stuck = _run(
            store, "stuck", finished_days_ago=400, state=ExecutionState.RUNNING
        )
        report = sweep(store, policy, now=T0)
        assert (report.abandoned, report.redacted, report.purged) == (1, 1, 1)
        assert store.get(stuck.id) is None
        assert SECRET not in json.dumps([j.to_dict() for j in store.list_jobs()])

    def test_abandoned_run_is_purged_in_the_same_sweep_without_redaction(self, store):
        policy = RetentionPolicy.from_values(retention_days=30)
        stuck = _run(store, "stuck", finished_days_ago=31, state=ExecutionState.RUNNING)
        report = sweep(store, policy, now=T0)
        assert (report.abandoned, report.redacted, report.purged) == (1, 0, 1)
        assert store.get(stuck.id) is None

    def test_running_younger_than_retention_is_never_touched(self, store):
        policy = RetentionPolicy.from_values(retention_days=30, redact_after_days=7)
        inside = _run(
            store,
            "inside",
            finished_days_ago=30 - 1 / 24,
            state=ExecutionState.RUNNING,
        )
        before = store.get(inside.id).to_dict()
        report = sweep(store, policy, now=T0)
        assert (report.abandoned, report.redacted, report.purged) == (0, 0, 0)
        assert store.get(inside.id).to_dict() == before

    def test_dry_run_never_counts_running_younger_than_retention(self, store):
        policy = RetentionPolicy.from_values(retention_days=30, redact_after_days=7)
        mid = _run(store, "mid", finished_days_ago=10, state=ExecutionState.RUNNING)
        before = store.get(mid.id).to_dict()
        report = sweep(store, policy, now=T0, dry_run=True)
        assert (report.abandoned, report.redacted, report.purged) == (0, 0, 0)
        assert store.get(mid.id).to_dict() == before

    def test_abandon_cutoff_is_exactly_retention_days(self, store):
        policy = RetentionPolicy.from_values(retention_days=30)
        edge = store.enqueue(
            ExecutionJob(
                agent_id="a1",
                kind=ExecutionKind.EVENT,
                idempotency_key="edge",
                state=ExecutionState.RUNNING,
                run_at=T0 - timedelta(days=30),
                created_at=T0 - timedelta(days=30),
                updated_at=T0 - timedelta(days=30),
            )
        )
        report = sweep(store, policy, now=T0)
        assert report.abandoned == 0
        assert store.get(edge.id).state == ExecutionState.RUNNING

    @pytest.mark.parametrize(
        "policy",
        [RetentionPolicy(), RetentionPolicy.from_values(redact_after_days=7)],
        ids=["unset", "redact-only"],
    )
    def test_without_retention_nothing_is_abandoned(self, store, policy):
        stuck = _run(
            store, "stuck", finished_days_ago=10_000, state=ExecutionState.RUNNING
        )
        before = store.get(stuck.id).to_dict()
        report = sweep(store, policy, now=T0)
        assert report.abandoned == 0
        assert store.get(stuck.id).to_dict() == before

    def test_queued_run_is_never_abandoned(self, store):
        policy = RetentionPolicy.from_values(retention_days=30)
        queued = store.enqueue(
            ExecutionJob(
                agent_id="a1",
                kind=ExecutionKind.EVENT,
                idempotency_key="queued",
                run_at=T0 - timedelta(days=400),
                created_at=T0 - timedelta(days=400),
                updated_at=T0 - timedelta(days=400),
            )
        )
        assert sweep(store, policy, now=T0).abandoned == 0
        assert store.get(queued.id).state == ExecutionState.PENDING

    def test_abandoned_run_without_purge_reads_as_failed_with_fixed_reason(self):
        # Abandoning alone (no sweep purge yet) leaves a content-free reason.
        store = _store()
        recorder = AgentRunRecorder(store)
        run_id = recorder.record_start(
            "a1",
            "One",
            {"event_type": "node.update", "summary": SECRET},
            now=T0 - timedelta(days=40),
        )
        cutoff = T0 - timedelta(days=30)
        assert store.abandon([run_id], cutoff=cutoff, now=T0) == 1
        run = recorder.get_run(run_id)
        assert run["status"] == "failed"
        assert run["error"] == ABANDONED_REASON
        assert run["finished_at"] == "2025-11-22T12:00:00Z"

    def test_sweep_rechecks_age_at_write_time_against_the_purge_cutoff(self, store):
        """A run that shows life after it was listed is not abandoned."""
        policy = RetentionPolicy.from_values(retention_days=30)
        job = store.enqueue(
            ExecutionJob(
                agent_id="a1",
                kind=ExecutionKind.EVENT,
                idempotency_key="revived",
                run_at=T0 - timedelta(days=60),
                created_at=T0 - timedelta(days=60),
            )
        )
        store.claim_next("w", now=T0 - timedelta(days=60))

        class RenewsAfterListing:
            """Delegates to the store, renewing the lease once listing is done."""

            def __getattr__(self, name):
                return getattr(store, name)

            def list_running_older_than(self, cutoff):
                listed = store.list_running_older_than(cutoff)
                assert [j.id for j in listed] == [job.id]
                assert store.renew_lease(job.id, "w", now=T0 - timedelta(days=1))
                return listed

        report = sweep(RenewsAfterListing(), policy, now=T0)
        assert (report.abandoned, report.redacted, report.purged) == (0, 0, 0)
        assert store.get(job.id).state == ExecutionState.RUNNING

    def test_crashed_recorder_run_ages_out(self, store):
        """The motivating case: a run recorded at start, never finalized."""
        recorder = AgentRunRecorder(store)
        run_id = recorder.record_start(
            "a1",
            "One",
            {"event_type": "node.update", "entity": {"data": SECRET}},
            now=T0 - timedelta(days=31),
        )
        report = sweep(store, RetentionPolicy.from_values(retention_days=30), now=T0)
        assert report.abandoned == 1 and report.purged == 1
        assert store.get(run_id) is None

    def test_sweep_with_abandoned_runs_is_idempotent(self, store):
        policy = RetentionPolicy.from_values(retention_days=30, redact_after_days=7)
        _run(store, "stuck", finished_days_ago=400, state=ExecutionState.RUNNING)
        _run(store, "live", finished_days_ago=1, state=ExecutionState.RUNNING)
        sweep(store, policy, now=T0)
        before = [j.to_dict() for j in store.list_jobs()]
        again = sweep(store, policy, now=T0)
        assert (again.abandoned, again.redacted, again.purged) == (0, 0, 0)
        assert [j.to_dict() for j in store.list_jobs()] == before


class TestRedactJob:
    def _job(self, payload, result):
        return ExecutionJob(
            agent_id="a1",
            kind=ExecutionKind.EVENT,
            idempotency_key="k",
            payload=payload,
            result=result,
        )

    def test_free_text_under_any_result_key_is_dropped(self):
        from backend.agents.execution.models import redact_job

        job = self._job({}, {"handled": True, "turns": 3, "message": SECRET})
        redact_job(job, T0)
        assert job.result == {"handled": True, "turns": 3, "message": None}

    def test_numeric_result_values_are_kept_but_summary_never(self):
        from backend.agents.execution.models import redact_job

        job = self._job({"foo": SECRET}, {"summary": 5, "cost": 1.5, "turns": 2})
        redact_job(job, T0)
        assert job.result == {"summary": None, "cost": 1.5, "turns": 2}
        assert job.payload == {}

    @pytest.mark.parametrize("result", ["free text " + SECRET, [SECRET]])
    def test_non_dict_result_is_dropped(self, result):
        from backend.agents.execution.models import redact_job

        job = self._job({}, result)
        redact_job(job, T0)
        assert job.result is None

    def test_booleans_are_not_kept_as_identifiers(self):
        from backend.agents.execution.models import redact_job

        job = self._job({"event": {"event_type": False, "entity": {"id": True}}}, None)
        redact_job(job, T0)
        assert job.payload["event"] == {
            "event_type": None,
            "entity": {"kind": None, "id": None, "type": None},
        }

    def test_terminal_age_prefers_finished_at_over_updated_at(self):
        job = self._job({}, None)
        job.finished_at = T0 - timedelta(days=40)
        job.updated_at = T0
        assert job.terminal_at == T0 - timedelta(days=40)
        job.finished_at = None
        assert job.terminal_at == T0

    def test_non_scalar_or_long_kept_fields_are_dropped(self):
        from backend.agents.execution.models import redact_job

        job = self._job(
            {
                "agent_name": {"nested": SECRET},
                "event": {
                    "event_type": "node.update",
                    "entity": {"kind": "node", "id": SECRET * 20, "type": [SECRET]},
                },
            },
            None,
        )
        redact_job(job, T0)
        assert SECRET not in json.dumps(job.to_dict())
        assert job.payload["event"]["event_type"] == "node.update"


# -- sweeper -----------------------------------------------------------------


class TestRetentionSweeper:
    def test_failing_store_is_logged_and_flagged_not_raised(self, caplog):
        broken = MagicMock()
        broken.list_running_older_than.side_effect = RuntimeError("db gone")
        sweeper = RetentionSweeper(
            broken, RetentionPolicy.from_values(retention_days=30), clock=lambda: T0
        )
        with caplog.at_level(logging.ERROR):
            report = sweeper.run_once()
        assert "retention sweep failed" in caplog.text
        assert report is not None and report.run_history_failed is True
        assert sweeper.last_report is report

    def test_start_sweeps_immediately_and_stop_joins(self):
        store = _store()
        old = _run(store, "old", finished_days_ago=40)
        sweeper = RetentionSweeper(
            store, RetentionPolicy.from_values(retention_days=30), clock=lambda: T0
        )
        sweeper.start()
        sweeper.stop()
        assert store.get(old.id) is None
        assert sweeper.last_report.purged == 1

    def test_loop_waits_the_interval_in_seconds_between_sweeps(self):
        sweeper = RetentionSweeper(
            _store(),
            RetentionPolicy.from_values(retention_days=30, sweep_interval_hours=2),
            clock=lambda: T0,
        )
        waits = []

        def fake_wait(seconds):
            waits.append(seconds)
            if len(waits) == 3:
                sweeper._stop.set()
            return sweeper._stop.is_set()

        sweeper._stop.wait = fake_wait
        sweeper._loop()
        assert waits == [7200, 7200, 7200]

    def test_unset_policy_starts_no_thread(self):
        sweeper = RetentionSweeper(MagicMock(), RetentionPolicy())
        sweeper.start()
        assert sweeper._thread is None


# -- CLI ---------------------------------------------------------------------


class TestCli:
    def _db(self, tmp_path):
        store = SqliteExecutionStore(tmp_path / "runs.db")
        job = _run(store, "old", finished_days_ago=4000)
        store.close()
        return tmp_path / "runs.db", job.id

    def test_dry_run_leaves_database_untouched(self, tmp_path, monkeypatch, capsys):
        db, job_id = self._db(tmp_path)
        store = SqliteExecutionStore(db)
        stuck = _run(
            store, "stuck", finished_days_ago=4000, state=ExecutionState.RUNNING
        )
        store.close()
        monkeypatch.setenv("AGENTS_RUN_HISTORY_DB", str(db))
        monkeypatch.setenv("AGENTS_RUN_HISTORY_RETENTION_DAYS", "30")
        assert main(["--dry-run"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert (out["abandoned"], out["purged"]) == (1, 2)
        store = SqliteExecutionStore(db)
        try:
            assert store.get(job_id) is not None
            assert store.get(stuck.id).state == ExecutionState.RUNNING
        finally:
            store.close()

    def test_sweep_purges(self, tmp_path, monkeypatch):
        db, job_id = self._db(tmp_path)
        monkeypatch.setenv("AGENTS_RUN_HISTORY_DB", str(db))
        monkeypatch.setenv("AGENTS_RUN_HISTORY_RETENTION_DAYS", "30")
        assert main([]) == 0
        store = SqliteExecutionStore(db)
        try:
            assert store.get(job_id) is None
        finally:
            store.close()

    def test_invalid_config_and_missing_db_exit_nonzero(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AGENTS_RUN_HISTORY_DB", str(tmp_path / "absent.db"))
        monkeypatch.setenv("AGENTS_RUN_HISTORY_RETENTION_DAYS", "30")
        assert main([]) == 2
        assert not (tmp_path / "absent.db").exists()
        monkeypatch.setenv("AGENTS_RUN_HISTORY_RETENTION_DAYS", "x")
        assert main([]) == 2


# -- registry read window ----------------------------------------------------


class TestRegistryReadWindow:
    def _registry(self, store, policy):
        from backend.agents.registry import AgentRegistry

        return AgentRegistry(
            settings=AgentsSettings(enabled=False, run_history_retention=policy),
            graph_storage=MagicMock(),
            graph_service=MagicMock(),
            execution_store=store,
        )

    def test_runs_outside_export_window_are_not_readable(self):
        from backend.agents.execution.models import utcnow

        store = _store()
        now = utcnow()
        old = store.enqueue(
            ExecutionJob(
                agent_id="a1",
                kind=ExecutionKind.EVENT,
                idempotency_key="old",
                created_at=now - timedelta(days=11),
            )
        )
        new = store.enqueue(
            ExecutionJob(
                agent_id="a1",
                kind=ExecutionKind.EVENT,
                idempotency_key="new",
                created_at=now - timedelta(days=1),
            )
        )
        registry = self._registry(
            store, RetentionPolicy.from_values(retention_days=30, export_max_days=10)
        )
        assert [r["id"] for r in registry.list_runs()] == [new.id]
        assert registry.get_run(old.id) is None
        assert registry.get_run(new.id)["id"] == new.id

    def test_run_created_exactly_at_cutoff_is_outside_the_window(self):
        from backend.agents.run_history import AgentRunRecorder

        store = _store()
        job = store.enqueue(
            ExecutionJob(
                agent_id="a1",
                kind=ExecutionKind.EVENT,
                idempotency_key="k",
                created_at=T0,
            )
        )
        recorder = AgentRunRecorder(store)
        assert recorder.get_run(job.id, created_after=T0) is None
        assert recorder.get_run(job.id, created_after=T0 - timedelta(seconds=1))

    def test_redacted_run_view_carries_redacted_at(self):
        store = _store()
        old = _run(store, "old", finished_days_ago=10)
        store.redact([old.id], now=T0)
        registry = self._registry(store, RetentionPolicy())
        assert registry.get_run(old.id)["redacted_at"] == "2026-01-01T12:00:00Z"

    def test_unset_policy_reads_everything(self):
        store = _store()
        old = _run(store, "old", finished_days_ago=10_000)
        registry = self._registry(store, RetentionPolicy())
        assert [r["id"] for r in registry.list_runs()] == [old.id]
        assert registry.get_run_retention_policy()["sweeps"] is False

    def test_registry_start_sweeps_even_when_agents_disabled(self):
        store = _store()
        old = _run(store, "old", finished_days_ago=4000)
        registry = self._registry(store, RetentionPolicy.from_values(retention_days=30))
        registry.start()
        registry.stop()
        assert registry._retention_sweeper._thread is None
        assert store.get(old.id) is None
        last = registry.get_run_retention_policy()["last_sweep"]
        assert last["purged"] == 1
