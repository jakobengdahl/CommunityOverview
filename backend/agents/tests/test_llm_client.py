"""Tests for the agent tool loop in LLMClient.execute_with_tools."""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from backend.agents.governance import (
    AutonomyGate,
    AutonomyLevel,
    InMemoryProposalStore,
)
from backend.agents.llm_client import LLMClient
from backend.llm.llm_providers import LLMResponse


class FakeLoader:
    def get_tool(self, namespaced_name):
        return SimpleNamespace(original_name=namespaced_name.split(".")[-1])


class ScriptedProvider:
    """Returns the scripted tool calls once, then ends the turn."""

    def __init__(self, tool_calls):
        self.tool_calls = tool_calls
        self.received_messages = []

    def create_completion(self, messages, system_prompt, tools, max_tokens=4096):
        self.received_messages.append(list(messages))
        if len(self.received_messages) == 1:
            return LLMResponse(
                content=[
                    {"type": "tool_use", "id": f"t{i}", "name": name, "input": args}
                    for i, (name, args) in enumerate(self.tool_calls)
                ],
                stop_reason="tool_use",
            )
        return LLMResponse(
            content=[{"type": "text", "text": "done"}], stop_reason="end_turn"
        )


@pytest.mark.parametrize(
    "tool_input, json_type",
    [
        ([], "array"),
        ([["nodes", []]], "array"),
        (5, "number"),
        (False, "boolean"),
        ("x", "string"),
        (None, "null"),
    ],
)
def test_non_object_input_is_an_error_and_never_becomes_a_proposal(
    tool_input, json_type
):
    # The gate records mutating calls as proposals with dict(input_args), which
    # turns [] into {} and [["k", v]] into {"k": v}; a non-object input must be
    # rejected before any executor sees it, while the next call still runs.
    store = InMemoryProposalStore()
    raw = MagicMock(return_value={"ok": True})
    executor = AutonomyGate(
        autonomy_level=AutonomyLevel.ACT_AFTER_APPROVAL,
        tool_allowlist=None,
        mcp_loader=FakeLoader(),
        proposal_store=store,
        agent_id="a1",
        agent_name="A",
        run_id="run-1",
    ).wrap(raw)
    provider = ScriptedProvider(
        [("graph.add_nodes", tool_input), ("graph.search_graph", {"query": "x"})]
    )

    result = LLMClient(llm_provider=provider).execute_with_tools(
        system_prompt="", user_message="event", tools=[], tool_executor=executor
    )

    assert result["success"] is True
    assert store.list_proposals() == []
    raw.assert_called_once_with("graph.search_graph", {"query": "x"})
    bad, good = provider.received_messages[1][-1]["content"]
    assert bad["is_error"] is True
    assert bad["content"] == (
        '{"error": "Tool \'graph.add_nodes\' was not run: its arguments must be '
        f'a JSON object, got {json_type}."}}'
    )
    assert good["is_error"] is False
