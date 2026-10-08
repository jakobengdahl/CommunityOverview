"""
Tests for the recording layer.

The transcript is the only evidence the scorers get, so what it does and does
not capture is the harness's foundation: a dropped tool result would silently
turn an ID-resolution pass into a failure.
"""

import pytest

from backend.evaluation.transcript import (
    RecordingProvider,
    TokenUsage,
    extract_usage,
)
from backend.evaluation.tests.conftest import ScriptedProvider, _RawResponse


class TestExtractUsage:
    def test_reads_openai_field_names(self):
        usage = extract_usage(
            _RawResponse({"prompt_tokens": 120, "completion_tokens": 30})
        )
        assert (usage.prompt_tokens, usage.completion_tokens) == (120, 30)
        assert usage.total_tokens == 150
        assert usage.reported

    def test_reads_anthropic_field_names(self):
        usage = extract_usage(_RawResponse({"input_tokens": 7, "output_tokens": 11}))
        assert (usage.prompt_tokens, usage.completion_tokens) == (7, 11)
        assert usage.total_tokens == 18

    def test_a_provider_that_reports_no_usage_is_unreported_not_zero(self):
        """An OpenAI-compatible endpoint may omit usage; that is not 0 tokens."""
        usage = extract_usage(None)
        assert not usage.reported
        assert usage.prompt_tokens is None
        assert usage.total_tokens is None

    def test_partial_usage_is_still_reported(self):
        usage = extract_usage(_RawResponse({"prompt_tokens": 5}))
        assert usage.reported
        assert usage.total_tokens == 5

    def test_non_integer_counts_are_ignored(self):
        """A stringly-typed count must not be summed as if it were a number."""
        usage = extract_usage(_RawResponse({"prompt_tokens": "lots"}))
        assert not usage.reported

    def test_reads_usage_from_a_plain_dict_response(self):
        usage = extract_usage({"usage": {"prompt_tokens": 3, "completion_tokens": 4}})
        assert usage.total_tokens == 7


class TestTokenUsageTotals:
    def test_total_of_nothing_reported_is_none(self):
        assert TokenUsage().total_tokens is None


class TestRecordingProvider:
    def test_records_requested_tool_calls_in_order(self):
        inner = ScriptedProvider(
            [
                [("search_graph", {"query": "a"})],
                [("update_node", {"node_id": "n1", "updates": {}})],
                "done",
            ]
        )
        recorder = RecordingProvider(inner)
        for _ in range(3):
            recorder.create_completion([], "sys", [{"name": "search_graph"}])

        assert recorder.transcript.tool_call_names == ["search_graph", "update_node"]
        assert recorder.transcript.tool_calls[0].turn == 0
        assert recorder.transcript.tool_calls[1].turn == 1

    def test_records_parallel_tool_calls_from_one_turn(self):
        inner = ScriptedProvider(
            [[("search_graph", {"query": "a"}), ("list_node_types", {})], "done"]
        )
        recorder = RecordingProvider(inner)
        recorder.create_completion([], "sys", [])
        calls = recorder.transcript.tool_calls
        assert [c.name for c in calls] == ["search_graph", "list_node_types"]
        assert {c.turn for c in calls} == {0}

    def test_harvests_tool_results_the_assistant_feeds_back(self):
        """Results reach the model as JSON text; the transcript decodes them."""
        recorder = RecordingProvider(ScriptedProvider(["done"]))
        messages = [
            {"role": "user", "content": "hi"},
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "call_0_0",
                        "content": '{"nodes": [{"id": "eval-node-one"}]}',
                    }
                ],
            },
        ]
        recorder.create_completion(messages, "sys", [])
        assert recorder.transcript.tool_results["call_0_0"] == {
            "nodes": [{"id": "eval-node-one"}]
        }

    def test_a_non_json_tool_result_is_kept_verbatim(self):
        recorder = RecordingProvider(ScriptedProvider(["done"]))
        recorder.create_completion(
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "x",
                            "content": "not json at all",
                        }
                    ],
                }
            ],
            "sys",
            [],
        )
        assert recorder.transcript.tool_results["x"] == "not json at all"

    def test_the_first_result_for_an_id_wins(self):
        """
        History is re-sent every turn; a result must be counted once.

        The two payloads differ on purpose: re-sending the identical block makes
        first-wins and last-wins produce the same value, so the de-duplication
        could be removed without the assertion noticing — the test's name
        claimed an invariant it could not distinguish.
        """
        recorder = RecordingProvider(ScriptedProvider(["a", "b"]))

        def block(payload):
            return {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "x", "content": payload}
                ],
            }

        recorder.create_completion([block('{"n": 1}')], "sys", [])
        recorder.create_completion([block('{"n": 2}')], "sys", [])
        assert recorder.transcript.tool_results == {"x": {"n": 1}}

    def test_records_latency_advertised_tools_and_usage(self):
        inner = ScriptedProvider(
            ["done"], usage={"prompt_tokens": 10, "completion_tokens": 2}
        )
        recorder = RecordingProvider(inner)
        recorder.create_completion(
            [], "sys", [{"name": "search_graph"}, {"name": "get_schema"}]
        )

        call = recorder.transcript.provider_calls[0]
        assert call.latency_ms >= 0
        assert call.tools_advertised == ["search_graph", "get_schema"]
        assert call.stop_reason == "end_turn"
        assert call.usage.total_tokens == 12
        assert recorder.transcript.total_usage.total_tokens == 12

    def test_usage_sums_across_calls_and_latency_accumulates(self):
        inner = ScriptedProvider(
            [[("get_schema", {})], "done"],
            usage={"prompt_tokens": 100, "completion_tokens": 5},
        )
        recorder = RecordingProvider(inner)
        recorder.create_completion([], "sys", [])
        recorder.create_completion([], "sys", [])
        assert recorder.transcript.total_usage.prompt_tokens == 200
        assert recorder.transcript.total_usage.completion_tokens == 10
        assert recorder.transcript.total_latency_ms >= 0
        assert len(recorder.transcript.provider_calls) == 2

    def test_a_provider_error_is_recorded_and_re_raised(self):
        """The runner must see the exception; the transcript must keep the evidence."""
        recorder = RecordingProvider(ScriptedProvider(["done"], raise_on_turn=0))
        with pytest.raises(RuntimeError, match="scripted provider failure"):
            recorder.create_completion([], "sys", [{"name": "search_graph"}])

        call = recorder.transcript.provider_calls[0]
        assert call.error is not None
        assert "scripted provider failure" in call.error
        assert call.stop_reason is None

    def test_delegates_tool_formatting_to_the_wrapped_provider(self):
        inner = ScriptedProvider(["done"])
        recorder = RecordingProvider(inner)
        tools = [{"name": "search_graph", "input_schema": {}}]
        assert recorder.format_tool_definitions(tools) == tools
        assert recorder.inner is inner

    def test_system_prompt_is_recorded_so_skill_injection_can_be_asserted(self):
        recorder = RecordingProvider(ScriptedProvider(["done"]))
        recorder.create_completion([], "SYSTEM TEXT", [])
        assert recorder.transcript.provider_calls[0].system_prompt == "SYSTEM TEXT"
