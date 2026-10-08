"""
Tests for the scorers.

Each scorer is tested in both directions. A scorer that only ever returns True
would make every shipped case pass against every model, which is the one defect
that would make the whole harness worse than no harness — it would report
reliability nobody verified.
"""

import pytest

from backend.evaluation.cases import AcceptanceCase, ExpectedBehaviour
from backend.evaluation.scoring import (
    ID_TOKEN_RE,
    is_id_shaped,
    score_case,
    _align,
    ConditionResult,
    score_answer_entities_supported,
    score_discriminating_first_call,
    score_final_node_fields_changed,
    score_final_node_state,
    score_forbidden_calls,
    score_ids_resolved_from_results,
    score_required_call_sequence,
    score_tool_calls_valid,
    score_verify_after_write,
)
from backend.evaluation.transcript import ProviderCall, RunTranscript, ToolCall

SEARCH_SCHEMA = {
    "name": "search_graph",
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
        "required": ["query"],
    },
}
UPDATE_SCHEMA = {
    "name": "update_node",
    "input_schema": {
        "type": "object",
        "properties": {"node_id": {"type": "string"}, "updates": {"type": "object"}},
        "required": ["node_id", "updates"],
    },
}
TOOL_DEFS = [SEARCH_SCHEMA, UPDATE_SCHEMA]


def transcript(
    calls=(), results=None, final_text="", final_graph=None, advertised=None
):
    """Build a transcript directly, so a scorer can be tested without a run."""
    names = (
        list(advertised) if advertised is not None else ["search_graph", "update_node"]
    )
    tr = RunTranscript(
        provider_calls=[
            ProviderCall(
                turn=0, latency_ms=1.0, stop_reason="tool_use", tools_advertised=names
            )
        ],
        tool_calls=list(calls),
        tool_results=dict(results or {}),
        final_text=final_text,
        final_graph=final_graph or {},
    )
    return tr


def call(name, arguments, turn=0, tool_use_id=None):
    return ToolCall(
        name=name, input=arguments, tool_use_id=tool_use_id or f"c{turn}", turn=turn
    )


class TestToolCallValidity:
    def test_valid_calls_pass(self):
        tr = transcript([call("search_graph", {"query": "x", "limit": 5})])
        assert score_tool_calls_valid(tr, TOOL_DEFS).passed

    def test_a_missing_required_argument_fails(self):
        tr = transcript([call("search_graph", {"qeury": "x"})])
        result = score_tool_calls_valid(tr, TOOL_DEFS)
        assert not result.passed
        assert "'query' is a required property" in result.detail

    def test_a_wrongly_typed_argument_fails(self):
        tr = transcript([call("search_graph", {"query": "x", "limit": "five"})])
        result = score_tool_calls_valid(tr, TOOL_DEFS)
        assert not result.passed
        assert "limit" in result.detail

    def test_a_tool_the_run_never_advertised_fails(self):
        """A model may invent a tool name; the run's own advertised set decides."""
        tr = transcript([call("get_node_details", {"node_id": "n1"})])
        result = score_tool_calls_valid(tr, TOOL_DEFS)
        assert not result.passed
        assert "not advertised" in result.detail

    def test_making_no_tool_calls_at_all_fails(self):
        assert not score_tool_calls_valid(transcript([]), TOOL_DEFS).passed

    def test_an_invented_parameter_name_fails(self):
        """
        The quiet failure: chat_logic drops arguments it does not recognise.

        The schemas do not set additionalProperties:false, so a validator
        accepts `max_results`; the assistant then filters it out and runs
        search_graph with the default limit, reporting success. The model chose
        a value that was discarded and was never told.
        """
        tr = transcript([call("search_graph", {"query": "x", "max_results": 5})])
        result = score_tool_calls_valid(tr, TOOL_DEFS)
        assert not result.passed
        assert "max_results" in result.detail
        assert "not a parameter of this tool" in result.detail

    def test_declared_optional_parameters_are_accepted(self):
        tr = transcript([call("search_graph", {"query": "x", "limit": 5})])
        assert score_tool_calls_valid(tr, TOOL_DEFS).passed

    def test_a_schema_with_no_properties_accepts_any_arguments(self):
        """A no-argument tool (list_node_types) must not fail on an empty dict."""
        defs = [{"name": "list_node_types", "input_schema": {"type": "object"}}]
        tr = transcript([call("list_node_types", {})], advertised=["list_node_types"])
        assert score_tool_calls_valid(tr, defs).passed

    def test_nested_object_arguments_are_not_checked_for_undeclared_keys(self):
        """
        update_node.updates is free-form by design (additionalProperties: true).

        Only top-level parameters are checked; flagging keys inside `updates`
        would fail every legitimate write.
        """
        tr = transcript(
            [call("update_node", {"node_id": "n", "updates": {"anything": 1}})]
        )
        assert score_tool_calls_valid(tr, TOOL_DEFS).passed


class TestRequiredCallSequence:
    def test_an_ordered_subsequence_passes_even_with_calls_in_between(self):
        tr = transcript(
            [
                call("search_graph", {"query": "x"}, turn=0),
                call("get_schema", {}, turn=1),
                call("update_node", {"node_id": "n", "updates": {}}, turn=2),
            ]
        )
        assert score_required_call_sequence(tr, ["search_graph", "update_node"]).passed

    def test_the_wrong_order_fails(self):
        tr = transcript(
            [
                call("update_node", {"node_id": "n", "updates": {}}, turn=0),
                call("search_graph", {"query": "x"}, turn=1),
            ]
        )
        assert not score_required_call_sequence(
            tr, ["search_graph", "update_node"]
        ).passed

    def test_a_missing_call_fails(self):
        tr = transcript([call("search_graph", {"query": "x"})])
        assert not score_required_call_sequence(
            tr, ["search_graph", "update_node"]
        ).passed


class TestForbiddenCalls:
    def test_not_calling_a_forbidden_tool_passes(self):
        tr = transcript([call("search_graph", {"query": "x"})])
        assert score_forbidden_calls(tr, ["update_node"]).passed

    def test_calling_a_forbidden_tool_fails(self):
        tr = transcript(
            [
                call("search_graph", {"query": "x"}, turn=0),
                call("update_node", {"node_id": "n", "updates": {}}, turn=1),
            ]
        )
        result = score_forbidden_calls(tr, ["update_node"])
        assert not result.passed
        assert "update_node" in result.detail


class TestIdsResolvedFromResults:
    def test_an_id_read_from_an_earlier_result_passes(self):
        tr = transcript(
            [
                call("search_graph", {"query": "x"}, turn=0, tool_use_id="r0"),
                call(
                    "update_node",
                    {"node_id": "eval-node-one", "updates": {}},
                    turn=1,
                    tool_use_id="w1",
                ),
            ],
            results={"r0": {"nodes": [{"id": "eval-node-one"}]}},
        )
        assert score_ids_resolved_from_results(tr).passed

    def test_a_correct_id_that_was_never_read_still_fails(self):
        """
        The behaviour the ID-first rule exists to catch.

        The id happens to be right, so the write succeeds and the model reports
        success. It guessed, and on the next graph the guess is wrong — which is
        precisely why "it worked" must not score as a pass.
        """
        tr = transcript(
            [call("update_node", {"node_id": "eval-node-one", "updates": {}}, turn=0)]
        )
        result = score_ids_resolved_from_results(tr)
        assert not result.passed
        assert "without being read first" in result.detail

    def test_an_id_from_a_later_result_does_not_count(self):
        """A result the model had not been shown yet cannot have informed the write."""
        tr = transcript(
            [
                call(
                    "update_node",
                    {"node_id": "eval-node-one", "updates": {}},
                    turn=0,
                    tool_use_id="w0",
                ),
                call("search_graph", {"query": "x"}, turn=1, tool_use_id="r1"),
            ],
            results={"r1": {"nodes": [{"id": "eval-node-one"}]}},
        )
        assert not score_ids_resolved_from_results(tr).passed

    def test_a_run_with_no_id_bearing_write_does_not_pass_vacuously(self):
        """Nothing exercised the rule, so there is nothing to credit."""
        tr = transcript([call("search_graph", {"query": "x"})])
        result = score_ids_resolved_from_results(tr)
        assert not result.passed
        assert "never exercised" in result.detail

    def test_list_valued_id_arguments_are_checked_element_by_element(self):
        tr = transcript(
            [
                call("search_graph", {"query": "x"}, turn=0, tool_use_id="r0"),
                call(
                    "delete_nodes",
                    {"node_ids": ["eval-node-one", "eval-node-invented"]},
                    turn=1,
                    tool_use_id="d1",
                ),
            ],
            results={"r0": {"nodes": [{"id": "eval-node-one"}]}},
        )
        result = score_ids_resolved_from_results(tr)
        assert not result.passed
        assert "eval-node-invented" in result.detail

    def test_an_edge_endpoint_created_in_the_same_add_nodes_call_is_allowed(self):
        """An edge may point at a node being created beside it."""
        tr = transcript(
            [
                call(
                    "add_nodes",
                    {
                        "nodes": [
                            {"id": "eval-fresh-node", "name": "New", "type": "Actor"}
                        ],
                        "edges": [
                            {"source": "eval-fresh-node", "target": "eval-fresh-node"}
                        ],
                    },
                    turn=0,
                )
            ]
        )
        assert score_ids_resolved_from_results(tr).passed

    def test_an_edge_may_name_a_node_being_created_in_the_same_call(self):
        """
        The schema-valid create-and-connect call.

        add_nodes' `nodes` item schema has NO id property — ids are
        server-generated — while edges[].source/target are "node ID or name".
        So the only legitimate way to connect a node being created is by its
        name. Collecting only ids charged every such call an ID-first failure,
        and the only passing route was to invent a client-side nodes[].id.
        """
        tr = transcript(
            [
                call("search_graph", {"query": "Handbook"}, turn=0, tool_use_id="r0"),
                call(
                    "add_nodes",
                    {
                        "nodes": [{"type": "Resource", "name": "New Handbook"}],
                        "edges": [
                            {
                                "source": "New Handbook",
                                "target": "eval-resource-metadata-handbook",
                                "type": "RELATES_TO",
                            }
                        ],
                    },
                    turn=1,
                    tool_use_id="w1",
                ),
            ],
            results={"r0": {"nodes": [{"id": "eval-resource-metadata-handbook"}]}},
        )
        result = score_ids_resolved_from_results(tr)
        assert result.passed, result.detail

    def test_an_edge_endpoint_that_is_neither_read_nor_created_fails(self):
        tr = transcript(
            [
                call(
                    "add_nodes",
                    {
                        "nodes": [
                            {"id": "eval-fresh-node", "name": "New", "type": "Actor"}
                        ],
                        "edges": [
                            {"source": "eval-fresh-node", "target": "eval-guessed-node"}
                        ],
                    },
                    turn=0,
                )
            ]
        )
        result = score_ids_resolved_from_results(tr)
        assert not result.passed
        assert "eval-guessed-node" in result.detail

    def test_an_id_nested_deep_in_a_result_still_counts_as_read(self):
        """Over-strict extraction would invent failures; seen anywhere is seen."""
        tr = transcript(
            [
                call("search_graph", {"query": "x"}, turn=0, tool_use_id="r0"),
                call(
                    "update_node",
                    {"node_id": "eval-node-deep", "updates": {}},
                    turn=1,
                    tool_use_id="w1",
                ),
            ],
            results={"r0": {"a": {"b": [{"c": ["eval-node-deep"]}]}}},
        )
        assert score_ids_resolved_from_results(tr).passed

    def test_an_id_seen_only_inside_a_result_message_counts_as_read(self):
        """
        Same rule as the answer-citation scorer: shown is shown.

        The two asked this differently before — an id the model only ever saw
        inside a status message counted as supported in its answer but not as
        read before a write.
        """
        tr = transcript(
            [
                call("search_graph", {"query": "x"}, turn=0, tool_use_id="r0"),
                call(
                    "update_node",
                    {"node_id": "eval-node-one", "updates": {}},
                    turn=1,
                    tool_use_id="w1",
                ),
            ],
            results={"r0": {"message": "Found node eval-node-one in the graph"}},
        )
        assert score_ids_resolved_from_results(tr).passed


class TestVerifyAfterWrite:
    def test_a_read_after_the_write_that_returns_the_node_passes(self):
        tr = transcript(
            [
                call(
                    "update_node",
                    {"node_id": "eval-node-one", "updates": {}},
                    turn=0,
                    tool_use_id="w0",
                ),
                call(
                    "get_related_nodes",
                    {"node_id": "eval-node-one"},
                    turn=1,
                    tool_use_id="r1",
                ),
            ],
            results={
                "w0": {"success": True},
                "r1": {"nodes": [{"id": "eval-node-one"}]},
            },
        )
        assert score_verify_after_write(
            tr, ["get_related_nodes", "search_graph"]
        ).passed

    def test_no_read_after_the_write_fails(self):
        tr = transcript(
            [
                call(
                    "update_node",
                    {"node_id": "eval-node-one", "updates": {}},
                    turn=0,
                    tool_use_id="w0",
                )
            ],
            results={"w0": {"success": True}},
        )
        assert not score_verify_after_write(tr, ["get_related_nodes"]).passed

    def test_a_read_before_the_write_does_not_count(self):
        tr = transcript(
            [
                call(
                    "get_related_nodes",
                    {"node_id": "eval-node-one"},
                    turn=0,
                    tool_use_id="r0",
                ),
                call(
                    "update_node",
                    {"node_id": "eval-node-one", "updates": {}},
                    turn=1,
                    tool_use_id="w1",
                ),
            ],
            results={
                "r0": {"nodes": [{"id": "eval-node-one"}]},
                "w1": {"success": True},
            },
        )
        assert not score_verify_after_write(tr, ["get_related_nodes"]).passed

    def test_a_read_that_does_not_return_the_written_node_does_not_count(self):
        tr = transcript(
            [
                call(
                    "update_node",
                    {"node_id": "eval-node-one", "updates": {}},
                    turn=0,
                    tool_use_id="w0",
                ),
                call(
                    "search_graph",
                    {"query": "something else"},
                    turn=1,
                    tool_use_id="r1",
                ),
            ],
            results={
                "w0": {"success": True},
                "r1": {"nodes": [{"id": "eval-node-other"}]},
            },
        )
        assert not score_verify_after_write(tr, ["search_graph"]).passed

    def test_verification_is_measured_from_the_last_write_not_the_first(self):
        """Verifying write 1 and then writing again unverified is not verified."""
        tr = transcript(
            [
                call(
                    "update_node",
                    {"node_id": "eval-a", "updates": {}},
                    turn=0,
                    tool_use_id="w0",
                ),
                call(
                    "get_related_nodes", {"node_id": "eval-a"}, turn=1, tool_use_id="r1"
                ),
                call(
                    "update_node",
                    {"node_id": "eval-b", "updates": {}},
                    turn=2,
                    tool_use_id="w2",
                ),
            ],
            results={
                "w0": {"success": True},
                "r1": {"nodes": [{"id": "eval-a"}]},
                "w2": {"success": True},
            },
        )
        assert not score_verify_after_write(tr, ["get_related_nodes"]).passed

    def test_a_failed_write_is_not_the_write_to_verify(self):
        """An errored write leaves the earlier successful one as the last write."""
        tr = transcript(
            [
                call(
                    "update_node",
                    {"node_id": "eval-a", "updates": {}},
                    turn=0,
                    tool_use_id="w0",
                ),
                call(
                    "update_node",
                    {"node_id": "eval-b", "updates": {}},
                    turn=1,
                    tool_use_id="w1",
                ),
                call(
                    "get_related_nodes", {"node_id": "eval-a"}, turn=2, tool_use_id="r2"
                ),
            ],
            results={
                "w0": {"success": True},
                "w1": {"error": "no such node"},
                "r2": {"nodes": [{"id": "eval-a"}]},
            },
        )
        assert score_verify_after_write(tr, ["get_related_nodes"]).passed

    def test_reading_back_only_the_node_an_edge_pointed_at_does_not_verify(self):
        """
        PD2: `written` must mean "created or changed", not "referenced".

        add_nodes' id-bearing arguments are its edges' endpoints — pre-existing
        nodes the call did NOT write, and since those may be names, a read-back
        could match an old node by name. So a model could create a node, read
        back only the node its new edge pointed at, and pass verification
        without the written node ever being read.
        """
        calls = [
            call("search_graph", {"query": "Office"}, turn=0, tool_use_id="r0"),
            call(
                "add_nodes",
                {
                    "nodes": [{"type": "Resource", "name": "New Handbook"}],
                    "edges": [
                        {
                            "source": "New Handbook",
                            "target": "National Statistics Office",
                        }
                    ],
                },
                turn=1,
                tool_use_id="w1",
            ),
            call("search_graph", {"query": "Office"}, turn=2, tool_use_id="r2"),
        ]
        shared = {
            "r0": {
                "nodes": [
                    {
                        "id": "eval-actor-statistics-office",
                        "name": "National Statistics Office",
                    }
                ]
            },
            "w1": {"success": True, "added_node_ids": ["srv-generated-id-abc"]},
        }

        read_the_old_node = transcript(
            calls,
            results={
                **shared,
                "r2": {
                    "nodes": [
                        {
                            "id": "eval-actor-statistics-office",
                            "name": "National Statistics Office",
                        }
                    ]
                },
            },
            advertised=["search_graph", "add_nodes"],
        )
        assert not score_verify_after_write(read_the_old_node, ["search_graph"]).passed

        read_the_new_node = transcript(
            calls,
            results={
                **shared,
                "r2": {
                    "nodes": [{"id": "srv-generated-id-abc", "name": "New Handbook"}]
                },
            },
            advertised=["search_graph", "add_nodes"],
        )
        assert score_verify_after_write(read_the_new_node, ["search_graph"]).passed

    def test_a_run_with_no_write_fails_rather_than_passing_vacuously(self):
        tr = transcript([call("search_graph", {"query": "x"})])
        result = score_verify_after_write(tr, ["search_graph"])
        assert not result.passed
        assert "no successful write" in result.detail

    def test_a_read_through_a_tool_outside_the_allowed_set_does_not_count(self):
        tr = transcript(
            [
                call(
                    "update_node",
                    {"node_id": "eval-a", "updates": {}},
                    turn=0,
                    tool_use_id="w0",
                ),
                call(
                    "get_related_nodes", {"node_id": "eval-a"}, turn=1, tool_use_id="r1"
                ),
            ],
            results={"w0": {"success": True}, "r1": {"nodes": [{"id": "eval-a"}]}},
        )
        assert not score_verify_after_write(tr, ["search_graph"]).passed

    def test_an_added_node_id_comes_from_the_write_result(self):
        """add_nodes generates ids, so the written id is only in the result."""
        tr = transcript(
            [
                call(
                    "add_nodes",
                    {"nodes": [{"name": "N", "type": "Actor"}], "edges": []},
                    turn=0,
                    tool_use_id="w0",
                ),
                call("search_graph", {"query": "N"}, turn=1, tool_use_id="r1"),
            ],
            results={
                "w0": {"success": True, "added_node_ids": ["generated-id-1234"]},
                "r1": {"nodes": [{"id": "generated-id-1234"}]},
            },
        )
        assert score_verify_after_write(tr, ["search_graph"]).passed


class TestFinalNodeState:
    GRAPH = {"nodes": [{"id": "eval-a", "name": "After", "summary": "New summary"}]}

    def test_matching_field_values_pass(self):
        tr = transcript(final_graph=self.GRAPH)
        assert score_final_node_state(tr, {"eval-a": {"summary": "New summary"}}).passed

    def test_a_differing_value_fails(self):
        tr = transcript(final_graph=self.GRAPH)
        result = score_final_node_state(tr, {"eval-a": {"summary": "Something else"}})
        assert not result.passed
        assert "expected" in result.detail

    def test_a_missing_node_fails(self):
        tr = transcript(final_graph=self.GRAPH)
        result = score_final_node_state(tr, {"eval-missing": {"name": "x"}})
        assert not result.passed
        assert "absent from the final graph" in result.detail


class TestFinalNodeFieldsChanged:
    FIXTURE = {
        "nodes": [
            {
                "id": "eval-a",
                "name": "Before",
                "description": "Before",
                "summary": "Before",
            }
        ]
    }

    def test_all_required_fields_changed_passes(self):
        tr = transcript(
            final_graph={
                "nodes": [
                    {
                        "id": "eval-a",
                        "name": "Efter",
                        "description": "Efter",
                        "summary": "Efter",
                    }
                ]
            }
        )
        assert score_final_node_fields_changed(
            tr, self.FIXTURE, {"eval-a": ["name", "description", "summary"]}
        ).passed

    def test_a_partial_change_fails_and_names_the_field_left_behind(self):
        """The partial write reported as complete — protocol rule 3's failure."""
        tr = transcript(
            final_graph={
                "nodes": [
                    {
                        "id": "eval-a",
                        "name": "Efter",
                        "description": "Before",
                        "summary": "Before",
                    }
                ]
            }
        )
        result = score_final_node_fields_changed(
            tr, self.FIXTURE, {"eval-a": ["name", "description", "summary"]}
        )
        assert not result.passed
        assert "description" in result.detail and "summary" in result.detail

    def test_a_deleted_node_fails(self):
        tr = transcript(final_graph={"nodes": []})
        assert not score_final_node_fields_changed(
            tr, self.FIXTURE, {"eval-a": ["name"]}
        ).passed

    def test_a_serializer_default_does_not_count_as_a_change(self):
        """
        The vacuous pass: the fixture omits the field, the serializer fills it.

        `None != []` would read as "changed" on a run in which the model did
        nothing at all, so a case naming subtypes/aliases/metadata passed
        against an empty transcript.
        """
        tr = transcript(
            final_graph={
                "nodes": [
                    {
                        "id": "eval-a",
                        "name": "Before",
                        "description": "Before",
                        "summary": "Before",
                        "subtypes": [],
                        "aliases": [],
                        "metadata": {},
                    }
                ]
            }
        )
        result = score_final_node_fields_changed(
            tr, self.FIXTURE, {"eval-a": ["subtypes", "aliases", "metadata"]}
        )
        assert not result.passed
        for field in ("subtypes", "aliases", "metadata"):
            assert field in result.detail

    def test_filling_an_absent_field_does_count_as_a_change(self):
        """The mirror case: "give this node a summary it lacks" is legitimate."""
        fixture = {"nodes": [{"id": "eval-a", "name": "N"}]}
        tr = transcript(
            final_graph={"nodes": [{"id": "eval-a", "name": "N", "summary": "Added"}]}
        )
        assert score_final_node_fields_changed(
            tr, fixture, {"eval-a": ["summary"]}
        ).passed


class TestAnswerEntitiesSupported:
    def test_a_cited_id_present_in_a_result_passes(self):
        tr = transcript(
            results={"r0": {"nodes": [{"id": "eval-initiative-metadata-register"}]}},
            final_text="It is eval-initiative-metadata-register.",
        )
        assert score_answer_entities_supported(tr).passed

    def test_a_fabricated_id_fails(self):
        tr = transcript(
            results={"r0": {"nodes": [{"id": "eval-initiative-metadata-register"}]}},
            final_text="It is eval-initiative-metadata-registry-programme.",
        )
        result = score_answer_entities_supported(tr)
        assert not result.passed
        assert "eval-initiative-metadata-registry-programme" in result.detail

    def test_an_answer_citing_no_id_passes_and_says_it_was_vacuous(self):
        tr = transcript(
            results={"r0": {}}, final_text="The handbook is produced there."
        )
        result = score_answer_entities_supported(tr)
        assert result.passed
        assert "vacuously" in result.detail

    def test_an_id_embedded_inside_a_longer_result_string_counts_as_supported(self):
        tr = transcript(
            results={
                "r0": {"message": "Updated node eval-resource-metadata-handbook ok"}
            },
            final_text="Done: eval-resource-metadata-handbook.",
        )
        assert score_answer_entities_supported(tr).passed

    def test_ordinary_hyphenated_prose_is_not_read_as_an_id(self):
        """Two-segment words are everywhere in prose."""
        tr = transcript(
            results={}, final_text="This is machine-readable, well-formed data."
        )
        assert score_answer_entities_supported(tr).passed

    @pytest.mark.parametrize(
        "phrase",
        ["up-to-date", "end-to-end", "state-of-the-art", "one-size-fits-all"],
    )
    def test_multi_segment_english_phrases_are_not_read_as_fabricated_ids(self, phrase):
        """
        Three segments alone does not distinguish an id from English.

        "the node is now up-to-date" must not be reported as citing an invented
        node. A false accusation here is indistinguishable from a real finding,
        which is exactly what the hallucination row refuses to risk.
        """
        tr = transcript(results={}, final_text=f"The node is now {phrase}.")
        result = score_answer_entities_supported(tr)
        assert result.passed, result.detail

    @pytest.mark.parametrize(
        "mention",
        [
            "I checked this on 2026-10-08.",
            "Resolved with gpt-4o-mini.",
            "See the left-hand-side panel.",
            "The graph is in read-only-mode.",
            "Counted 1-2-3 nodes.",
        ],
    )
    def test_a_date_version_or_compound_the_model_supplies_is_not_a_node(self, mention):
        """
        PD3: shape alone still accused these.

        "2026-10-08" and "gpt-4o-mini" are id-SHAPED — three-plus segments, no
        function word — so a correct answer that mentioned today's date failed a
        scored dimension. An open-vocabulary candidate must now also belong to
        this graph's id vocabulary, which a date does not.
        """
        fixture = {"nodes": [{"id": "eval-initiative-metadata-register"}]}
        tr = transcript(
            results={"r0": {"nodes": [{"id": "eval-initiative-metadata-register"}]}},
            final_text=f"Produced by eval-initiative-metadata-register. {mention}",
        )
        result = score_answer_entities_supported(tr, fixture)
        assert result.passed, result.detail

    def test_a_fabricated_id_sharing_the_graphs_prefix_is_still_caught(self):
        """The realistic fabrication: a near-miss of a real id."""
        fixture = {"nodes": [{"id": "eval-initiative-metadata-register"}]}
        tr = transcript(
            results={"r0": {"nodes": [{"id": "eval-initiative-metadata-register"}]}},
            final_text="Produced by eval-initiative-metadata-registry-programme.",
        )
        result = score_answer_entities_supported(tr, fixture)
        assert not result.passed
        assert "eval-initiative-metadata-registry-programme" in result.detail

    def test_a_fixture_id_cited_but_never_read_fails(self):
        """
        The closed-vocabulary half: a real node the run never looked at.

        No heuristic involved — the id is in the case's own fixture, so citing
        it without a tool result having returned it is unambiguous.
        """
        fixture = {"nodes": [{"id": "eval-actor-statistics-office"}]}
        tr = transcript(
            results={"r0": {"nodes": [{"id": "eval-resource-metadata-handbook"}]}},
            final_text="It belongs to eval-actor-statistics-office.",
        )
        result = score_answer_entities_supported(tr, fixture)
        assert not result.passed
        assert "eval-actor-statistics-office" in result.detail

    def test_a_fixture_id_that_was_read_passes(self):
        fixture = {"nodes": [{"id": "eval-actor-statistics-office"}]}
        tr = transcript(
            results={"r0": {"nodes": [{"id": "eval-actor-statistics-office"}]}},
            final_text="It belongs to eval-actor-statistics-office.",
        )
        assert score_answer_entities_supported(tr, fixture).passed


class TestIdTokenPattern:
    @pytest.mark.parametrize(
        "text",
        [
            "eval-initiative-metadata-register",
            "task-compare-skills-openai-open-models",
            "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
        ],
    )
    def test_matches_ids_this_system_writes(self, text):
        assert ID_TOKEN_RE.findall(text) == [text]

    @pytest.mark.parametrize(
        "text", ["machine-readable", "well-formed", "metadata", "AI-Act"]
    )
    def test_does_not_match_ordinary_prose(self, text):
        assert ID_TOKEN_RE.findall(text) == []


class TestIsIdShaped:
    @pytest.mark.parametrize(
        "token",
        [
            "eval-initiative-metadata-register",
            "task-compare-skills-openai-open-models",
            "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
            # Upper and mixed case: the only UUIDs that need the UUID branch.
            # A lowercase one also matches the slug alternative, so covering
            # only that one made the UUID branch removable unnoticed.
            "3F2504E0-4F89-11D3-9A0C-0305E82C3301",
            "3f2504E0-4f89-11d3-9A0c-0305e82c3301",
        ],
    )
    def test_accepts_ids_this_system_writes(self, token):
        assert is_id_shaped(token)

    @pytest.mark.parametrize(
        "token",
        [
            "3F2504E0-4F89-11D3-9A0C-0305E82C3301",
            "3f2504E0-4f89-11d3-9A0c-0305e82c3301",
        ],
    )
    def test_the_token_pattern_itself_finds_a_non_lowercase_uuid(self, token):
        """is_id_shaped never sees a token the pattern did not extract."""
        assert ID_TOKEN_RE.findall(f"the node {token} is here") == [token]

    @pytest.mark.parametrize(
        "token",
        [
            "up-to-date",
            "end-to-end",
            "state-of-the-art",
            "one-size-fits-all",
            "machine-readable",
            "metadata",
        ],
    )
    def test_rejects_english_prose(self, token):
        assert not is_id_shaped(token)

    @pytest.mark.parametrize("token", ["2026-10-08", "1-2-3", "0-0-0"])
    def test_rejects_an_all_numeric_token(self, token):
        """A date is not a node id, and a model states today's date freely."""
        assert not is_id_shaped(token)

    @pytest.mark.parametrize("token", ["gpt-4o-mini", "left-hand-side"])
    def test_a_token_that_is_shaped_like_an_id_but_is_not_one(self, token):
        """
        Shape is deliberately not the whole test.

        These pass the shape check, which is why the scorer also requires an
        open-vocabulary candidate to share a leading segment with an id the run
        has seen — see score_answer_entities_supported.
        """
        assert is_id_shaped(token)

    def test_rejects_a_real_id_containing_a_function_word_segment(self):
        """
        The documented false negative, pinned so it stays a known trade.

        An id like task-fix-edge-authentication-on-sspcloud is missed because
        "on" marks a token as prose. Biasing towards a miss is deliberate: the
        alternative is reporting an English phrase as a fabricated node.
        """
        assert not is_id_shaped("task-fix-edge-authentication-on-sspcloud")


class TestDiscriminatingFirstCall:
    def test_the_expected_first_call_passes(self):
        tr = transcript([call("search_graph", {"query": ""})])
        assert score_discriminating_first_call(tr, "search_graph").passed

    def test_a_different_first_call_fails_even_if_the_expected_one_follows(self):
        """Following the wrong skill first is not selecting the right one."""
        tr = transcript(
            [
                call("get_schema", {}, turn=0),
                call("search_graph", {"query": ""}, turn=1),
            ]
        )
        assert not score_discriminating_first_call(tr, "search_graph").passed

    def test_no_tool_calls_fails(self):
        assert not score_discriminating_first_call(transcript([]), "get_schema").passed


class TestNegativeExpectationsAreWired:
    """
    _align has unit tests; its WIRING at each dispatch site had none.

    Every shipped case declares its conditions as `true`, so replacing
    `_align(actual, expected)` with `actual` at any site changed nothing the
    suite could see — and _align's own docstring says its purpose is that a
    suite of only positive cases cannot tell a working scorer from one that
    always returns True. Driving score_case with negative expectations exercises
    the wiring without putting an artificial case in the shipped set.
    """

    def _case(self, **expectation):
        return AcceptanceCase(
            id="negative-probe",
            dimension="id_resolution",
            prompt="p",
            graph="metadata-pilot-small.json",
            expect=ExpectedBehaviour(**expectation),
            notes=(
                "probe case asserting that a violated condition is detected, which "
                "is what keeps a scorer that always returned True from passing"
            ),
        )

    def test_a_violated_condition_a_case_expected_to_fail_scores_as_a_pass(self):
        guessed = transcript(
            [call("update_node", {"node_id": "eval-node-one", "updates": {}})]
        )
        score = score_case(
            self._case(ids_resolved_from_results=False), guessed, TOOL_DEFS, {}
        )
        assert score.passed, [(c.name, c.detail) for c in score.conditions]
        assert score.dimensions["id_resolution"].passed is True

    def test_a_held_condition_a_case_expected_to_fail_scores_as_a_failure(self):
        """The mirror: bypassing _align would make this pass."""
        resolved = transcript(
            [
                call("search_graph", {"query": "x"}, turn=0, tool_use_id="r0"),
                call(
                    "update_node",
                    {"node_id": "eval-node-one", "updates": {}},
                    turn=1,
                    tool_use_id="w1",
                ),
            ],
            results={"r0": {"nodes": [{"id": "eval-node-one"}]}},
        )
        score = score_case(
            self._case(ids_resolved_from_results=False), resolved, TOOL_DEFS, {}
        )
        assert not score.passed
        assert score.dimensions["id_resolution"].passed is False

    @pytest.mark.parametrize(
        "field,dimension",
        [
            ("tool_calls_valid", "tool_call_validity"),
            ("verify_after_write", "post_write_verification"),
            ("answer_entities_supported", "unsupported_entity_reference"),
        ],
    )
    def test_every_polarity_bearing_dispatch_site_honours_the_expectation(
        self, field, dimension
    ):
        """Each site that passes through _align, pinned against a violation."""
        violating = transcript([], final_text="")
        case = AcceptanceCase(
            id="negative-probe",
            dimension=dimension,
            prompt="p",
            graph="metadata-pilot-small.json",
            expect=ExpectedBehaviour(**{field: False}),
            notes=(
                "probe case asserting the harness detects a violation of this "
                "condition rather than reporting the scorer's raw verdict"
            ),
        )
        fixture = {}
        if field == "answer_entities_supported":
            # The id must be free of function-word segments, AND the run must
            # have seen some id sharing its leading segment — otherwise the
            # token is correctly not treated as a node reference at all.
            fixture = {"nodes": [{"id": "eval-initiative-metadata-register"}]}
            violating = transcript(
                results={
                    "r0": {"nodes": [{"id": "eval-initiative-metadata-register"}]}
                },
                final_text="See eval-fabricated-node-reference.",
            )
        score = score_case(case, violating, TOOL_DEFS, fixture)
        assert score.passed, [(c.name, c.detail) for c in score.conditions]


class TestAlign:
    def test_a_condition_expected_to_hold_passes_when_it_holds(self):
        inner = ConditionResult("c", True, "held")
        assert _align(inner, True).passed

    def test_a_condition_expected_to_fail_passes_when_it_fails(self):
        """Negative polarity: a case may pin that a violation is detected."""
        inner = ConditionResult("c", False, "did not hold")
        aligned = _align(inner, False)
        assert aligned.passed
        assert aligned.detail == "did not hold"

    def test_a_condition_expected_to_fail_does_not_pass_when_it_holds(self):
        assert not _align(ConditionResult("c", True, "held"), False).passed
