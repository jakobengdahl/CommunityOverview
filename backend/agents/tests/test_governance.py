"""
Tests for basic agent governance: autonomy levels, the tool gate, the durable
proposal store, and the approve/reject/apply manager, plus worker wiring.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from backend.agents.config import AgentConfig, AgentPrompts, AgentsSettings
from backend.agents.governance import (
    AutonomyGate,
    AutonomyLevel,
    GovernanceManager,
    InMemoryProposalStore,
    Proposal,
    ProposalStatus,
    SqliteProposalStore,
    coerce_autonomy,
)
from backend.agents.worker import AgentWorker, EventItem


class FakeLoader:
    """Minimal MCP-loader stand-in: original_name is the last dotted segment."""

    def get_tool(self, namespaced_name):
        return SimpleNamespace(original_name=namespaced_name.split(".")[-1])


def _payload(event_id="evt-1"):
    return {
        "event_id": event_id,
        "event_type": "node.create",
        "origin": {"event_correlation_id": "corr-1"},
    }


# -- models -----------------------------------------------------------------


class TestModels:
    def test_read_only_and_apply_properties(self):
        assert AutonomyLevel.OBSERVE.is_read_only
        assert AutonomyLevel.ASSIST.is_read_only
        assert not AutonomyLevel.PROPOSE.is_read_only
        assert AutonomyLevel.ACT_AFTER_APPROVAL.applies_on_approval
        assert not AutonomyLevel.PROPOSE.applies_on_approval

    def test_coerce_unknown_falls_back_to_default(self):
        assert coerce_autonomy(None) is AutonomyLevel.ACT_AFTER_APPROVAL
        assert coerce_autonomy("bogus") is AutonomyLevel.ACT_AFTER_APPROVAL
        assert coerce_autonomy("observe") is AutonomyLevel.OBSERVE

    def test_proposal_roundtrip(self):
        p = Proposal(
            agent_id="a1",
            tool="graph.add_nodes",
            input_args={"nodes": [1]},
            autonomy_level=AutonomyLevel.PROPOSE,
            agent_name="A",
        )
        restored = Proposal.from_dict(p.to_dict())
        assert restored.id == p.id
        assert restored.autonomy_level is AutonomyLevel.PROPOSE
        assert restored.input_args == {"nodes": [1]}


# -- store ------------------------------------------------------------------


class TestStores:
    def test_in_memory_crud_and_ordering(self):
        store = InMemoryProposalStore()
        a = store.create(Proposal("a1", "graph.add_nodes", {}, AutonomyLevel.PROPOSE))
        b = store.create(Proposal("a2", "graph.update_node", {}, AutonomyLevel.PROPOSE))
        assert len(store.list_proposals()) == 2
        assert len(store.list_proposals(agent_id="a1")) == 1
        # newest first
        assert store.list_proposals()[0].id == b.id
        a.status = ProposalStatus.REJECTED
        store.save(a)
        assert store.get(a.id).status is ProposalStatus.REJECTED
        assert len(store.list_proposals(statuses=[ProposalStatus.REJECTED])) == 1

    def test_decide_is_atomic_compare_and_set(self):
        store = InMemoryProposalStore()
        p = store.create(
            Proposal("a1", "graph.add_nodes", {}, AutonomyLevel.ACT_AFTER_APPROVAL)
        )
        first = store.decide(p.id, ProposalStatus.APPROVED, decided_by="jakob")
        assert first is not None and first.status is ProposalStatus.APPROVED
        # A second decide on a no-longer-pending proposal loses the race.
        assert store.decide(p.id, ProposalStatus.REJECTED) is None
        assert store.decide("missing", ProposalStatus.APPROVED) is None

    def test_sqlite_decide_guards_on_pending(self, tmp_path):
        store = SqliteProposalStore(tmp_path / "gov.db")
        try:
            p = store.create(
                Proposal("a1", "graph.add_nodes", {}, AutonomyLevel.PROPOSE)
            )
            assert store.decide(p.id, ProposalStatus.APPROVED) is not None
            assert store.decide(p.id, ProposalStatus.REJECTED) is None
            assert store.get(p.id).status is ProposalStatus.APPROVED
        finally:
            store.close()

    def test_sqlite_persists_across_reopen(self, tmp_path):
        db = tmp_path / "gov.db"
        store = SqliteProposalStore(db)
        p = store.create(
            Proposal("a1", "graph.add_nodes", {"nodes": [1]}, AutonomyLevel.PROPOSE)
        )
        p.status = ProposalStatus.APPROVED
        store.save(p)
        store.close()

        reopened = SqliteProposalStore(db)
        try:
            got = reopened.get(p.id)
            assert got is not None
            assert got.status is ProposalStatus.APPROVED
            assert got.input_args == {"nodes": [1]}
        finally:
            reopened.close()


# -- gate -------------------------------------------------------------------


class TestGate:
    def _gate(self, level, store, allowlist=None):
        return AutonomyGate(
            autonomy_level=level,
            tool_allowlist=allowlist,
            mcp_loader=FakeLoader(),
            proposal_store=store,
            agent_id="a1",
            agent_name="A",
            run_id="run-1",
        )

    def test_read_tool_executes(self):
        store = InMemoryProposalStore()
        raw = MagicMock(return_value={"ok": True})
        gov = self._gate(AutonomyLevel.ACT_AFTER_APPROVAL, store).wrap(raw)
        assert gov("graph.search_graph", {"q": "x"}) == {"ok": True}
        raw.assert_called_once()
        assert store.list_proposals() == []

    def test_allowlist_blocks_unlisted_tool(self):
        store = InMemoryProposalStore()
        raw = MagicMock(return_value={"ok": True})
        gov = self._gate(
            AutonomyLevel.ACT_AFTER_APPROVAL, store, allowlist=["graph.search_graph"]
        ).wrap(raw)
        result = gov("graph.add_nodes", {"nodes": []})
        assert "error" in result
        raw.assert_not_called()
        assert store.list_proposals() == []

    def test_read_only_level_blocks_mutation(self):
        store = InMemoryProposalStore()
        raw = MagicMock()
        gov = self._gate(AutonomyLevel.OBSERVE, store).wrap(raw)
        result = gov("graph.add_nodes", {"nodes": []})
        assert "error" in result
        raw.assert_not_called()
        assert store.list_proposals() == []

    def test_mutation_becomes_proposal_not_executed(self):
        store = InMemoryProposalStore()
        raw = MagicMock()
        gov = self._gate(AutonomyLevel.ACT_AFTER_APPROVAL, store).wrap(raw)
        result = gov("graph.add_nodes", {"nodes": [1]})
        assert result["status"] == "proposed"
        raw.assert_not_called()
        proposals = store.list_proposals()
        assert len(proposals) == 1
        assert proposals[0].tool == "graph.add_nodes"
        assert proposals[0].input_args == {"nodes": [1]}
        assert proposals[0].run_id == "run-1"


# -- manager (approve / reject / apply) -------------------------------------


class TestManager:
    def _proposal(self, store, level):
        return store.create(
            Proposal("a1", "graph.add_nodes", {"nodes": [1]}, level, agent_name="A")
        )

    def test_reject_marks_rejected(self):
        store = InMemoryProposalStore()
        mgr = GovernanceManager(store)
        p = self._proposal(store, AutonomyLevel.ACT_AFTER_APPROVAL)
        out = mgr.reject(p.id, decided_by="jakob")
        assert out["status"] == "rejected"
        assert out["decided_by"] == "jakob"

    def test_approve_propose_does_not_apply(self):
        store = InMemoryProposalStore()
        executor = MagicMock()
        mgr = GovernanceManager(store, executor_factory=lambda aid: executor)
        p = self._proposal(store, AutonomyLevel.PROPOSE)
        out = mgr.approve(p.id)
        assert out["status"] == "approved"
        executor.assert_not_called()

    def test_approve_act_after_approval_applies(self):
        store = InMemoryProposalStore()
        applied = MagicMock(return_value={"added_node_ids": ["n1"]})
        mgr = GovernanceManager(store, executor_factory=lambda aid: applied)
        p = self._proposal(store, AutonomyLevel.ACT_AFTER_APPROVAL)
        out = mgr.approve(p.id)
        assert out["status"] == "applied"
        assert out["apply_result"] == {"added_node_ids": ["n1"]}
        applied.assert_called_once_with("graph.add_nodes", {"nodes": [1]})

    def test_apply_error_marks_apply_failed(self):
        store = InMemoryProposalStore()
        failing = MagicMock(return_value={"error": "boom"})
        mgr = GovernanceManager(store, executor_factory=lambda aid: failing)
        p = self._proposal(store, AutonomyLevel.ACT_AFTER_APPROVAL)
        out = mgr.approve(p.id)
        assert out["status"] == "apply_failed"
        assert out["apply_error"] == "boom"

    def test_apply_exception_marks_apply_failed(self):
        store = InMemoryProposalStore()

        def raiser(aid):
            def _ex(tool, args):
                raise RuntimeError("kaboom")

            return _ex

        mgr = GovernanceManager(store, executor_factory=raiser)
        p = self._proposal(store, AutonomyLevel.ACT_AFTER_APPROVAL)
        out = mgr.approve(p.id)
        assert out["status"] == "apply_failed"
        assert "kaboom" in out["apply_error"]

    def test_decisions_are_sticky(self):
        store = InMemoryProposalStore()
        applied = MagicMock(return_value={"ok": True})
        mgr = GovernanceManager(store, executor_factory=lambda aid: applied)
        p = self._proposal(store, AutonomyLevel.ACT_AFTER_APPROVAL)
        mgr.approve(p.id)
        # A second approve is a no-op and does not re-apply.
        again = mgr.approve(p.id)
        assert again["status"] == "applied"
        assert applied.call_count == 1

    def test_unknown_proposal_returns_none(self):
        mgr = GovernanceManager(InMemoryProposalStore())
        assert mgr.approve("nope") is None
        assert mgr.reject("nope") is None
        assert mgr.get_proposal("nope") is None

    def test_unknown_status_filter_returns_empty(self):
        store = InMemoryProposalStore()
        mgr = GovernanceManager(store)
        self._proposal(store, AutonomyLevel.PROPOSE)
        assert mgr.list_proposals(status="bogus") == []
        assert len(mgr.list_proposals(status="pending")) == 1


# -- worker wiring ----------------------------------------------------------


class TestWorkerAppliesGate:
    def _worker(self, store, autonomy):
        config = AgentConfig(
            agent_id="agent-001",
            name="Test Agent",
            enabled=True,
            prompts=AgentPrompts(task_prompt="Do things."),
            mcp_integration_ids=[],
            autonomy_level=autonomy,
        )
        worker = AgentWorker(
            config=config,
            settings=AgentsSettings(enabled=True, openai_api_key="k"),
            mcp_loader=MagicMock(),
            graph_service=MagicMock(),
            proposal_store=store,
        )
        # Raw executor the gate wraps; must never run for a gated mutation.
        raw = MagicMock(return_value={"added_node_ids": ["n1"]})
        worker.mcp_loader.create_tool_executor.return_value = raw
        worker.mcp_loader.get_tool_definitions.return_value = []
        worker.mcp_loader.get_tool.return_value = SimpleNamespace(
            original_name="add_nodes"
        )
        worker._cached_skills = []
        worker._llm_client = MagicMock()
        return worker, raw

    def test_mutating_tool_call_becomes_proposal(self):
        store = InMemoryProposalStore()
        worker, raw = self._worker(store, "act_after_approval")

        captured = {}

        def fake_execute(**kwargs):
            captured["result"] = kwargs["tool_executor"](
                "graph.add_nodes", {"nodes": [1]}
            )
            return {"success": True, "final_response": "ok", "trace": [], "turns": 1}

        worker._llm_client.execute_with_tools.side_effect = fake_execute
        worker._process_event(EventItem(event_id="evt-1", payload=_payload()))

        assert captured["result"]["status"] == "proposed"
        raw.assert_not_called()  # the mutation was gated, never executed
        assert len(store.list_proposals(agent_id="agent-001")) == 1
