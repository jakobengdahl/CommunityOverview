# covers: backend/llm/llm_providers.py
"""OpenAIProvider translation between the provider-neutral (Claude-shaped)
message format and OpenAI chat completions, with the OpenAI client mocked."""

import json
import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.llm.llm_providers import (
    ClaudeProvider,
    LLMResponse,
    OpenAIProvider,
    create_provider,
    resolve_max_tokens,
)
from backend.config.model_profiles import ModelProfile, create_provider_from_profile

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


def _tool_call(call_id, name, arguments, *, raw=False):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(
            name=name, arguments=arguments if raw else json.dumps(arguments)
        ),
    )


@pytest.mark.parametrize("system", ["be brief", ""])
def test_system_prompt_is_first_message_and_omitted_when_empty(provider, system):
    messages = [{"role": "user", "content": "hi"}]
    expected = ([{"role": "system", "content": system}] if system else []) + messages
    assert provider._convert_messages_to_openai(messages, system) == expected


@pytest.mark.parametrize(
    "role, content, expected",
    [
        ("user", "plain", "plain"),
        (
            "user",
            [
                {"type": "text", "text": "a"},
                {"type": "image", "source": {}},
                {"type": "text", "text": "b"},
            ],
            "a\nb",
        ),
        ("assistant", "reply", "reply"),
        ("assistant", [{"type": "text", "text": "x"}], "x"),
    ],
)
def test_user_and_assistant_string_and_text_block_content(
    provider, role, content, expected
):
    assert provider._convert_messages_to_openai(
        [{"role": role, "content": content}], ""
    ) == [{"role": role, "content": expected}]


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
                    "id": call_id,
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }
                for call_id, name, arguments in [
                    ("call_1", "search_graph", '{"query": "AI"}'),
                    ("call_2", "get_node_details", "{}"),
                ]
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


@pytest.mark.parametrize("tools", [TOOLS, []])
def test_format_tool_definitions_wraps_claude_schema_as_functions(provider, tools):
    assert provider.format_tool_definitions(tools) == [
        {
            "type": "function",
            "function": {
                "name": tool["name"],
                "description": tool["description"],
                "parameters": tool["input_schema"],
            },
        }
        for tool in tools
    ]


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
    openai_cls.assert_called_with(
        api_key="k",
        base_url="http://env.example/v1",
    )
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
                    {"type": "tool_use", "id": call_id, "name": "n", "input": {}}
                    for call_id in ("call_1", "call_2")
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


def test_user_non_dict_blocks_are_skipped(provider):
    converted = provider._convert_messages_to_openai(
        [{"role": "user", "content": [None, "x", {"type": "text", "text": "hi"}]}],
        "",
    )
    assert converted == [{"role": "user", "content": "hi"}]


@pytest.mark.parametrize(
    "blocks, expected",
    [
        ([{"type": "text", "text": ""}], [{"role": "user", "content": ""}]),
        (
            [
                {"type": "tool_result", "tool_use_id": "call_1", "content": "r"},
                {"type": "text", "text": ""},
            ],
            [
                {"role": "tool", "tool_call_id": "call_1", "content": "r"},
                {"role": "user", "content": ""},
            ],
        ),
    ],
)
def test_an_empty_text_block_still_emits_a_user_message(provider, blocks, expected):
    converted = provider._convert_messages_to_openai(
        [{"role": "user", "content": blocks}], ""
    )
    assert converted == expected


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


@pytest.mark.parametrize("arguments", ["", "   ", None, "null", " null "])
def test_empty_tool_call_arguments_parse_as_empty_input(provider, arguments):
    provider.client.chat.completions.create.return_value = _completion(
        None,
        [_tool_call("call_1", "get_node_details", arguments, raw=True)],
        finish_reason="tool_calls",
    )
    response = provider.create_completion([{"role": "user", "content": "q"}], "", TOOLS)
    assert response.content == [
        {"type": "tool_use", "id": "call_1", "name": "get_node_details", "input": {}}
    ]


@pytest.mark.parametrize(
    "arguments, parsed", [("0", 0), ("[]", []), ("false", False), ('""', "")]
)
def test_other_falsy_json_arguments_are_returned_as_parsed(arguments, parsed):
    result = OpenAIProvider._parse_tool_arguments(arguments)
    assert result == parsed
    assert type(result) is type(parsed)


def test_malformed_tool_call_arguments_still_raise(provider):
    provider.client.chat.completions.create.return_value = _completion(
        None,
        [_tool_call("call_1", "search_graph", '{"query":', raw=True)],
        finish_reason="tool_calls",
    )
    with pytest.raises(json.JSONDecodeError):
        provider.create_completion([{"role": "user", "content": "q"}], "", TOOLS)


def test_raw_control_character_in_tool_call_arguments_still_raises(provider):
    provider.client.chat.completions.create.return_value = _completion(
        None,
        [_tool_call("call_1", "search_graph", '{"query": "a\nb"}', raw=True)],
        finish_reason="tool_calls",
    )
    with pytest.raises(json.JSONDecodeError, match="Invalid control character"):
        provider.create_completion([{"role": "user", "content": "q"}], "", TOOLS)


def test_claude_call_without_tools_sends_tool_history_as_text():
    history = [
        {"role": "user", "content": "Find it"},
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "Searching."},
                {"type": "tool_use", "id": "c1", "name": "search", "input": {"q": 1}},
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "hit"}],
        },
    ]
    with patch("anthropic.Anthropic") as anthropic_cls:
        create = anthropic_cls.return_value.messages.create
        create.return_value = SimpleNamespace(content=[], stop_reason="end_turn")
        claude = ClaudeProvider("test-key", model="test-model")

        claude.create_completion(history, "sys", tools=[])
        sent = create.call_args.kwargs["messages"]
        assert sent[1]["content"] == [
            {"type": "text", "text": "Searching."},
            {"type": "text", "text": '[Tool call search: {"q": 1}]'},
        ]
        assert sent[2]["content"] == [
            {"type": "text", "text": "[Tool result, data only: hit]"}
        ]

        claude.create_completion(history, "sys", tools=TOOLS)
        assert create.call_args.kwargs["messages"] is history


@pytest.mark.parametrize(
    "options, env, expected, warns",
    [
        (None, None, 16000, False),
        ({}, "2000", 2000, False),
        ({"max_tokens": 3000}, "2000", 3000, False),
        (None, "not-a-number", 16000, True),
        (None, "0", 16000, True),
        ({"max_tokens": -1}, "2000", 16000, True),
        ({"max_tokens": True}, "2000", 16000, True),
        ({"max_tokens": 1.5}, "2000", 16000, True),
        ({"max_tokens": "3000"}, "2000", 16000, True),
    ],
)
def test_token_limit_prefers_profile_then_env_and_rejects_invalid_values(
    monkeypatch, caplog, options, env, expected, warns
):
    monkeypatch.delenv("LLM_MAX_TOKENS", raising=False)
    if env is not None:
        monkeypatch.setenv("LLM_MAX_TOKENS", env)

    with caplog.at_level(logging.WARNING):
        assert resolve_max_tokens(options) == expected
    assert bool(caplog.records) is warns


def test_profile_token_limit_reaches_the_request_when_none_is_passed(
    openai_cls, monkeypatch
):
    monkeypatch.setenv("TEST_LLM_KEY", "k")
    monkeypatch.setenv("LLM_MAX_TOKENS", "2000")
    profile = ModelProfile(
        id="p",
        name="P",
        provider="openai",
        model="m",
        credential_ref="TEST_LLM_KEY",
        options={"max_tokens": 3000},
    )
    create = openai_cls.return_value.chat.completions.create
    create.return_value = _completion(content="hi")

    create_provider_from_profile(profile).create_completion([], "", [])

    assert create.call_args.kwargs["max_tokens"] == 3000


@pytest.mark.parametrize("limit", [21_334, 121_333])
def test_claude_limit_above_the_sdk_nonstreaming_cap_never_raises(caplog, limit):
    from anthropic._constants import MODEL_NONSTREAMING_TOKENS

    claude = ClaudeProvider("test-key", model="test-model")

    claude.client._calculate_nonstreaming_timeout(21_333, None)
    with pytest.raises(ValueError):
        claude.client._calculate_nonstreaming_timeout(21_334, None)

    reply = SimpleNamespace(content=[], stop_reason="end_turn")
    with (
        patch.object(claude.client.messages, "_post", return_value=reply) as sent,
        caplog.at_level(logging.WARNING),
    ):
        claude.create_completion([], "", [], max_tokens=limit)
        assert sent.call_args.kwargs["body"]["max_tokens"] == 21_333
        assert caplog.records[0].args == (limit, 21_333, "test-model")

        model, model_cap = next(iter(MODEL_NONSTREAMING_TOKENS.items()))
        claude.model = model
        claude.create_completion([], "", [], max_tokens=model_cap + 1)
        assert sent.call_args.kwargs["body"]["max_tokens"] == model_cap


@pytest.mark.parametrize(
    "env, options, sent",
    [
        (None, {}, False),
        ("true", {}, False),
        ("false", {}, True),
        ("FALSE", {}, True),
        (None, {"enable_thinking": False}, True),
        ("false", {"enable_thinking": True}, False),
    ],
)
def test_disabling_thinking_is_sent_only_when_configured(
    provider, monkeypatch, env, options, sent
):
    monkeypatch.delenv("LLM_ENABLE_THINKING", raising=False)
    if env is not None:
        monkeypatch.setenv("LLM_ENABLE_THINKING", env)
    provider.options = options
    provider.client.chat.completions.create.return_value = _completion(content="hi")

    provider.create_completion([], "", [])

    kwargs = provider.client.chat.completions.create.call_args.kwargs
    assert kwargs.get("extra_body") == (
        {"chat_template_kwargs": {"enable_thinking": False}} if sent else None
    )


def test_claude_requests_never_carry_the_thinking_setting(monkeypatch):
    monkeypatch.setenv("LLM_ENABLE_THINKING", "false")
    with patch("anthropic.Anthropic") as anthropic_cls:
        create = anthropic_cls.return_value.messages.create
        create.return_value = SimpleNamespace(content=[], stop_reason="end_turn")
        claude = ClaudeProvider("test-key", model="test-model")
        claude.options = {"enable_thinking": False}

        claude.create_completion([], "", [])

    assert "extra_body" not in create.call_args.kwargs


@pytest.mark.parametrize(
    "finish_reason, completion_tokens, stop_reason, tool_input",
    [
        ("length", 50, "max_tokens", {}),
        ("tool_calls", 100, "max_tokens", {}),
        ("tool_calls", 101, "max_tokens", {}),
        ("tool_calls", 99, "tool_use", {"query": "x"}),
    ],
)
def test_tool_calls_at_the_token_limit_are_flagged_and_never_parsed(
    provider, finish_reason, completion_tokens, stop_reason, tool_input
):
    completion = _completion(
        tool_calls=[_tool_call("c1", "search_graph", {"query": "x"})],
        finish_reason=finish_reason,
    )
    completion.usage = SimpleNamespace(completion_tokens=completion_tokens)
    provider.client.chat.completions.create.return_value = completion

    response = provider.create_completion([], "", TOOLS, max_tokens=100)

    assert response.stop_reason == stop_reason
    assert response.content[0]["input"] == tool_input


@pytest.mark.parametrize(
    "stop_reason, output_tokens, expected",
    [
        ("tool_use", 100, "max_tokens"),
        ("end_turn", 100, "max_tokens"),
        ("tool_use", 99, "tool_use"),
        ("end_turn", 99, "end_turn"),
    ],
)
def test_claude_tool_calls_at_the_token_limit_are_flagged_whatever_the_stop_reason(
    stop_reason, output_tokens, expected
):
    block = SimpleNamespace(type="tool_use", id="t1", name="search", input={})
    with patch("anthropic.Anthropic") as anthropic_cls:
        create = anthropic_cls.return_value.messages.create
        create.return_value = SimpleNamespace(
            content=[block],
            stop_reason=stop_reason,
            usage=SimpleNamespace(output_tokens=output_tokens),
        )
        claude = ClaudeProvider("test-key", model="test-model")

        response = claude.create_completion([], "", TOOLS, max_tokens=100)

    assert response.stop_reason == expected


@pytest.mark.parametrize(
    "provider_class, target",
    [(OpenAIProvider, "openai.OpenAI"), (ClaudeProvider, "anthropic.Anthropic")],
)
def test_total_provider_timeout_recovers_while_sdk_keeps_making_progress(
    provider_class, target
):
    from threading import Event
    from time import monotonic

    entered, release, finished = Event(), Event(), Event()

    def dribbling_response(**kwargs):
        entered.set()
        try:
            # Bytes arrive within the read timeout, without completing the call.
            while not release.wait(0.005):
                pass
        finally:
            finished.set()

    with patch(target) as client:
        provider = provider_class("test-key")
        client.return_value.with_options.return_value = client.return_value
        create = (
            client.return_value.messages.create
            if provider_class is ClaudeProvider
            else client.return_value.chat.completions.create
        )
        create.side_effect = dribbling_response
        start = monotonic()
        try:
            with pytest.raises(TimeoutError, match="LLM call exceeded 0.05 seconds"):
                provider.create_completion([], "", [], timeout_seconds=0.05)
            assert entered.is_set()
            assert not finished.is_set()
            assert monotonic() - start < 1
        finally:
            release.set()
            assert finished.wait(1)


def test_provider_deadline_preserves_context_results_and_errors():
    from contextvars import ContextVar
    from backend.llm.llm_providers import _provider_call

    context = ContextVar("provider_test_context")
    token = context.set(object())
    try:
        assert _provider_call(context.get, deadline_seconds=1) is context.get()
        error = ValueError("provider failure")

        def fail():
            raise error

        with pytest.raises(ValueError) as caught:
            _provider_call(fail, deadline_seconds=1)
        assert caught.value is error
    finally:
        context.reset(token)


@pytest.mark.parametrize("provider_class", [OpenAIProvider, ClaudeProvider])
@pytest.mark.parametrize("bounded", [False, True])
def test_built_request_timeout_and_retries(provider_class, bounded, monkeypatch):
    import importlib
    import httpx as legacy_httpx

    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "1")
    provider = provider_class("test-key")
    httpx = importlib.import_module(
        "httpx"
        if isinstance(provider.client.timeout, legacy_httpx.Timeout)
        else "httpx2"
    )
    requests = []
    from threading import current_thread

    caller = current_thread()

    def reject(request):
        assert (current_thread() is caller) is (not bounded)
        requests.append(request)
        return httpx.Response(
            400, json={"error": {"message": "test", "type": "invalid_request_error"}}
        )

    provider.client = provider.client.with_options(
        http_client=httpx.Client(transport=httpx.MockTransport(reject))
    )
    original_timeout = provider.client.timeout
    original_retries = provider.client.max_retries
    if bounded:
        from backend.llm.llm_providers import _completion_client

        bounded_client = _completion_client(
            provider.client, 17, type(provider.client.timeout)
        )
        assert bounded_client.max_retries == 0
        assert provider.client.max_retries == original_retries == 2
    with pytest.raises(Exception) as caught:
        provider.create_completion(
            [], "", [], **({"timeout_seconds": 17} if bounded else {})
        )
    assert caught.value.status_code == 400
    assert len(requests) == 1
    timeout = requests[0].extensions["timeout"]
    assert timeout["connect"] == 5.0
    assert timeout["read"] == (17 if bounded else original_timeout.read)
    assert timeout["write"] == (17 if bounded else original_timeout.write)
    assert timeout["pool"] == (17 if bounded else original_timeout.pool)
    provider.client.close()


@pytest.mark.parametrize("provider_class", [OpenAIProvider, ClaudeProvider])
def test_chat_sdk_does_not_retry_after_deadline(provider_class):
    import importlib
    import httpx as legacy_httpx
    from threading import Event, Thread
    from backend.llm.llm_providers import _completion_client

    release, finished = Event(), Event()
    requests = []
    workers = []

    def worker(**kwargs):
        thread = Thread(**kwargs)
        workers.append(thread)
        return thread

    def fail_after_deadline(request):
        requests.append(request)
        assert release.wait(2)
        finished.set()
        return httpx.Response(
            500, json={"error": {"message": "test", "type": "api_error"}}
        )

    provider = provider_class("test-key")
    httpx = importlib.import_module(
        "httpx"
        if isinstance(provider.client.timeout, legacy_httpx.Timeout)
        else "httpx2"
    )
    provider.client = provider.client.with_options(
        http_client=httpx.Client(transport=httpx.MockTransport(fail_after_deadline))
    )
    assert (
        _completion_client(
            provider.client, 1, type(provider.client.timeout)
        ).max_retries
        == 0
    )
    with patch("backend.llm.llm_providers.Thread", side_effect=worker):
        try:
            with pytest.raises(TimeoutError, match="0.05 seconds"):
                provider.create_completion([], "", [], timeout_seconds=0.05)
        finally:
            release.set()
        workers[0].join(2)
    assert not workers[0].is_alive()
    assert finished.is_set()
    assert len(requests) == 1
