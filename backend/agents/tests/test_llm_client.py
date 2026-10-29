"""Tests for the agent tool loop in LLMClient.execute_with_tools."""

from datetime import datetime
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
    """Returns the scripted tool calls once, after ``text`` when given, then
    ends the turn."""

    def __init__(self, tool_calls, text=None):
        self.tool_calls = tool_calls
        self.text = text
        self.received_messages = []

    def create_completion(self, messages, system_prompt, tools, max_tokens=4096):
        self.received_messages.append(list(messages))
        if len(self.received_messages) == 1:
            text = [] if self.text is None else [{"type": "text", "text": self.text}]
            return LLMResponse(
                content=text
                + [
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
    assert (bad["tool_use_id"], bad["is_error"]) == ("t0", True)
    assert bad["content"] == (
        '{"error": "Tool \'graph.add_nodes\' was not run: its arguments must be '
        f'a JSON object, got {json_type}."}}'
    )
    assert (good["tool_use_id"], good["is_error"]) == ("t1", False)


def test_tool_result_messages_carry_exactly_id_content_and_error_flag():
    # Pins the full provider-facing messages for every tool-result branch
    # (bad input, success, executor exception) so the internal bookkeeping
    # can change without altering what the provider receives.
    def executor(name, args):
        if name == "graph.boom":
            raise RuntimeError("kaput")
        return {"ok": True}

    provider = ScriptedProvider(
        [
            ("graph.add_nodes", []),
            ("graph.search_graph", {"query": "x"}),
            ("graph.boom", {}),
        ]
    )

    LLMClient(llm_provider=provider).execute_with_tools(
        system_prompt="", user_message="event", tools=[], tool_executor=executor
    )

    assert provider.received_messages[1][1:] == [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "t0",
                    "name": "graph.add_nodes",
                    "input": [],
                },
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "graph.search_graph",
                    "input": {"query": "x"},
                },
                {"type": "tool_use", "id": "t2", "name": "graph.boom", "input": {}},
            ],
        },
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "t0",
                    "content": (
                        '{"error": "Tool \'graph.add_nodes\' was not run: its '
                        'arguments must be a JSON object, got array."}'
                    ),
                    "is_error": True,
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "t1",
                    "content": '{"ok": true}',
                    "is_error": False,
                },
                {
                    "type": "tool_result",
                    "tool_use_id": "t2",
                    "content": '{"error": "kaput"}',
                    "is_error": True,
                },
            ],
        },
    ]


def test_a_tool_result_json_cannot_encode_is_sent_as_its_string_form():
    # A tool may return datetimes or sets; the loop must hand the provider a
    # string rather than fail the whole run mid-way through its tool calls.
    provider = ScriptedProvider([("graph.search_graph", {"query": "x"})])

    result = LLMClient(llm_provider=provider).execute_with_tools(
        system_prompt="",
        user_message="event",
        tools=[],
        tool_executor=lambda name, args: {
            "at": datetime(2026, 1, 2, 3, 4, 5),
            "tags": {"a"},
        },
    )

    assert result["success"] is True
    (tool_result,) = provider.received_messages[1][-1]["content"]
    assert tool_result["content"] == (
        '{"at": "2026-01-02 03:04:05", "tags": "{\'a\'}"}'
    )


def test_text_alongside_tool_calls_is_kept_first_in_the_assistant_message():
    provider = ScriptedProvider(
        [("graph.search_graph", {"query": "x"})], text="Looking it up."
    )

    LLMClient(llm_provider=provider).execute_with_tools(
        system_prompt="",
        user_message="event",
        tools=[],
        tool_executor=lambda name, args: {"ok": True},
    )

    assert provider.received_messages[1][1] == {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Looking it up."},
            {
                "type": "tool_use",
                "id": "t0",
                "name": "graph.search_graph",
                "input": {"query": "x"},
            },
        ],
    }


def test_the_trace_records_each_turn_with_its_tool_call_inputs():
    provider = ScriptedProvider(
        [("graph.search_graph", {"query": "x"})], text="Looking it up."
    )

    result = LLMClient(llm_provider=provider).execute_with_tools(
        system_prompt="",
        user_message="event",
        tools=[],
        tool_executor=lambda name, args: {"ok": True},
    )

    assert result == {
        "success": True,
        "final_response": "done",
        "turns": 2,
        "trace": [
            {
                "turn": 1,
                "text_response": "Looking it up.",
                "tool_calls": [
                    {"id": "t0", "name": "graph.search_graph", "input": {"query": "x"}}
                ],
                "stop_reason": "tool_use",
            },
            {
                "turn": 2,
                "text_response": "done",
                "tool_calls": [],
                "stop_reason": "end_turn",
            },
        ],
    }
