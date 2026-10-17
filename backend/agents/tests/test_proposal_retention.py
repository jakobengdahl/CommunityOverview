"""Tests for the run-history retention sweep applied to governance proposals."""

import json
import logging
from datetime import timedelta
from unittest.mock import MagicMock

import pytest

from backend.agents.config import AgentsSettings
from backend.agents.execution import InMemoryExecutionStore
from backend.agents.execution.contract import T0
from backend.agents.governance import (
    InMemoryProposalStore,
    Proposal,
    ProposalStatus,
    SqliteProposalStore,
)
from backend.agents.run_history_retention import (
    RETENTION_FLOOR_DAYS,
    RetentionPolicy,
    RetentionSweeper,
    main,
    sweep,
)

SECRET = "secret-proposal-content"
POLICY = RetentionPolicy.from_values(retention_days=30, redact_after_days=7)


@pytest.fixture(params=["memory", "sqlite"])
def proposals(request, tmp_path):
    if request.param == "sqlite":
        store = SqliteProposalStore(tmp_path / "governance.db")
        yield store
        store.close()
    else:
        yield InMemoryProposalStore()


def _proposal(
    store,
    *,
    decided_days_ago=None,
    created_days_ago=None,
    status=ProposalStatus.APPLIED,
):
    """
    A proposal with content in every field retention strips. Pending when
    ``decided_days_ago`` is None; otherwise decided that many days before T0.
    """
    created = T0 - timedelta(
        days=created_days_ago
        if created_days_ago is not None
        else (decided_days_ago or 0) + 1
    )
    p = Proposal(
        agent_id="a1",
        agent_name="One",
        tool="graph__update_node",
        input_args={"node_id": "n1", "description": SECRET},
        autonomy_level="act_after_approval",
        created_at=created,
        updated_at=created,
    )
    p.correlation_id = "corr"
    if decided_days_ago is not None:
        decided = T0 - timedelta(days=decided_days_ago)
        p.status = status
        p.decided_by = "reviewer"
        p.decided_at = decided
        p.updated_at = decided
        p.apply_result = {"summary": SECRET}
        p.apply_error = SECRET
    return store.create(p)


def _assert_unredacted(p):
    assert p.input_args["description"] == SECRET
    assert p.apply_result == {"summary": SECRET} and p.apply_error == SECRET


class TestPendingNeverTouched:
    def test_pending_older_than_retention_is_neither_redacted_nor_purged(
        self, proposals
    ):
        pending = _proposal(proposals, created_days_ago=4000)
        before = pending.to_dict()
        report = sweep(None, POLICY, now=T0, proposal_store=proposals)
        assert (report.proposals_redacted, report.proposals_purged) == (0, 0)
        assert proposals.get(pending.id).to_dict() == before

    def test_store_guard_refuses_pending_ids_passed_directly(self, proposals):
        pending = _proposal(proposals, created_days_ago=4000)
        before = pending.to_dict()
        assert proposals.redact([pending.id]) == 0
        assert proposals.purge([pending.id]) == 0
        assert proposals.get(pending.id).to_dict() == before

    def test_pending_is_never_listed_as_decided(self, proposals):
        _proposal(proposals, created_days_ago=4000)
        assert proposals.list_decided_older_than(T0) == []


class TestRedactThenPurge:
    @pytest.mark.parametrize(
        "status",
        [
            ProposalStatus.APPROVED,
            ProposalStatus.REJECTED,
            ProposalStatus.APPLIED,
            ProposalStatus.APPLY_FAILED,
        ],
    )
    def test_decided_past_redact_window_loses_content_keeps_accounting(
        self, proposals, status
    ):
        p = _proposal(proposals, decided_days_ago=10, status=status)
        report = sweep(None, POLICY, now=T0, proposal_store=proposals)
        assert (report.proposals_redacted, report.proposals_purged) == (1, 0)
        got = proposals.get(p.id)
        assert got.input_args == {}
        assert got.apply_result is None and got.apply_error is None
        assert SECRET not in json.dumps(got.to_dict())
        assert (got.status, got.agent_id, got.tool, got.decided_by) == (
            status,
            "a1",
            "graph__update_node",
            "reviewer",
        )
        assert (got.decided_at, got.correlation_id) == (p.decided_at, "corr")

    def test_decided_within_redact_window_is_untouched(self, proposals):
        p = _proposal(proposals, decided_days_ago=6)
        sweep(None, POLICY, now=T0, proposal_store=proposals)
        _assert_unredacted(proposals.get(p.id))

    def test_decided_past_retention_is_purged(self, proposals):
        old = _proposal(proposals, decided_days_ago=31)
        young = _proposal(proposals, decided_days_ago=29)
        report = sweep(None, POLICY, now=T0, proposal_store=proposals)
        assert proposals.get(old.id) is None
        assert proposals.get(young.id) is not None
        assert (report.proposals_redacted, report.proposals_purged) == (2, 1)

    def test_age_is_measured_from_the_decision_not_creation(self, proposals):
        p = _proposal(proposals, created_days_ago=4000, decided_days_ago=1)
        sweep(None, POLICY, now=T0, proposal_store=proposals)
        _assert_unredacted(proposals.get(p.id))

    def test_redact_only_policy_never_purges(self, proposals):
        policy = RetentionPolicy.from_values(redact_after_days=7)
        p = _proposal(proposals, decided_days_ago=4000)
        report = sweep(None, policy, now=T0, proposal_store=proposals)
        assert (report.proposals_redacted, report.proposals_purged) == (1, 0)
        assert proposals.get(p.id).input_args == {}

    def test_unset_policy_touches_nothing(self, proposals):
        p = _proposal(proposals, decided_days_ago=4000)
        report = sweep(None, RetentionPolicy(), now=T0, proposal_store=proposals)
        assert (report.proposals_redacted, report.proposals_purged) == (0, 0)
        _assert_unredacted(proposals.get(p.id))


class TestFloor:
    def test_retention_below_floor_never_purges_younger_than_floor(self, proposals):
        policy = RetentionPolicy.from_values(retention_days=1)
        inside = _proposal(proposals, decided_days_ago=RETENTION_FLOOR_DAYS - 1)
        outside = _proposal(proposals, decided_days_ago=RETENTION_FLOOR_DAYS + 1)
        sweep(None, policy, now=T0, proposal_store=proposals)
        assert proposals.get(inside.id) is not None
        assert proposals.get(outside.id) is None


class TestIdempotence:
    def test_second_sweep_changes_nothing(self, proposals):
        _proposal(proposals, decided_days_ago=10)
        _proposal(proposals, decided_days_ago=40)
        pending = _proposal(proposals, created_days_ago=4000)
        sweep(None, POLICY, now=T0, proposal_store=proposals)
        after = [p.to_dict() for p in proposals.list_proposals()]
        again = sweep(None, POLICY, now=T0, proposal_store=proposals)
        assert (again.proposals_redacted, again.proposals_purged) == (0, 0)
        assert [p.to_dict() for p in proposals.list_proposals()] == after
        assert proposals.get(pending.id) is not None

    def test_dry_run_counts_match_real_sweep_and_change_nothing(self, proposals):
        _proposal(proposals, decided_days_ago=10)
        _proposal(proposals, decided_days_ago=40)
        _proposal(proposals, created_days_ago=4000)
        before = [p.to_dict() for p in proposals.list_proposals()]
        dry = sweep(None, POLICY, now=T0, dry_run=True, proposal_store=proposals)
        assert [p.to_dict() for p in proposals.list_proposals()] == before
        real = sweep(None, POLICY, now=T0, proposal_store=proposals)
        assert (
            (dry.proposals_redacted, dry.proposals_purged)
            == (
                real.proposals_redacted,
                real.proposals_purged,
            )
            == (2, 1)
        )


class TestAuditAndWiring:
    def test_audit_record_carries_counts_but_no_content_or_ids(self, proposals, caplog):
        p = _proposal(proposals, decided_days_ago=40)
        with caplog.at_level(
            logging.INFO, logger="backend.agents.run_history_retention.audit"
        ):
            sweep(None, POLICY, now=T0, proposal_store=proposals)
        audit = [
            r.getMessage()
            for r in caplog.records
            if r.name == "backend.agents.run_history_retention.audit"
        ]
        assert len(audit) == 1
        record = json.loads(audit[0].split(" ", 3)[3])
        assert (record["proposals_redacted"], record["proposals_purged"]) == (1, 1)
        for leaked in (SECRET, p.id, "corr", "n1", "reviewer"):
            assert leaked not in audit[0]

    def test_registry_sweeper_covers_its_proposal_store(self):
        from backend.agents.registry import AgentRegistry

        proposals = InMemoryProposalStore()
        p = _proposal(proposals, decided_days_ago=40)
        registry = AgentRegistry(
            settings=AgentsSettings(enabled=False, run_history_retention=POLICY),
            graph_storage=MagicMock(),
            graph_service=MagicMock(),
            execution_store=InMemoryExecutionStore(),
            proposal_store=proposals,
        )
        registry._retention_sweeper._clock = lambda: T0
        report = registry._retention_sweeper.run_once()
        assert report.proposals_purged == 1
        assert proposals.get(p.id) is None

    def test_sweeper_without_proposal_store_still_sweeps_runs(self):
        sweeper = RetentionSweeper(InMemoryExecutionStore(), POLICY, clock=lambda: T0)
        report = sweeper.run_once()
        assert (report.proposals_redacted, report.proposals_purged) == (0, 0)


class TestCli:
    def test_sweeps_governance_db_alone(self, tmp_path, monkeypatch, capsys):
        db = tmp_path / "governance.db"
        store = SqliteProposalStore(db)
        old = _proposal(store, decided_days_ago=4000)
        pending = _proposal(store, created_days_ago=4000)
        store.close()
        monkeypatch.delenv("AGENTS_RUN_HISTORY_DB", raising=False)
        monkeypatch.setenv("AGENTS_GOVERNANCE_DB", str(db))
        monkeypatch.setenv("AGENTS_RUN_HISTORY_RETENTION_DAYS", "30")
        assert main(["--dry-run"]) == 0
        assert json.loads(capsys.readouterr().out)["proposals_purged"] == 1
        assert main([]) == 0
        store = SqliteProposalStore(db)
        try:
            assert store.get(old.id) is None
            assert store.get(pending.id) is not None
        finally:
            store.close()

    def test_missing_governance_db_exits_nonzero(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AGENTS_RUN_HISTORY_DB", raising=False)
        monkeypatch.setenv("AGENTS_GOVERNANCE_DB", str(tmp_path / "absent.db"))
        monkeypatch.setenv("AGENTS_RUN_HISTORY_RETENTION_DAYS", "30")
        assert main([]) == 2
        assert not (tmp_path / "absent.db").exists()

    def test_no_database_set_exits_nonzero(self, monkeypatch):
        monkeypatch.delenv("AGENTS_RUN_HISTORY_DB", raising=False)
        monkeypatch.delenv("AGENTS_GOVERNANCE_DB", raising=False)
        monkeypatch.setenv("AGENTS_RUN_HISTORY_RETENTION_DAYS", "30")
        assert main([]) == 2
