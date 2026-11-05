# covers: backend/agents/governance/models.py, backend/agents/governance/store.py, backend/agents/governance/gate.py, backend/agents/governance/manager.py, backend/agents/worker.py
"""
Tests for basic agent governance: autonomy levels, the tool gate, the durable
proposal store, and the approve/reject/apply manager, plus worker wiring.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.runtime.attachment_access import ATTACHMENT_TOOLS, ATTACHMENT_READ_TOOLS
from backend.runtime.authorization import GraphAuthorizationDecision

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
    filter_tool_definitions,
)
from backend.agents.tests._sql_recording import RecordingConn, only_statement
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

    def test_sqlite_list_proposals_matches_quote_bearing_agent_id_literally(
        self, tmp_path
    ):
        store = SqliteProposalStore(tmp_path / "gov.db")
        try:
            hostile = "a1' OR '1'='1"
            target = store.create(
                Proposal(hostile, "graph.add_nodes", {}, AutonomyLevel.PROPOSE)
            )
            store.create(Proposal("a1", "graph.add_nodes", {}, AutonomyLevel.PROPOSE))
            assert [p.id for p in store.list_proposals(agent_id=hostile)] == [target.id]
            assert store.list_proposals(agent_id="x' OR '1'='1") == []
        finally:
            store.close()

    def test_sqlite_list_proposals_binds_every_filter_value(self, tmp_path):
        # The f-string SQL carries "nosec B608": this pins that only fixed
        # column clauses and "?" reach the text, and every caller value is a
        # bound param.
        store = SqliteProposalStore(tmp_path / "gov.db")
        try:
            hostile = "a1' OR '1'='1"
            recorder = RecordingConn(store._conn)
            store._conn = recorder
            store.list_proposals(
                agent_id=hostile,
                statuses=[ProposalStatus.PENDING, ProposalStatus.APPLY_FAILED],
                limit=7331,
            )
            sql, params = only_statement(recorder, "SELECT * FROM proposals")
            assert params == [hostile, "pending", "apply_failed", 7331]
            assert sql.count("?") == len(params)
            assert "'" not in sql
            assert "7331" not in sql
            for value in ("pending", "apply_failed"):
                assert value not in sql
        finally:
            store.close()


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


# -- definition pre-filter --------------------------------------------------


class TestFilterToolDefinitions:
    """The LLM-facing pre-filter must mirror the gate's static rejections."""

    def _defs(self):
        return [
            {"name": "graph.search_graph", "description": "read"},
            {"name": "graph.get_node_details", "description": "read"},
            {"name": "graph.add_nodes", "description": "mutate"},
            {"name": "graph.update_node", "description": "mutate"},
        ]

    def _names(self, defs):
        return {d["name"] for d in defs}

    def test_read_only_level_drops_mutating_definitions(self):
        out = filter_tool_definitions(
            self._defs(),
            autonomy_level=AutonomyLevel.OBSERVE,
            tool_allowlist=None,
            mcp_loader=FakeLoader(),
        )
        assert self._names(out) == {"graph.search_graph", "graph.get_node_details"}

    def test_allowlist_drops_unlisted_definitions(self):
        out = filter_tool_definitions(
            self._defs(),
            autonomy_level=AutonomyLevel.ACT_AFTER_APPROVAL,
            tool_allowlist=["graph.search_graph", "graph.add_nodes"],
            mcp_loader=FakeLoader(),
        )
        assert self._names(out) == {"graph.search_graph", "graph.add_nodes"}

    def test_writable_level_keeps_mutating_definitions(self):
        out = filter_tool_definitions(
            self._defs(),
            autonomy_level=AutonomyLevel.ACT_AFTER_APPROVAL,
            tool_allowlist=None,
            mcp_loader=FakeLoader(),
        )
        assert self._names(out) == self._names(self._defs())

    def test_allowlist_and_read_only_compose(self):
        # Allowlist keeps add_nodes, but the read-only level still drops it.
        out = filter_tool_definitions(
            self._defs(),
            autonomy_level=AutonomyLevel.ASSIST,
            tool_allowlist=["graph.search_graph", "graph.add_nodes"],
            mcp_loader=FakeLoader(),
        )
        assert self._names(out) == {"graph.search_graph"}


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


class TestWorkerPreFiltersDefinitions:
    """The definitions handed to the LLM exclude tools the gate would reject,
    while the gate remains the authoritative enforcement at execution time."""

    ALL_DEFS = [
        {"name": "graph.search_graph", "description": "read"},
        {"name": "graph.get_node_details", "description": "read"},
        {"name": "graph.add_nodes", "description": "mutate"},
        {"name": "graph.delete_nodes", "description": "mutate"},
    ]

    def _worker(self, store, autonomy, allowlist=None):
        config = AgentConfig(
            agent_id="agent-001",
            name="Test Agent",
            enabled=True,
            prompts=AgentPrompts(task_prompt="Do things."),
            mcp_integration_ids=[],
            autonomy_level=autonomy,
            tool_allowlist=allowlist,
        )
        loader = MagicMock()
        loader.get_tool_definitions.return_value = [dict(d) for d in self.ALL_DEFS]
        # is_mutating_tool consults get_tool().original_name.
        loader.get_tool.side_effect = lambda name: SimpleNamespace(
            original_name=name.split(".")[-1]
        )
        raw = MagicMock(return_value={"added_node_ids": ["n1"]})
        loader.create_tool_executor.return_value = raw
        worker = AgentWorker(
            config=config,
            settings=AgentsSettings(enabled=True, openai_api_key="k"),
            mcp_loader=loader,
            graph_service=MagicMock(),
            proposal_store=store,
        )
        worker._cached_skills = []
        worker._llm_client = MagicMock()
        return worker, raw

    def _run_and_capture(self, worker):
        captured = {}

        def fake_execute(**kwargs):
            captured["tools"] = kwargs["tools"]
            captured["executor"] = kwargs["tool_executor"]
            return {"success": True, "final_response": "ok", "trace": [], "turns": 1}

        worker._llm_client.execute_with_tools.side_effect = fake_execute
        worker._process_event(EventItem(event_id="evt-1", payload=_payload()))
        return captured

    def test_read_only_agent_is_not_shown_mutating_tools(self):
        store = InMemoryProposalStore()
        worker, raw = self._worker(store, "observe")
        captured = self._run_and_capture(worker)

        shown = {d["name"] for d in captured["tools"]}
        assert shown == {"graph.search_graph", "graph.get_node_details"}

        # Enforcement still blocks a mutating call at execution time even though
        # it was filtered from the definitions — the gate is authoritative.
        result = captured["executor"]("graph.add_nodes", {"nodes": [1]})
        assert "error" in result
        raw.assert_not_called()
        assert store.list_proposals() == []

    def test_allowlist_restricted_agent_is_not_shown_unlisted_tools(self):
        store = InMemoryProposalStore()
        worker, raw = self._worker(
            store, "act_after_approval", allowlist=["graph.search_graph"]
        )
        captured = self._run_and_capture(worker)

        shown = {d["name"] for d in captured["tools"]}
        assert shown == {"graph.search_graph"}

        # A disallowed tool is still blocked by the gate if the LLM attempts it.
        result = captured["executor"]("graph.add_nodes", {"nodes": [1]})
        assert "error" in result
        raw.assert_not_called()
        assert store.list_proposals() == []


def test_approved_chain_keeps_server_causation_across_sqlite_restart(tmp_path):
    from backend.core.events.causation import (
        MAX_AGENT_CAUSATION_DEPTH,
        agent_mutation_context,
        current_agent_mutation_context,
    )
    from backend.service.access import build_event_context

    db = tmp_path / "chain.db"
    store = SqliteProposalStore(db)
    observed = []
    for depth in range(MAX_AGENT_CAUSATION_DEPTH):
        agent_id = f"agent-{depth % 2}"
        gate = AutonomyGate(
            autonomy_level=AutonomyLevel.ACT_AFTER_APPROVAL,
            tool_allowlist=None,
            mcp_loader=FakeLoader(),
            proposal_store=store,
            agent_id=agent_id,
        )
        with agent_mutation_context(
            agent_id,
            {
                "event_id": "seed",
                "origin": {
                    "event_causation_depth": depth,
                    "event_correlation_id": "chain",
                },
            },
        ):
            result = gate.wrap(MagicMock())(
                "graph.update_node",
                {
                    "event_causation_depth": 0,
                    "event_origin": "spoofed",
                },
            )
        store.close()
        store = SqliteProposalStore(db)

        def execute(tool, arguments):
            context = build_event_context(target="update_node", event_origin="spoofed")
            observed.append(context)
            return {"success": True}

        manager = GovernanceManager(store, executor_factory=lambda _: execute)
        assert manager.approve(result["proposal_id"])["status"] == "applied"
        assert current_agent_mutation_context() is None
    assert [ctx.event_causation_depth for ctx in observed] == list(range(1, 9))
    assert {ctx.event_correlation_id for ctx in observed} == {"chain"}
    # Corrupt/exhausted persisted proposals also fail closed before any write.
    p = store.create(
        Proposal(
            "agent-0",
            "graph.update_node",
            {},
            AutonomyLevel.ACT_AFTER_APPROVAL,
            causation_depth=MAX_AGENT_CAUSATION_DEPTH + 1,
        )
    )
    assert manager.approve(p.id)["status"] == "apply_failed"
    assert len(observed) == MAX_AGENT_CAUSATION_DEPTH
    store.close()


def test_approval_checks_live_agent_pause_before_writing():
    from backend.agents.registry import AgentRegistry

    registry = AgentRegistry.__new__(AgentRegistry)
    registry._ensure_initialized = MagicMock()
    registry._mcp_loader = MagicMock()
    registry._service = MagicMock()
    worker = AgentWorker(
        AgentConfig(agent_id="a", name="A", enabled=True), AgentsSettings(), None
    )
    registry._workers = {"a": worker}
    worker._pause_reason = "temporarily throttled"
    store = InMemoryProposalStore()
    proposal = store.create(
        Proposal("a", "graph.update_node", {}, AutonomyLevel.ACT_AFTER_APPROVAL)
    )
    manager = GovernanceManager(store, executor_factory=registry._build_apply_executor)
    result = manager.approve(proposal.id)
    assert result["status"] == "apply_failed"
    assert "paused" in result["apply_error"]
    registry._mcp_loader.create_tool_executor.return_value.assert_not_called()


def test_causation_schema_migrates_existing_v2_proposals(tmp_path):
    import sqlite3

    db = tmp_path / "legacy.db"
    store = SqliteProposalStore(db)
    p = store.create(
        Proposal("a", "graph.update_node", {}, AutonomyLevel.ACT_AFTER_APPROVAL)
    )
    store.close()
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE proposals DROP COLUMN causation_depth")
        conn.execute("ALTER TABLE proposals DROP COLUMN tainted")
        conn.execute("PRAGMA user_version=2")
    migrated = SqliteProposalStore(db)
    assert migrated.get(p.id).causation_depth == 1
    assert migrated.get(p.id).tainted is False
    migrated.close()


@pytest.mark.parametrize("name", sorted(ATTACHMENT_TOOLS))
@pytest.mark.parametrize("allowlist", [None, [], "named"])
def test_attachment_grant_defaults_to_deny_even_with_allowlist(name, allowlist):
    """An empty allowlist, or naming a tool, never grants attachment access."""
    name = "GRAPH__" + name
    allowlist = [name] if allowlist == "named" else allowlist
    loader = SimpleNamespace(get_tool=lambda _: None)
    raw = MagicMock()
    gate = AutonomyGate(
        autonomy_level=AutonomyLevel.ACT_AFTER_APPROVAL,
        tool_allowlist=allowlist,
        mcp_loader=loader,
        proposal_store=InMemoryProposalStore(),
        agent_id="a1",
    )
    assert gate.wrap(raw)(name, {})["error_code"] == "access_denied"
    assert (
        filter_tool_definitions(
            [{"name": name}],
            autonomy_level=AutonomyLevel.ACT_AFTER_APPROVAL,
            tool_allowlist=allowlist,
            mcp_loader=loader,
        )
        == []
    )
    raw.assert_not_called()


@pytest.mark.parametrize("level", list(AutonomyLevel))
@pytest.mark.parametrize("access", ["read", "read_write"])
@pytest.mark.parametrize("name", sorted(ATTACHMENT_TOOLS))
def test_attachment_grants_compose_with_autonomy(level, access, name, monkeypatch):
    monkeypatch.setenv("ATTACHMENT_CONTENT_ACCESS", "read_write")
    raw = MagicMock(return_value={"ok": True})
    store = InMemoryProposalStore()
    gate = AutonomyGate(
        autonomy_level=level,
        tool_allowlist=[],
        mcp_loader=FakeLoader(),
        proposal_store=store,
        agent_id="a1",
        attachment_access=access,
    )
    tool = "graph." + name
    result = gate.wrap(raw)(tool, {})
    allowed = name in ATTACHMENT_READ_TOOLS or (
        access == "read_write" and not level.is_read_only
    )
    shown = filter_tool_definitions(
        [{"name": tool}],
        autonomy_level=level,
        tool_allowlist=[],
        mcp_loader=FakeLoader(),
        attachment_access=access,
    )
    assert bool(shown) == allowed
    if name in ATTACHMENT_READ_TOOLS:
        assert result == {"ok": True}
        raw.assert_called_once()
    else:
        raw.assert_not_called()
        assert result.get("status") == ("proposed" if allowed else None)
        assert len(store.list_proposals()) == int(allowed)


@pytest.mark.parametrize("name", sorted(ATTACHMENT_TOOLS))
def test_attachment_grant_does_not_bypass_nonempty_allowlist(name, monkeypatch):
    """A granted attachment tool outside a nonempty allowlist stays blocked."""
    monkeypatch.setenv("ATTACHMENT_CONTENT_ACCESS", "read_write")
    raw = MagicMock()
    store = InMemoryProposalStore()
    allowlist = ["graph.search_graph"]
    gate = AutonomyGate(
        autonomy_level=AutonomyLevel.ACT_AFTER_APPROVAL,
        tool_allowlist=allowlist,
        mcp_loader=FakeLoader(),
        proposal_store=store,
        agent_id="a1",
        attachment_access="read_write",
    )
    assert "allowlist" in gate.wrap(raw)("graph." + name, {})["error"]
    assert (
        filter_tool_definitions(
            [{"name": "graph." + name}],
            autonomy_level=AutonomyLevel.ACT_AFTER_APPROVAL,
            tool_allowlist=allowlist,
            mcp_loader=FakeLoader(),
            attachment_access="read_write",
        )
        == []
    )
    raw.assert_not_called()
    assert store.list_proposals() == []


@pytest.mark.parametrize("name", sorted(ATTACHMENT_TOOLS))
def test_attachment_denying_hook_blocks_execution_and_proposals(name, monkeypatch):
    monkeypatch.setenv("ATTACHMENT_CONTENT_ACCESS", "read_write")
    raw = MagicMock()
    store = InMemoryProposalStore()
    hook = SimpleNamespace(evaluate=lambda _: GraphAuthorizationDecision(allowed=False))
    gate = AutonomyGate(
        autonomy_level=AutonomyLevel.ACT_AFTER_APPROVAL,
        tool_allowlist=None,
        mcp_loader=FakeLoader(),
        proposal_store=store,
        agent_id="a1",
        attachment_access="read_write",
        authorization_hook=hook,
    )
    assert gate.wrap(raw)("graph." + name, {})["error_code"] == "access_denied"
    raw.assert_not_called()
    assert store.list_proposals() == []


@pytest.mark.parametrize(
    "name,target",
    [
        ("list_node_attachments", "list_node_attachments"),
        ("read_node_attachment", "read_node_attachment_content"),
        ("search_node_attachments", "read_node_attachment_content"),
        ("add_node_attachment", "add_node_attachment"),
        ("delete_node_attachment", "delete_node_attachment"),
    ],
)
def test_attachment_target_specific_hook_denial(name, target, monkeypatch, caplog):
    """Metadata listing and content reads have separate authorization targets."""
    monkeypatch.setenv("ATTACHMENT_CONTENT_ACCESS", "read_write")
    seen = []

    def evaluate(context):
        seen.append(context)
        return GraphAuthorizationDecision(allowed=context.target != target)

    raw = MagicMock()
    store = InMemoryProposalStore()
    gate = AutonomyGate(
        autonomy_level=AutonomyLevel.ACT_AFTER_APPROVAL,
        tool_allowlist=None,
        mcp_loader=FakeLoader(),
        proposal_store=store,
        agent_id="a1",
        attachment_access="read_write",
        authorization_hook=SimpleNamespace(evaluate=evaluate),
    )
    with caplog.at_level("INFO", logger="attachments.audit"):
        result = gate.wrap(raw)("graph." + name, {})
    if name in ("read_node_attachment", "search_node_attachments"):
        import json
        import logging

        records = [r for r in caplog.records if r.name == "attachments.audit"]
        assert len(records) == 1
        record = json.loads(logging.Formatter("%(message)s").format(records[0]))
        assert record["actor"] == {"type": "agent", "id": "a1"}
        assert record["tool"] == name
        assert record["outcome"] == "access_denied"
        assert [record[k] for k in ("node_id", "attachment_id", "sha256", "chars")] == [
            None,
            None,
            None,
            0,
        ]
    assert result["error_code"] == "access_denied"
    assert result["authorization"]["target"] == target
    assert seen[0].action == ("read" if name in ATTACHMENT_READ_TOOLS else "mutate")
    raw.assert_not_called()
    assert store.list_proposals() == []


@pytest.mark.parametrize("with_store", [False, True])
def test_worker_checks_live_attachment_grants_with_or_without_proposal_store(
    with_store, monkeypatch
):
    monkeypatch.setenv("ATTACHMENT_CONTENT_ACCESS", "read_write")
    helper = TestWorkerPreFiltersDefinitions()
    worker, raw = helper._worker(
        InMemoryProposalStore() if with_store else None, "act_after_approval"
    )
    node = SimpleNamespace(metadata={"attachment_access": "read_write"})
    worker._graph_storage = SimpleNamespace(get_node=lambda _: node)
    defs = [{"name": "graph." + name} for name in sorted(ATTACHMENT_TOOLS)]
    worker.mcp_loader.get_tool_definitions.return_value = defs
    captured = helper._run_and_capture(worker)
    shown = {d["name"] for d in captured["tools"]}
    assert len(shown) == (5 if with_store else 3)
    if not with_store:
        assert (
            captured["executor"]("graph.add_node_attachment", {})["error_code"]
            == "access_denied"
        )
    node.metadata["attachment_access"] = "none"
    assert (
        captured["executor"]("graph.read_node_attachment", {})["error_code"]
        == "access_denied"
    )
    assert helper._run_and_capture(worker)["tools"] == []
    raw.assert_not_called()


@pytest.mark.parametrize("ceiling", [None, "off", "read"])
@pytest.mark.parametrize("name", sorted(ATTACHMENT_TOOLS))
def test_attachment_agent_grants_respect_server_ceiling(monkeypatch, ceiling, name):
    if ceiling is None:
        monkeypatch.delenv("ATTACHMENT_CONTENT_ACCESS", raising=False)
    else:
        monkeypatch.setenv("ATTACHMENT_CONTENT_ACCESS", ceiling)
    raw = MagicMock(return_value={"ok": True})
    store = InMemoryProposalStore()
    gate = AutonomyGate(
        autonomy_level=AutonomyLevel.ACT_AFTER_APPROVAL,
        tool_allowlist=None,
        mcp_loader=FakeLoader(),
        proposal_store=store,
        agent_id="a1",
        attachment_access="read_write",
    )
    tool = "graph." + name
    shown = filter_tool_definitions(
        [{"name": tool}],
        autonomy_level=AutonomyLevel.ACT_AFTER_APPROVAL,
        tool_allowlist=None,
        mcp_loader=FakeLoader(),
        attachment_access="read_write",
    )
    result = gate.wrap(raw)(tool, {})
    allowed = ceiling == "read" and name in ATTACHMENT_READ_TOOLS
    assert bool(shown) == allowed
    if allowed:
        assert result == {"ok": True}
        raw.assert_called_once_with(tool, {})
    else:
        assert result["error_code"] == "access_denied"
        raw.assert_not_called()
    assert store.list_proposals() == []


@pytest.mark.parametrize("name", ["read_node_attachment", "search_node_attachments"])
def test_attachment_allowlist_refusal_formatted_audit(caplog, name):
    import json
    import logging

    raw = MagicMock()
    gate = AutonomyGate(
        autonomy_level=AutonomyLevel.ACT_AFTER_APPROVAL,
        tool_allowlist=["graph.search_graph"],
        mcp_loader=FakeLoader(),
        proposal_store=None,
        agent_id="a1",
        attachment_access="read",
    )
    secret = "private-query-content-filename.txt"
    with caplog.at_level("INFO", logger="attachments.audit"):
        result = gate.wrap(raw)("graph." + name, {"query": secret})
    assert "allowlist" in result["error"]
    raw.assert_not_called()
    lines = [
        logging.Formatter("%(message)s").format(r)
        for r in caplog.records
        if r.name == "attachments.audit"
    ]
    assert len(lines) == 1
    assert secret not in lines[0]
    record = json.loads(lines[0])
    assert record["actor"] == {"type": "agent", "id": "a1"}
    assert record["tool"] == name
    assert record["outcome"] == "access_denied"
    assert [record[k] for k in ("node_id", "attachment_id", "sha256", "chars")] == [
        None,
        None,
        None,
        0,
    ]


def test_governance_exception_formatted_logs_hide_content(caplog):
    import logging

    secret = "SECRET-proposal-content"
    broken_store = MagicMock()
    broken_store.create.side_effect = RuntimeError(secret)
    gate = AutonomyGate(
        autonomy_level=AutonomyLevel.PROPOSE,
        tool_allowlist=None,
        mcp_loader=FakeLoader(),
        proposal_store=broken_store,
        agent_id="a1",
        agent_name="A",
        run_id="run-1",
    )
    store = InMemoryProposalStore()
    proposal = store.create(
        Proposal("a1", "graph.add_nodes", {}, AutonomyLevel.ACT_AFTER_APPROVAL)
    )
    manager = GovernanceManager(
        store, executor_factory=lambda _: MagicMock(side_effect=ValueError(secret))
    )
    with caplog.at_level("INFO"):
        assert gate.wrap(MagicMock())("graph.add_nodes", {"nodes": [secret]}).get(
            "error"
        )
        assert manager.approve(proposal.id)["status"] == "apply_failed"
    lines = [logging.Formatter("%(message)s").format(r) for r in caplog.records]
    assert all(secret not in line for line in lines)
    assert any(
        "error_code=proposal_record_failed error_class=RuntimeError" in line
        and "raising_frame=" in line
        for line in lines
    )
    assert any(
        "error_code=proposal_apply_failed error_class=ValueError" in line
        and "raising_frame=" in line
        for line in lines
    )


def test_taint_permitted_tools_are_explicit_and_classify_graph():
    """The reviewed list is fixed and every advertised graph tool is classified."""
    from backend.agents.governance.gate import (
        ATTACHMENT_TAINT_PERMITTED_TOOLS as permitted,
        MUTATING_ORIGINAL_TOOLS as mutating,
    )
    from backend.agents.mcp_loader import MCPLoader
    from backend.agents.config import MCPIntegration

    assert permitted == {
        "search_graph",
        "get_related_nodes",
        "find_similar_nodes",
        "find_similar_nodes_batch",
        "list_node_types",
        "get_subtypes",
        "get_schema",
        "get_presentation",
        "list_saved_views",
        "get_saved_view",
        "get_node_details",
        "get_graph_stats",
        "get_capabilities",
        "get_runtime_info",
        "get_tenant_context",
        "get_config_context",
        "get_request_actor",
        "get_request_scope",
        "get_request_selection",
        "list_node_attachments",
        "read_node_attachment",
        "search_node_attachments",
        "propose_new_node",
        "present_form",
        "mark_nodes",
        "clear_visualization",
    }
    loader = MCPLoader([])
    assert {
        t.original_name
        for t in loader._get_graph_mcp_tools(MCPIntegration(id="GRAPH", name="Graph"))
    } <= permitted | mutating


@pytest.mark.parametrize("store_available", [False, True])
@pytest.mark.parametrize("level", list(AutonomyLevel))
def test_run_taint_blocks_egress_and_unknown_graph_tools(store_available, level):
    """Taint sticks to the run and permits only graph reads or governed writes."""
    from backend.runtime.content_logging import content_logging, is_tainted

    loader = SimpleNamespace(
        get_tool=lambda name: SimpleNamespace(
            integration_id=name.split("__")[0], original_name=name.split("__")[-1]
        )
    )
    store = InMemoryProposalStore() if store_available else None
    gate = AutonomyGate(
        autonomy_level=level,
        tool_allowlist=None,
        mcp_loader=loader,
        proposal_store=store,
        agent_id="a1",
    )
    raw = MagicMock(return_value={"untrusted_content": True})
    execute = gate.wrap(raw)
    with content_logging():
        execute("WEB__fetch", {})
        assert is_tainted()
        raw.return_value = {"success": True}
        for name in (
            "WEB__fetch",
            "SEARCH__search",
            "FS__read_file",
            "FUTURE__search_graph",
            "GRAPH__future_read",
            "FS__update_node",
        ):
            assert execute(name, {})["error_code"] == "access_denied"
        assert execute("GRAPH__search_graph", {}) == {"success": True}
        result = execute("GRAPH__update_node", {})
        if store_available and not level.is_read_only:
            assert result["status"] == "proposed"
            assert store.get(result["proposal_id"]).tainted
        else:
            assert "error" in result
    assert raw.call_count == 2
    assert not is_tainted()


@pytest.mark.parametrize("durable", [False, True])
def test_all_run_proposals_keep_taint_through_storage(tmp_path, durable):
    """Proposals before and after a read expose persistent taint to approvers."""
    store = (
        SqliteProposalStore(tmp_path / "taint.db")
        if durable
        else InMemoryProposalStore()
    )
    gate = AutonomyGate(
        autonomy_level=AutonomyLevel.PROPOSE,
        tool_allowlist=None,
        mcp_loader=FakeLoader(),
        proposal_store=store,
        agent_id="a1",
    )
    execute = gate.wrap(lambda *_: {"untrusted_content": True})
    before = execute("graph.update_node", {})["proposal_id"]
    stale = store.get(before)
    assert not stale.tainted
    store.decide(before, ProposalStatus.APPROVED)
    execute("graph.search_graph", {})
    assert store.get(before).status is ProposalStatus.APPROVED
    stale.status = ProposalStatus.APPROVED
    assert store.save(stale).tainted
    if durable:
        store.close()
        store = SqliteProposalStore(tmp_path / "taint.db")
    assert GovernanceManager(store).get_proposal(before)["tainted"] is True
    assert Proposal.from_dict(store.get(before).to_dict()).tainted
    assert not Proposal.from_dict(
        {**store.get(before).to_dict(), "tainted": False}
    ).tainted
    if durable:
        store.close()


@pytest.mark.parametrize("failure", ["returned", "raised", None])
@pytest.mark.parametrize("tainted", [False, True])
def test_proposal_apply_formatted_logs_preserve_boundary(caplog, tainted, failure):
    """Approved actions bind taint across loader call, error and exception logs."""
    import logging
    from backend.agents.config import MCPIntegration
    from backend.agents.mcp_loader import MCPLoader
    from backend.runtime.content_logging import is_tainted

    loader = MCPLoader([])
    loader._tools_cache = {
        t.namespaced_name: t
        for t in loader._get_graph_mcp_tools(MCPIntegration(id="GRAPH", name="Graph"))
    }

    def update_node(**kwargs):
        assert is_tainted() is tainted
        if failure == "raised":
            raise ValueError("SECRET-exception")
        return {"error": "SECRET-error"} if failure else {"success": True}

    store = InMemoryProposalStore()
    p = store.create(
        Proposal(
            "a1",
            "GRAPH__update_node",
            {"description": "SECRET-input"},
            AutonomyLevel.ACT_AFTER_APPROVAL,
            tainted=tainted,
        )
    )
    manager = GovernanceManager(
        store,
        lambda _: loader.create_tool_executor(
            graph_service=SimpleNamespace(update_node=update_node)
        ),
    )
    with caplog.at_level(logging.INFO):
        manager.approve(p.id)
    output = caplog.text
    assert ("SECRET-input" in output) is not tainted
    assert ("SECRET-error" in output) is (failure == "returned" and not tainted)
    assert ("SECRET-exception" in output) is (failure == "raised" and not tainted)
    assert "GRAPH__update_node" in output
    assert not is_tainted()

    def fail_factory(_):
        raise RuntimeError("SECRET-factory")

    p = store.create(
        Proposal(
            "a1",
            "GRAPH__update_node",
            {},
            AutonomyLevel.ACT_AFTER_APPROVAL,
            tainted=tainted,
        )
    )
    GovernanceManager(store, fail_factory).approve(p.id)
    assert p.id in caplog.text and "SECRET-factory" not in caplog.text


@pytest.mark.parametrize("logger_name", ["httpx", "httpx2"])
def test_tainted_http_request_logs_omit_query(caplog, logger_name):
    """Both HTTP clients retain clean diagnostics and redact tainted URL queries."""
    import logging
    from backend.runtime.content_logging import content_logging

    for tainted in (False, True):
        caplog.clear()
        with caplog.at_level(logging.INFO), content_logging(tainted=tainted):
            logging.getLogger(logger_name).info(
                'HTTP Request: %s %s "%s %d %s"',
                "GET",
                "https://example.test/file?SECRET-query",
                "HTTP/1.1",
                200,
                "OK",
            )
        assert ("SECRET-query" in caplog.text) is not tainted
