# covers: backend/agents/governance/gate.py, backend/agents/governance/models.py, backend/agents/governance/store.py, backend/agents/governance/manager.py, backend/agents/mcp_loader.py, backend/runtime/content_logging.py
"""Tests for governance gate taint and proposal approval boundaries."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.agents.governance import (
    AutonomyGate,
    AutonomyLevel,
    GovernanceManager,
    InMemoryProposalStore,
    Proposal,
    ProposalStatus,
    SqliteProposalStore,
)


class FakeLoader:
    """Minimal MCP-loader stand-in: original_name is the last dotted segment."""

    def get_tool(self, namespaced_name):
        return SimpleNamespace(original_name=namespaced_name.split(".")[-1])


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


@pytest.mark.parametrize("durable", [False, True])
def test_proposal_taint_failure_withholds_result_and_blocks_run(
    monkeypatch, tmp_path, durable
):
    store = (
        SqliteProposalStore(tmp_path / "taint-failure.db")
        if durable
        else InMemoryProposalStore()
    )
    gate = AutonomyGate(
        autonomy_level=AutonomyLevel.ACT_AFTER_APPROVAL,
        tool_allowlist=None,
        mcp_loader=FakeLoader(),
        proposal_store=store,
        agent_id="a1",
    )
    calls = []

    def raw_executor(name, args):
        calls.append(name)
        return {"untrusted_content": True, "content": "SECRET-file-content"}

    execute = gate.wrap(raw_executor)
    before = execute("graph.update_node", {})["proposal_id"]

    def fail_mark_tainted(ids):
        assert ids == [before]
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(store, "mark_tainted", fail_mark_tainted)
    result = execute("graph.search_graph", {})
    assert result["error_code"] == "access_denied"
    assert "SECRET-file-content" not in str(result)
    assert "untrusted_content" not in result
    assert not store.get(before).tainted

    def unexpected_create(*args, **kwargs):
        pytest.fail("Blocked run must not create another proposal")

    monkeypatch.setattr(store, "create", unexpected_create)
    for tool in ("graph.update_node", "graph.search_graph", "web.fetch"):
        assert execute(tool, {}) == result
    # The result is withheld and no subsequent execution is allowed.
    assert calls == ["graph.search_graph"]
    if durable:
        store.close()


@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize("outcome", ["clean", "untrusted", "raised"])
def test_proposals_publish_taint_only_after_untrusted_result(
    tmp_path, durable, outcome
):
    db_path = tmp_path / "barrier.db"
    store = SqliteProposalStore(db_path) if durable else InMemoryProposalStore()
    gate = AutonomyGate(
        autonomy_level=AutonomyLevel.PROPOSE,
        tool_allowlist=None,
        mcp_loader=FakeLoader(),
        proposal_store=store,
        agent_id="a1",
    )
    proposal_ids = []

    def raw_executor(*_):
        # An approver must not see speculative taint during a raw execution.
        reader = SqliteProposalStore(db_path) if durable else store
        try:
            for proposal_id in proposal_ids:
                assert not GovernanceManager(reader).get_proposal(proposal_id)[
                    "tainted"
                ]
        finally:
            if durable:
                reader.close()
        if outcome == "raised":
            raise ValueError("clean tool failure")
        return {"untrusted_content": outcome == "untrusted"}

    execute = gate.wrap(raw_executor)
    for _ in range(2):
        proposal_ids.append(execute("graph.update_node", {})["proposal_id"])
    if outcome == "raised":
        with pytest.raises(ValueError, match="clean tool failure"):
            execute("graph.search_graph", {})
    else:
        execute("graph.search_graph", {})
    if durable:
        store.close()
        store = SqliteProposalStore(db_path)
    for proposal_id in proposal_ids:
        assert store.get(proposal_id).tainted is (outcome == "untrusted")
    if durable:
        store.close()


@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize("outcome", ["clean", "raised"])
def test_concurrent_approval_during_clean_execution_preserves_logging_context(
    tmp_path, durable, outcome
):
    from concurrent.futures import ThreadPoolExecutor
    from backend.runtime.content_logging import is_tainted

    db_path = tmp_path / "clean-approval.db"
    store = SqliteProposalStore(db_path) if durable else InMemoryProposalStore()
    gate = AutonomyGate(
        autonomy_level=AutonomyLevel.ACT_AFTER_APPROVAL,
        tool_allowlist=None,
        mcp_loader=FakeLoader(),
        proposal_store=store,
        agent_id="a1",
    )
    applied = []

    def apply(name, args):
        applied.append((name, args, is_tainted()))
        return {"success": True}

    def approve():
        reader = SqliteProposalStore(db_path) if durable else store
        try:
            manager = GovernanceManager(reader, executor_factory=lambda _: apply)
            assert manager.get_proposal(proposal_id)["tainted"] is False
            result = manager.approve(proposal_id)
            assert result["tainted"] is False
            assert result["status"] == "applied"
        finally:
            if durable:
                reader.close()

    def raw_executor(*_):
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(approve).result(timeout=10)
        if outcome == "raised":
            raise ValueError("clean tool failure")
        return {"success": True}

    execute = gate.wrap(raw_executor)
    proposal_id = execute("graph.update_node", {"name": "clean-value"})["proposal_id"]
    if outcome == "raised":
        with pytest.raises(ValueError, match="clean tool failure"):
            execute("graph.search_graph", {})
    else:
        assert execute("graph.search_graph", {}) == {"success": True}
    assert applied == [("graph.update_node", {"name": "clean-value"}, False)]
    assert store.get(proposal_id).tainted is False
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
@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/file?SECRET-query",
        "https://example.test/SECRET-path",
        "https://SECRET-user:SECRET-password@example.test/file",
        "https://example.test/file#SECRET-fragment",
        "https://SECRET-user:SECRET-password@example.test/SECRET-path#SECRET-fragment?query",
    ],
)
def test_tainted_http_request_logs_omit_content_during_approval(
    caplog, logger_name, url
):
    """Only clean approvals retain HTTP diagnostics, across all URL components."""
    import logging

    for tainted in (False, True):
        caplog.clear()
        store = InMemoryProposalStore()
        proposal = store.create(
            Proposal(
                "a1",
                "GRAPH__update_node",
                {"url": url},
                AutonomyLevel.ACT_AFTER_APPROVAL,
                tainted=tainted,
            )
        )

        def execute(*_):
            logging.getLogger(logger_name).info(
                'HTTP Request: %s %s "%s %d %s"',
                "POST",
                url,
                "HTTP/1.1",
                200,
                "SECRET-reason",
            )
            return {"success": True}

        with caplog.at_level(logging.INFO):
            result = GovernanceManager(store, lambda _: execute).approve(proposal.id)
            logging.getLogger(logger_name).info("ordinary diagnostic")
        assert result["status"] == "applied"
        assert (url in caplog.text) is not tainted
        assert ("SECRET-reason" in caplog.text) is not tainted
        assert ("HTTP Request:" in caplog.text) is not tainted
        assert "ordinary diagnostic" in caplog.text


@pytest.mark.parametrize("durable", [False, True])
@pytest.mark.parametrize("publication", ["after_snapshot", "during_execution"])
@pytest.mark.parametrize("failure", ["returned", "raised"])
def test_approval_logging_observes_taint_after_clean_snapshot(
    tmp_path, monkeypatch, caplog, durable, publication, failure
):
    import logging
    from backend.agents.config import MCPIntegration
    from backend.agents.mcp_loader import MCPLoader
    from backend.runtime.content_logging import is_tainted

    db_path = tmp_path / "approval-race.db"
    store = SqliteProposalStore(db_path) if durable else InMemoryProposalStore()
    publisher = SqliteProposalStore(db_path) if durable else store
    proposal = store.create(
        Proposal(
            "a1",
            "GRAPH__update_node",
            {"description": "SECRET-input"},
            AutonomyLevel.ACT_AFTER_APPROVAL,
        )
    )
    decide = store.decide

    def decide_then_publish(*args, **kwargs):
        snapshot = decide(*args, **kwargs)
        assert snapshot.tainted is False
        if publication == "after_snapshot":
            publisher.mark_tainted([proposal.id])
        return snapshot

    monkeypatch.setattr(store, "decide", decide_then_publish)
    loader = MCPLoader([])
    loader._tools_cache = {
        t.namespaced_name: t
        for t in loader._get_graph_mcp_tools(MCPIntegration(id="GRAPH", name="Graph"))
    }

    def update_node(**kwargs):
        if publication == "during_execution":
            assert not is_tainted()
            publisher.mark_tainted([proposal.id])
        logging.getLogger("httpx").info(
            'HTTP Request: %s %s "%s %d %s"',
            "POST",
            "https://example.test/SECRET-url",
            "HTTP/1.1",
            200,
            "OK",
        )
        if failure == "raised":
            raise ValueError("SECRET-exception")
        return {"error": "SECRET-error"}

    manager = GovernanceManager(
        store,
        lambda _: loader.create_tool_executor(
            graph_service=SimpleNamespace(update_node=update_node)
        ),
    )
    try:
        with caplog.at_level(logging.INFO):
            result = manager.approve(proposal.id)
        assert result["tainted"] is True
        assert result["status"] == "apply_failed"
        assert ("SECRET-input" in caplog.text) is (publication == "during_execution")
        assert "SECRET-error" not in caplog.text
        assert "SECRET-exception" not in caplog.text
        assert "SECRET-url" not in caplog.text
        assert "GRAPH__update_node" in caplog.text
        if publication == "after_snapshot":
            assert "argument_count=1 argument_sizes=" in caplog.text
        assert not is_tainted()
    finally:
        if durable:
            publisher.close()
            store.close()


@pytest.mark.parametrize("version", [2, 3])
def test_taint_schema_migrates_existing_proposals(tmp_path, version):
    import sqlite3

    db = tmp_path / "legacy.db"
    store = SqliteProposalStore(db)
    proposal = store.create(
        Proposal("a", "graph.update_node", {}, AutonomyLevel.ACT_AFTER_APPROVAL)
    )
    store.close()
    with sqlite3.connect(db) as conn:
        conn.execute("ALTER TABLE proposals DROP COLUMN tainted")
        if version == 2:
            conn.execute("ALTER TABLE proposals DROP COLUMN causation_depth")
        conn.execute(f"PRAGMA user_version={version}")
    store = SqliteProposalStore(db)
    try:
        assert store.get(proposal.id).causation_depth == 1
        assert store.get(proposal.id).tainted is False
        store.mark_tainted([proposal.id])
        assert store.get(proposal.id).tainted is True
    finally:
        store.close()
