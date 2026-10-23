"""Revision expectations survive the registered MCP tool boundary."""

from unittest.mock import Mock

import pytest

from backend.core import GraphStorage, Node
from backend.service import GraphService, register_mcp_tools


@pytest.fixture
def revision_tools(tmp_path):
    storage = GraphStorage(json_path=str(tmp_path / "graph.json"))
    storage.add_nodes([Node(id="a", type="Actor", name="original")], [])
    mcp = Mock()
    mcp.tool.return_value = lambda tool: tool
    tools = register_mcp_tools(mcp, GraphService(storage))
    try:
        yield tools, storage
    finally:
        storage.shutdown_events()


def test_update_node_forwards_expected_revision(revision_tools):
    tools, storage = revision_tools
    revision = storage.nodes["a"].revision
    applied = tools["update_node"]("a", {"name": "changed"}, expected_revision=revision)
    assert applied["success"] is True
    current = storage.nodes["a"].revision
    assert current > revision

    refused = tools["update_node"]("a", {"name": "stale"}, expected_revision=revision)
    assert refused["success"] is False
    assert refused["error"] == "entity_revision_conflict"
    assert refused["current_revision"] == current
    assert storage.nodes["a"].name == "changed"


def test_delete_nodes_forwards_expected_revisions(revision_tools):
    tools, storage = revision_tools
    revision = storage.nodes["a"].revision
    refused = tools["delete_nodes"](
        ["a"], confirmed=True, expected_revisions={"a": revision + 1}
    )
    assert refused["success"] is False
    assert refused["error"] == "entity_revision_conflict"
    assert refused["current_revision"] == revision
    assert "a" in storage.nodes

    applied = tools["delete_nodes"](
        ["a"], confirmed=True, expected_revisions={"a": revision}
    )
    assert applied["success"] is True
    assert "a" not in storage.nodes


def test_add_nodes_forwards_expect_absent_and_refuses_the_whole_batch(revision_tools):
    tools, storage = revision_tools
    refused = tools["add_nodes"](
        [
            {"id": "fresh", "type": "Actor", "name": "fresh"},
            {"id": "a", "type": "Actor", "name": "duplicate"},
        ],
        [],
        expect_absent=True,
    )
    assert refused["success"] is False
    assert refused["error"] == "entity_revision_conflict"
    assert refused["current_revision"] is None
    assert set(storage.nodes) == {"a"}
    assert storage.nodes["a"].name == "original"
