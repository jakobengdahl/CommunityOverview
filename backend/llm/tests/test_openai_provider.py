"""OpenAIProvider translation between the provider-neutral (Claude-shaped)
message format and OpenAI chat completions, with the OpenAI client mocked."""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.llm.llm_providers import (
    ClaudeProvider,
    LLMResponse,
    OpenAIProvider,
    create_provider,
)

TOOLS = [
    {
        "name": "search_graph",
        "description": "Search the graph",
        "input_schema": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
    {
        "name": "get_node_details",
        "description": "Get one node",
        "input_schema": {"type": "object", "properties": {}},
    },
]


@pytest.fixture
def openai_cls():
    with patch("openai.OpenAI") as cls:
        yield cls


@pytest.fixture
def provider(openai_cls, monkeypatch):
    monkeypatch.delenv("OPENAI_TOOL_CALLING", raising=False)
    return OpenAIProvider("test-key", model="test-model")


def _completion(content=None, tool_calls=None, finish_reason="stop"):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason=finish_reason)]
    )


def _tool_call(call_id, name, arguments):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
    )


def test_system_prompt_is_first_message_and_omitted_when_empty(provider):
    converted = provider._convert_messages_to_openai(
        [{"role": "user", "content": "hi"}], "be brief"
    )
    assert converted == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hi"},
    ]
    assert provider._convert_messages_to_openai(
        [{"role": "user", "content": "hi"}], ""
    ) == [{"role": "user", "content": "hi"}]


def test_user_and_assistant_string_and_text_block_content(provider):
    converted = provider._convert_messages_to_openai(
        [
            {"role": "user", "content": "plain"},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "a"},
                    {"type": "image", "source": {}},
                    {"type": "text", "text": "b"},
                ],
            },
            {"role": "assistant", "content": "reply"},
            {"role": "assistant", "content": [{"type": "text", "text": "x"}]},
        ],
        "",
    )
    assert converted == [
        {"role": "user", "content": "plain"},
        {"role": "user", "content": "a\nb"},
        {"role": "assistant", "content": "reply"},
        {"role": "assistant", "content": "x"},
    ]


def test_user_blocks_without_text_produce_no_message(provider):
    converted = provider._convert_messages_to_openai(
        [{"role": "user", "content": [{"type": "image", "source": {}}]}], ""
    )
    assert converted == []


def test_assistant_tool_use_becomes_tool_calls_with_json_arguments(provider):
    converted = provider._convert_messages_to_openai(
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Let me look."},
                    {
                        "type": "tool_use",
                        "id": "call_1",
                        "name": "search_graph",
                        "input": {"query": "AI"},
                    },
                    {
                        "type": "tool_use",
                        "id": "call_2",
                        "name": "get_node_details",
                    },
                ],
            }
        ],
        "",
    )
    assert converted == [
        {
            "role": "assistant",
            "content": "Let me look.",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "search_graph",
                        "arguments": '{"query": "AI"}',
                    },
                },
                {
                    "id": "call_2",
                    "type": "function",
                    "function": {"name": "get_node_details", "arguments": "{}"},
                },
            ],
        }
    ]


def test_assistant_tool_use_only_has_no_content_key(provider):
    converted = provider._convert_messages_to_openai(
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "c", "name": "n", "input": {}},
                ],
            }
        ],
        "",
    )
    assert "content" not in converted[0]
    assert converted[0]["tool_calls"][0]["id"] == "c"


def test_tool_results_become_one_tool_message_per_result_with_matching_ids(
    provider,
):
    converted = provider._convert_messages_to_openai(
        [
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "call_1", "content": "r1"},
                    {"type": "tool_result", "tool_use_id": "call_2"},
                ],
            }
        ],
        "",
    )
    assert converted == [
        {"role": "tool", "tool_call_id": "call_1", "content": "r1"},
        {"role": "tool", "tool_call_id": "call_2", "content": ""},
    ]


def test_unknown_roles_are_dropped(provider):
    assert (
        provider._convert_messages_to_openai([{"role": "x", "content": "y"}], "") == []
    )


@pytest.mark.parametrize(
    "finish_reason,stop_reason",
    [
        ("stop", "end_turn"),
        ("tool_calls", "tool_use"),
        ("length", "max_tokens"),
        ("content_filter", "end_turn"),
        ("function_call", "end_turn"),
        (None, "end_turn"),
    ],
)
def test_finish_reason_maps_to_provider_neutral_stop_reason(
    provider, finish_reason, stop_reason
):
    assert provider._map_finish_reason(finish_reason) == stop_reason


def test_format_tool_definitions_wraps_claude_schema_as_functions(provider):
    assert provider.format_tool_definitions(TOOLS) == [
        {
            "type": "function",
            "function": {
                "name": "search_graph",
                "description": "Search the graph",
                "parameters": TOOLS[0]["input_schema"],
            },
        },
        {
            "type": "function",
            "function": {
                "name": "get_node_details",
                "description": "Get one node",
                "parameters": TOOLS[1]["input_schema"],
            },
        },
    ]
    assert provider.format_tool_definitions([]) == []


def test_create_completion_sends_converted_request(provider):
    provider.client.chat.completions.create.return_value = _completion("hello")
    response = provider.create_completion(
        [{"role": "user", "content": "hi"}], "sys", TOOLS, max_tokens=123
    )
    kwargs = provider.client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == "test-model"
    assert kwargs["max_tokens"] == 123
    assert kwargs["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]
    assert kwargs["tools"] == provider.format_tool_definitions(TOOLS)
    assert kwargs["tool_choice"] == "auto"
    assert isinstance(response, LLMResponse)
    assert response.content == [{"type": "text", "text": "hello"}]
    assert response.stop_reason == "end_turn"
    assert response.raw_response is provider.client.chat.completions.create.return_value


def test_create_completion_without_tools_sends_none(provider):
    provider.client.chat.completions.create.return_value = _completion("ok")
    provider.create_completion([{"role": "user", "content": "hi"}], "", [])
    kwargs = provider.client.chat.completions.create.call_args.kwargs
    assert kwargs["tools"] is None
    assert kwargs["tool_choice"] is None


def test_tool_calling_disabled_by_env_omits_tools(openai_cls, monkeypatch):
    monkeypatch.setenv("OPENAI_TOOL_CALLING", "False")
    provider = OpenAIProvider("k", model="m")
    provider.client.chat.completions.create.return_value = _completion("ok")
    provider.create_completion([{"role": "user", "content": "hi"}], "", TOOLS)
    kwargs = provider.client.chat.completions.create.call_args.kwargs
    assert kwargs["tools"] is None
    assert kwargs["tool_choice"] is None


def test_create_completion_maps_text_and_tool_calls_to_neutral_shape(provider):
    provider.client.chat.completions.create.return_value = _completion(
        "Searching.",
        [
            _tool_call("call_1", "search_graph", {"query": "AI"}),
            _tool_call("call_2", "get_node_details", {"id": "n1"}),
        ],
        finish_reason="tool_calls",
    )
    response = provider.create_completion(
        [{"role": "user", "content": "find AI"}], "", TOOLS
    )
    assert response.stop_reason == "tool_use"
    assert response.content == [
        {"type": "text", "text": "Searching."},
        {
            "type": "tool_use",
            "id": "call_1",
            "name": "search_graph",
            "input": {"query": "AI"},
        },
        {
            "type": "tool_use",
            "id": "call_2",
            "name": "get_node_details",
            "input": {"id": "n1"},
        },
    ]


def test_tool_call_round_trip_preserves_ids_names_and_arguments(provider):
    """A tool-calling turn fed back as history yields the OpenAI request shape
    the API requires: assistant tool_calls followed by tool messages whose
    tool_call_id matches."""
    provider.client.chat.completions.create.return_value = _completion(
        None,
        [_tool_call("call_9", "search_graph", {"query": "AI", "limit": 5})],
        finish_reason="tool_calls",
    )
    first = provider.create_completion([{"role": "user", "content": "q"}], "", TOOLS)
    assert first.content[0]["type"] == "tool_use"
    assert len(first.content) == 1

    history = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": first.content},
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": first.content[0]["id"],
                    "content": json.dumps({"nodes": []}),
                }
            ],
        },
    ]
    provider.client.chat.completions.create.return_value = _completion("done")
    second = provider.create_completion(history, "", TOOLS)

    sent = provider.client.chat.completions.create.call_args.kwargs["messages"]
    assert sent[1] == {
        "role": "assistant",
        "tool_calls": [
            {
                "id": "call_9",
                "type": "function",
                "function": {
                    "name": "search_graph",
                    "arguments": json.dumps({"query": "AI", "limit": 5}),
                },
            }
        ],
    }
    assert sent[2] == {
        "role": "tool",
        "tool_call_id": "call_9",
        "content": '{"nodes": []}',
    }
    assert second.content == [{"type": "text", "text": "done"}]


def test_constructor_resolves_base_url_and_model(openai_cls, monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://env.example/v1")
    monkeypatch.setenv("OPENAI_MODEL", "env-model")
    provider = OpenAIProvider("k")
    openai_cls.assert_called_with(api_key="k", base_url="http://env.example/v1")
    assert provider.model == "env-model"

    provider = OpenAIProvider("k", model="explicit", base_url="http://arg.example")
    openai_cls.assert_called_with(api_key="k", base_url="http://arg.example")
    assert provider.model == "explicit"

    monkeypatch.delenv("OPENAI_BASE_URL")
    monkeypatch.delenv("OPENAI_MODEL")
    provider = OpenAIProvider("k")
    openai_cls.assert_called_with(api_key="k", base_url=None)
    assert provider.model == "gpt-4o"


def test_create_provider_selects_by_argument(openai_cls):
    provider = create_provider(
        "k", provider_type="openai", model="m", base_url="http://b"
    )
    assert isinstance(provider, OpenAIProvider)
    openai_cls.assert_called_once_with(api_key="k", base_url="http://b")
    assert provider.model == "m"
    with patch("anthropic.Anthropic") as anthropic_cls:
        provider = create_provider(
            "k", provider_type="claude", model="m", base_url="http://b"
        )
    assert isinstance(provider, ClaudeProvider)
    anthropic_cls.assert_called_once_with(api_key="k", base_url="http://b")
    assert provider.model == "m"


def test_create_provider_falls_back_to_env_then_claude(openai_cls, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "OpenAI")
    assert isinstance(create_provider("k"), OpenAIProvider)
    monkeypatch.delenv("LLM_PROVIDER")
    with patch("anthropic.Anthropic"):
        assert isinstance(create_provider("k"), ClaudeProvider)


def test_create_provider_rejects_unknown_type(openai_cls):
    with pytest.raises(ValueError, match="Unknown provider type: gemini"):
        create_provider("k", provider_type="gemini")
    openai_cls.assert_not_called()


def test_tool_calls_attribute_none_and_empty_content_yield_no_blocks(provider):
    provider.client.chat.completions.create.return_value = _completion(
        "", None, finish_reason="length"
    )
    response = provider.create_completion([{"role": "user", "content": "q"}], "", [])
    assert response.content == []
    assert response.stop_reason == "max_tokens"


def test_assistant_non_dict_and_unknown_blocks_are_skipped(provider):
    converted = provider._convert_messages_to_openai(
        [
            {
                "role": "assistant",
                "content": [
                    "stray",
                    {"type": "thinking", "thinking": "hidden"},
                    {"type": "text", "text": "visible"},
                ],
            },
            {"role": "user", "content": None},
            {"role": "assistant", "content": None},
        ],
        "",
    )
    assert converted == [{"role": "assistant", "content": "visible"}]


def test_user_turn_mixing_tool_results_and_text_keeps_text_after_tool_messages(
    provider,
):
    converted = provider._convert_messages_to_openai(
        [
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "call_1", "name": "n", "input": {}},
                    {"type": "tool_use", "id": "call_2", "name": "n", "input": {}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "call_1", "content": "r1"},
                    {"type": "text", "text": "also consider X"},
                    {"type": "tool_result", "tool_use_id": "call_2", "content": "r2"},
                    {"type": "text", "text": "and Y"},
                ],
            },
        ],
        "",
    )
    assert [m["role"] for m in converted] == ["assistant", "tool", "tool", "user"]
    assert converted[1:] == [
        {"role": "tool", "tool_call_id": "call_1", "content": "r1"},
        {"role": "tool", "tool_call_id": "call_2", "content": "r2"},
        {"role": "user", "content": "also consider X\nand Y"},
    ]


def test_user_turn_with_text_before_tool_result_keeps_tool_result(provider):
    converted = provider._convert_messages_to_openai(
        [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "note"},
                    {"type": "tool_result", "tool_use_id": "call_1", "content": "r"},
                ],
            }
        ],
        "",
    )
    assert converted == [
        {"role": "tool", "tool_call_id": "call_1", "content": "r"},
        {"role": "user", "content": "note"},
    ]


@pytest.mark.parametrize("arguments", ["", "   ", None])
def test_empty_tool_call_arguments_parse_as_empty_input(provider, arguments):
    provider.client.chat.completions.create.return_value = _completion(
        None,
        [
            SimpleNamespace(
                id="call_1",
                function=SimpleNamespace(name="get_node_details", arguments=arguments),
            )
        ],
        finish_reason="tool_calls",
    )
    response = provider.create_completion([{"role": "user", "content": "q"}], "", TOOLS)
    assert response.content == [
        {"type": "tool_use", "id": "call_1", "name": "get_node_details", "input": {}}
    ]


def test_malformed_tool_call_arguments_still_raise(provider):
    provider.client.chat.completions.create.return_value = _completion(
        None,
        [
            SimpleNamespace(
                id="call_1",
                function=SimpleNamespace(name="search_graph", arguments='{"query":'),
            )
        ],
        finish_reason="tool_calls",
    )
    with pytest.raises(json.JSONDecodeError):
        provider.create_completion([{"role": "user", "content": "q"}], "", TOOLS)
