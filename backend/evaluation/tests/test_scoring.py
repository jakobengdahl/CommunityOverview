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
    CaseScore,
    _normalise_field,
    is_id_shaped,
    score_case,
    _align,
    ConditionResult,
    score_answer_cites_ids,
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
from backend.evaluation.cases import DEFAULT_VERIFY_READ_TOOLS, WRITE_TOOLS
from backend.evaluation.scoring import _join_problems
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


class TestEveryQuotedValueIsBounded:
    """
    PD2: the docs say a quoted argument is bounded too, and it was not.

    `_abbreviate` was applied at one of the sites that quote model-written
    content. jsonschema's message embeds the offending instance, and the
    ID-first detail quoted the raw argument — both reach a report.
    """

    LONG = "ZZQQ" * 800  # ~3.2 kB of model-written value

    @pytest.mark.parametrize(
        "site",
        ["tool_name", "argument_name", "sequence", "first_call", "cited_token"],
    )
    def test_every_site_that_quotes_a_name_is_bounded(self, site):
        """
        The previous probe put the oversized string in an argument VALUE under a
        short key, so none of the NAME sites was exercised — and five of them
        were unbounded, one reaching 5 kB.
        """
        huge = "Z" * 2500
        if site == "tool_name":
            tr = transcript([call(huge, {}, turn=0)])
            detail = score_tool_calls_valid(tr, TOOL_DEFS).detail
        elif site == "argument_name":
            tr = transcript([call("search_graph", {"query": "x", huge: 1}, turn=0)])
            detail = score_tool_calls_valid(tr, TOOL_DEFS).detail
        elif site == "sequence":
            tr = transcript([call(huge, {}, turn=0)])
            detail = score_required_call_sequence(tr, ["search_graph"]).detail
        elif site == "first_call":
            tr = transcript([call(huge, {}, turn=0)])
            detail = score_discriminating_first_call(tr, "get_schema").detail
        else:
            tr = transcript(
                results={"r0": {"nodes": [{"id": f"eval-{huge}"}]}},
                final_text=f"It is eval-{huge}-other.",
            )
            detail = score_answer_entities_supported(
                tr, {"nodes": [{"id": f"eval-{huge}"}]}
            ).detail
        assert len(detail) < 1000, f"{site} detail is {len(detail)} chars"

    @pytest.mark.parametrize("count", [1, 5, 60])
    def test_a_detail_with_many_entries_is_bounded_too(self, count):
        """
        Per-entry bounds are not a bounded detail, and the count is the
        model's.

        Each unresolved id is capped at 80 characters and nothing capped how
        many there could be — one per reference the model failed to resolve.
        Five guessed edge endpoints gave 537 characters, past the 500-character
        leaf invariant this suite asserts of every report string; 60 gave 6037,
        which is also 60 × 80 characters of free-form model text reconstructable
        from the file `--out` writes. The probes for this family only ever made
        one entry.
        """
        edges = [
            {
                "source": f"guessed-source-node-{index:03d}",
                "target": f"guessed-target-node-{index:03d}",
            }
            for index in range(count)
        ]
        tr = transcript([call("add_nodes", {"nodes": [], "edges": edges}, turn=0)])
        detail = score_ids_resolved_from_results(tr).detail
        assert len(detail) < 500, f"{count} entries gave {len(detail)} chars"

    def test_the_passing_path_bounds_its_quoted_list_as_the_failing_one_does(self):
        """
        Two scorers abbreviated on failure and interpolated raw on success.

        `score_required_call_sequence` quoted the whole model-chosen tool-call
        list, and its own failure path five lines below already abbreviated it;
        the parametrised bound probe reached only the failure path.
        """
        tr = transcript(
            [
                call("search_graph", {"query": "x"}, turn=0),
                call("Z" * 3000, {}, turn=1),
            ]
        )
        result = score_required_call_sequence(tr, ["search_graph"])
        assert result.passed, "this probe must reach the PASS path"
        assert len(result.detail) < 500, len(result.detail)

    def test_a_schema_error_quoting_a_huge_value_is_bounded(self):
        """
        The oversized value has to be the one that violates the schema.

        The previous probe put it in `query` and the type error in `limit`, so
        jsonschema reported the `limit` error and the detail never quoted the
        oversized value at all: a 53-character detail passing a 1000-character
        bound without reaching the branch it names. Asserting the marker is
        present and the whole value is not keeps it from going vacuous again.
        """
        tr = transcript([call("search_graph", {"query": "x", "limit": self.LONG})])
        detail = score_tool_calls_valid(tr, TOOL_DEFS).detail
        assert "ZZQQ" in detail, "the probe no longer reaches the quoted instance"
        assert self.LONG not in detail, "the whole instance was quoted"
        assert len(detail) < 1000, len(detail)

    def test_an_unresolved_id_detail_is_bounded(self):
        tr = transcript(
            [call("update_node", {"node_id": self.LONG, "updates": {}}, turn=0)]
        )
        detail = score_ids_resolved_from_results(tr).detail
        assert len(detail) < 1000, len(detail)

    def test_a_written_id_detail_is_bounded(self):
        tr = transcript(
            [
                call(
                    "update_node",
                    {"node_id": self.LONG, "updates": {}},
                    turn=0,
                    tool_use_id="w0",
                )
            ],
            results={"w0": {"success": True}},
        )
        detail = score_verify_after_write(tr, ["search_graph"]).detail
        assert len(detail) < 1000, len(detail)


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

    def test_making_no_tool_calls_at_all_is_not_restraint(self):
        """
        TD2: the one scorer of its family that could pass vacuously.

        Its three siblings each refuse this explicitly. A case declaring only
        forbidden_calls would have scored skill_adherence as passed against a
        model that did nothing whatsoever.
        """
        result = score_forbidden_calls(transcript([]), ["update_node"])
        assert not result.passed
        assert "no tool calls at all" in result.detail

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

    @pytest.mark.parametrize("field", ["type", "description", "summary"])
    def test_an_edge_endpoint_matching_a_same_call_nodes_other_field_still_fails(
        self, field
    ):
        """
        Only `id` and `name` identify a node being created in the same call.

        Accepting every string in the node dict would let an edge endpoint that
        happens to equal the new node's `type` ("Initiative") pass the ID-first
        check — widening exactly the allowance the function is careful to bound.
        """
        node = {
            "type": "Initiative",
            "name": "New Thing",
            "description": "D",
            "summary": "S",
        }
        tr = transcript(
            [
                call("search_graph", {"query": "x"}, turn=0, tool_use_id="r0"),
                call(
                    "add_nodes",
                    {
                        "nodes": [node],
                        "edges": [{"source": node[field], "target": "eval-node-one"}],
                    },
                    turn=1,
                    tool_use_id="w1",
                ),
            ],
            results={"r0": {"nodes": [{"id": "eval-node-one"}]}},
        )
        result = score_ids_resolved_from_results(tr)
        assert not result.passed, f"{field} was accepted as a node reference"
        assert node[field] in result.detail

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

    def test_a_failed_presence_write_blames_the_model_not_the_case(self):
        """
        PD5: the inverse of the misattribution the previous fix removed.

        If the model attempts a presence-verifiable write that FAILS and a
        removal succeeds, the case is correct and the model botched its write —
        but the detail said the case was mis-specified.
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
                call(
                    "delete_nodes",
                    {"node_ids": ["eval-node-one"], "confirmed": True},
                    turn=2,
                    tool_use_id="w2",
                ),
            ],
            results={
                "r0": {"nodes": [{"id": "eval-node-one"}]},
                "w1": {"success": False, "error": "validation failed"},
                "w2": {"success": True},
            },
        )
        result = score_verify_after_write(tr, ["search_graph"])
        assert not result.passed
        assert "did not succeed" in result.detail
        assert "mis-specified" not in result.detail, (
            "a failed model write must not be reported as the case's fault"
        )

    def test_a_removal_is_reported_as_a_mis_specified_case_not_a_model_failure(self):
        """
        PD5: this is a PRESENCE check, so it cannot verify a deletion.

        A model that deletes a node and then reads back to confirm it is gone
        has verified correctly — and was scored as having failed, with a detail
        that read like the model's fault. The dimension is defined only for
        writes that leave the node readable, and a case pointing it at a removal
        now says so.
        """
        tr = transcript(
            [
                call("search_graph", {"query": "x"}, turn=0, tool_use_id="r0"),
                call(
                    "delete_nodes",
                    {"node_ids": ["eval-node-one"], "confirmed": True},
                    turn=1,
                    tool_use_id="w1",
                ),
                call("search_graph", {"query": "x"}, turn=2, tool_use_id="r2"),
            ],
            results={
                "r0": {"nodes": [{"id": "eval-node-one"}]},
                "w1": {"success": True},
                "r2": {"nodes": [], "total": 0},
            },
        )
        result = score_verify_after_write(tr, ["search_graph"])
        assert not result.passed
        assert "mis-specified" in result.detail
        assert "delete_nodes" in result.detail

    @pytest.mark.parametrize(
        "tool,arguments",
        [
            ("archive_nodes", {"node_ids": ["eval-node-one"]}),
            ("delete_edges", {"edge_ids": ["eval-edge-one"], "confirmed": True}),
        ],
    )
    def test_every_removal_tool_is_treated_the_same_way(self, tool, arguments):
        tr = transcript(
            [
                call("search_graph", {"query": "x"}, turn=0, tool_use_id="r0"),
                call(tool, arguments, turn=1, tool_use_id="w1"),
            ],
            results={
                "r0": {
                    "nodes": [{"id": "eval-node-one"}],
                    "edges": [{"id": "eval-edge-one"}],
                },
                "w1": {"success": True},
            },
        )
        result = score_verify_after_write(tr, ["search_graph"])
        assert not result.passed
        assert "mis-specified" in result.detail

    def test_an_unarchive_is_still_presence_verifiable(self):
        """It leaves the node readable again, so the presence check applies."""
        tr = transcript(
            [
                call("search_graph", {"query": "x"}, turn=0, tool_use_id="r0"),
                call(
                    "unarchive_nodes",
                    {"node_ids": ["eval-node-one"]},
                    turn=1,
                    tool_use_id="w1",
                ),
                call("search_graph", {"query": "x"}, turn=2, tool_use_id="r2"),
            ],
            results={
                "r0": {"nodes": [{"id": "eval-node-one"}]},
                "w1": {"success": True},
                "r2": {"nodes": [{"id": "eval-node-one"}]},
            },
        )
        assert score_verify_after_write(tr, ["search_graph"]).passed

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


class TestAWriteIsNeverItsOwnVerification:
    """
    Widening the read-tool allowlist to include the write tools survived.

    `allowed = set(read_tools) | set(WRITE_TOOLS)` then passed the shipped
    `post-write-verification` case on a model that wrote, archived the same
    node, and said "Saved and archived." No read ran after the write at all,
    and the presence check reported green on a node the run had just archived.
    Every bad-model probe ended in prose, so nothing drove a post-write call
    that was itself a write.
    """

    def test_the_default_read_tools_are_disjoint_from_the_write_tools(self):
        assert not set(DEFAULT_VERIFY_READ_TOOLS) & set(WRITE_TOOLS)

    def test_a_second_write_does_not_count_as_reading_back(self):
        node = "eval-resource-metadata-handbook"
        tr = transcript(
            [
                call(
                    "update_node",
                    {"node_id": node, "updates": {"summary": "x"}},
                    turn=0,
                ),
                call("archive_nodes", {"node_ids": [node]}, turn=1),
            ],
            results={"r0": {"id": node}, "r1": {"archived_node_ids": [node]}},
            final_text="Saved and archived.",
        )
        result = score_verify_after_write(tr, list(DEFAULT_VERIFY_READ_TOOLS))
        assert not result.passed, (
            f"a write after a write was credited as the read-back: {result.detail}"
        )


class TestNoScorerReadsTheModelsProseForAnImpression:
    """
    The module docstring's central claim, which no test pinned.

    Crediting `verify_after_write` when the answer merely SAYS the write was
    verified survived the whole suite — and the shipped case then passed
    end-to-end with no read-back at all, on a model that wrote and then
    announced "Confirmed: the change is saved." That is a scorer grading a
    claim instead of a behaviour, which is the one thing `dimensions.py` calls
    unmechanical and refuses to ship.

    Closing it at that one site was not enough: the same fallback in
    `score_required_call_sequence` then passed the case whose whole purpose is
    "did the model halt on an ambiguous name instead of guessing", on a run
    where `search_graph` was never called — a full case pass bought with one
    sentence of model prose. So the property is asserted over the module: for
    every condition except the two DEFINED over the answer, the verdict must
    be identical whether `final_text` is empty or stuffed with every tool
    name, every id and every claim a model might make.
    """

    ANSWER_DEFINED = {"answer_cites_ids", "answer_entities_supported"}

    def test_final_text_cannot_change_any_other_conditions_verdict(self):
        from backend.evaluation.cases import AcceptanceCase, ExpectedBehaviour
        from backend.evaluation.scoring import score_case

        stuffed = (
            "I called search_graph, then get_related_nodes, then update_node, "
            "and verified it. Confirmed: eval-resource-metadata-handbook and "
            "eval-actor-statistics-office and eval-initiative-metadata-register "
            "are all correct and I did not call archive_nodes or delete_nodes."
        )
        case = AcceptanceCase(
            id="prose-immunity-probe",
            dimension="skill_adherence",
            prompt="p",
            graph="metadata-pilot-ambiguous-names.json",
            expect=ExpectedBehaviour(
                tool_calls_valid=True,
                required_call_sequence=["search_graph", "get_related_nodes"],
                forbidden_calls=["update_node", "archive_nodes"],
                verify_after_write=True,
                discriminating_first_call="search_graph",
                # The three the first version of this probe left out, which
                # made the docstring's "every condition that is not defined
                # over the answer" false and left a third of the property
                # unguarded: a prose fallback in any of these would have
                # survived.
                ids_resolved_from_results=True,
                final_node_state={
                    "eval-resource-metadata-handbook": {"summary": "a real summary"}
                },
                final_node_fields_changed={
                    "eval-resource-metadata-handbook": ["summary"]
                },
            ),
            notes=(
                "probe declaring every condition that is not defined over the "
                "answer, so prose immunity can be asserted as a property"
            ),
        )
        fixture = {"nodes": [{"id": "eval-resource-metadata-handbook"}], "edges": []}
        made = transcript([call("get_schema", {}, turn=0)], final_text="")
        claimed = transcript([call("get_schema", {}, turn=0)], final_text=stuffed)

        quiet = {
            c.name: c.passed for c in score_case(case, made, [], fixture).conditions
        }
        loud = {
            c.name: c.passed for c in score_case(case, claimed, [], fixture).conditions
        }
        # The probe must cover every condition that is NOT defined over the
        # answer, or the property is asserted over a subset while claiming the
        # module. Derived from the model so a newly added condition fails here
        # until it is either declared or named as answer-defined.
        every = set(ExpectedBehaviour.model_fields) - {"verify_read_tools"}
        assert set(quiet) | self.ANSWER_DEFINED >= every, (
            "conditions outside this probe: "
            f"{sorted(every - set(quiet) - self.ANSWER_DEFINED)}"
        )
        assert quiet, "the probe declared no condition"
        for name, verdict in quiet.items():
            if name in self.ANSWER_DEFINED:
                continue
            assert loud[name] == verdict, (
                f"{name} changed verdict when the answer claimed the behaviour: "
                f"{verdict} -> {loud[name]}"
            )

    def test_an_answer_claiming_verification_without_a_read_still_fails(self):
        tr = transcript(
            [
                call(
                    "update_node",
                    {"node_id": "eval-resource-metadata-handbook", "updates": {}},
                    turn=0,
                )
            ],
            results={"r0": {"id": "eval-resource-metadata-handbook"}},
            final_text="Confirmed: the change is saved and I verified it.",
        )
        result = score_verify_after_write(
            tr, ["search_graph", "get_related_nodes", "find_similar_nodes"]
        )
        assert not result.passed
        # The verdict is the pin. An earlier assertion here tried to show the
        # detail did not quote the answer, via
        # `"verified" not in detail.lower().replace("verify","")` — which can
        # never fail, since the replace cannot touch "verified" and the scorer
        # never interpolates `final_text` at all.
        assert "search_graph" in result.detail and "update_node" in result.detail


class TestJoinProblems:
    """
    The joined detail's own bound, which no test covered.

    `_join_problems` existed to stop a detail growing with the NUMBER of
    problems — model-controlled, one per tool call — and nothing asserted it
    held. Raising its limit from 400 to 4000 left the suite green, because the
    report-side probe generates two or three problems and never the five that
    produced the original overrun.
    """

    @pytest.mark.parametrize("count", [1, 2, 5, 12, 40])
    @pytest.mark.parametrize("size", [10, 130, 196, 198, 900])
    def test_the_result_never_exceeds_the_limit(self, count, size):
        result = _join_problems(["x" * size] * count)
        assert len(result) <= 400, f"{count}x{size} gave {len(result)}"

    def test_one_oversized_problem_still_leaves_content(self):
        """
        Appending the suffix after the budget was spent overran the limit, and
        breaking on the first problem left a detail that was only "; and N
        more" — reachable from case-authored text, not just from a model.
        """
        result = _join_problems(["y" * 5000, "second", "third"])
        assert len(result) <= 400
        assert result.startswith("y")
        assert result.endswith("and 2 more")

    def test_everything_fits_when_it_fits(self):
        result = _join_problems(["one", "two"])
        assert result == "one; two"
        assert "more" not in result


class TestFinalNodeStateIsNotSatisfiedByAnIdleModel:
    """
    The mirror of the pin its sibling already has.

    `score_final_node_fields_changed` is guarded three ways against crediting
    a serializer default as a change. `score_final_node_state` had no such
    coverage, because the only shipped case declaring it names a field the
    fixture already fills — so normalising the comparison "for consistency"
    (`got = _normalise_field(node.get(key)); if got is not None and got !=
    want`) passed a case asking the model to fill an EMPTY field on a run
    where the model wrote nothing. "Fill in the field that is empty" is the
    most natural completeness case there is, so this is live for the next case
    added rather than hypothetical.
    """

    @pytest.mark.parametrize("fixture_value", ["", None])
    def test_an_empty_or_absent_field_is_not_already_as_expected(self, fixture_value):
        node = {"id": "eval-resource-metadata-handbook"}
        if fixture_value is not None:
            node["summary"] = fixture_value
        tr = transcript([], final_text="I did nothing at all.")
        tr.final_graph = {"nodes": [dict(node)], "edges": []}

        result = score_final_node_state(
            tr, {"eval-resource-metadata-handbook": {"summary": "a real summary"}}
        )
        assert not result.passed
        assert "summary" in result.detail


class TestAnswerCitesIds:
    """
    The positive half of the entity check, which the pure negative cannot be.

    `answer_entities_supported` passes an answer that cites nothing, because an
    answer citing nothing cites nothing unsupported. That is the right reading
    of the condition — and it made the one shipped case for the dimension green
    on a model that ran one valid read and then said it could not tell, on a
    prompt asking for the node id exactly as stored. Inverting the negative was
    not the fix: a family of no-false-positive tests legitimately asserts that
    ordinary prose does not trip the detector, and those answers cite no id
    either. The case needed a condition that fails on a non-answer.
    """

    def test_an_answer_stating_the_id_passes(self):
        tr = transcript(final_text="It is eval-initiative-metadata-register.")
        result = score_answer_cites_ids(tr, ["eval-initiative-metadata-register"])
        assert result.passed

    def test_an_id_in_backticks_or_a_list_still_counts(self):
        tr = transcript(final_text="- `eval-initiative-metadata-register`\n")
        assert score_answer_cites_ids(tr, ["eval-initiative-metadata-register"]).passed

    def test_a_non_answer_fails_and_names_the_missing_id(self):
        tr = transcript(final_text="I could not determine the node id.")
        result = score_answer_cites_ids(tr, ["eval-initiative-metadata-register"])
        assert not result.passed
        assert "eval-initiative-metadata-register" in result.detail

    def test_an_empty_answer_fails_and_says_so(self):
        result = score_answer_cites_ids(
            transcript(final_text=""), ["eval-initiative-metadata-register"]
        )
        assert not result.passed
        assert "empty" in result.detail

    def test_a_differently_cased_id_is_not_the_stored_one(self):
        """The prompt asks for the id "exactly as stored"."""
        tr = transcript(final_text="It is EVAL-Initiative-Metadata-Register.")
        assert not score_answer_cites_ids(
            tr, ["eval-initiative-metadata-register"]
        ).passed

    def test_an_id_the_run_only_READ_does_not_satisfy_the_condition(self):
        """
        It reads the answer and nothing else — which is the whole point.

        A mutation widened the haystack to the tool results as well, and every
        test here built a transcript with empty results, so none could see it.
        The shipped case then passed with the model reading the node and then
        declining to answer: `answer_entities_supported` vacuously supported,
        `answer_cites_ids` satisfied from the result it had read — exactly the
        vacuous pass this condition was added to close, restored.
        """
        tr = transcript(
            results={"r0": {"nodes": [{"id": "eval-initiative-metadata-register"}]}},
            final_text="I read the graph but cannot say which one it is.",
        )
        result = score_answer_cites_ids(tr, ["eval-initiative-metadata-register"])
        assert not result.passed
        assert "eval-initiative-metadata-register" in result.detail

    def test_a_bounded_detail_when_many_ids_are_missing(self):
        result = score_answer_cites_ids(
            transcript(final_text="no"), [f"eval-node-{i:04d}-x" for i in range(200)]
        )
        assert not result.passed
        assert len(result.detail) < 400, len(result.detail)


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

    REAL_RESULT = {
        "nodes": [
            {
                "id": "eval-actor-statistics-office",
                "name": "National Statistics Office",
                "tags": ["open-data-standard", "high-priority"],
                "created_at": "2026-10-08T22:39:02.240070+00:00",
                "updated_at": "2026-10-08T22:39:02.240071+00:00",
            }
        ],
        "total": 1,
    }

    @pytest.mark.parametrize(
        "mention,why",
        [
            (
                "I checked the 2026-10-08-snapshot of the register.",
                "every result carries ISO timestamps, which are id-SHAPED, so the "
                "vocabulary absorbed the prefix 2026 on every single case",
            ),
            (
                "The office follows an open-source-first policy.",
                "a THREE-segment tag is id-shaped too, which reopened the exact "
                "example the previous fix was written against",
            ),
        ],
    )
    def test_shape_alone_does_not_qualify_a_prefix_donor(self, mention, why):
        """
        Narrowing prefix donation by SHAPE was not enough.

        `is_id_shaped` says True for "open-data-standard" and for an ISO
        timestamp, so a shape test cannot separate an id from a tag or a date.
        Donors now come from id-bearing result keys instead. Driven with the
        real search_graph result shape, timestamps included.
        """
        fixture = {"nodes": [{"id": "eval-actor-statistics-office"}]}
        tr = transcript(
            results={"r0": self.REAL_RESULT},
            final_text=f"It is eval-actor-statistics-office. {mention}",
        )
        result = score_answer_entities_supported(tr, fixture)
        assert result.passed, f"{why}\n{result.detail}"

    def test_a_fabrication_is_still_caught_against_the_real_result_shape(self):
        fixture = {"nodes": [{"id": "eval-actor-statistics-office"}]}
        tr = transcript(
            results={"r0": self.REAL_RESULT},
            final_text="It is eval-actor-statistics-bureau.",
        )
        result = score_answer_entities_supported(tr, fixture)
        assert not result.passed
        assert "eval-actor-statistics-bureau" in result.detail

    def test_a_bare_word_in_a_result_does_not_donate_an_id_prefix(self):
        """
        `_all_strings` collects dict KEYS too — "id", "type", "name", "source".

        Without the hyphen guard those bare words donate prefixes, and a prose
        phrase like "the id-first-rule" is reported as a fabricated node. The
        reviewer classed this as outside the stated guarantees because widening
        can only make the scorer stricter; it is pinned anyway, because a false
        accusation is the failure this signal has now been corrected for twice.
        """
        fixture = {"nodes": [{"id": "eval-actor-statistics-office"}]}
        tr = transcript(
            results={"r0": {"nodes": [{"id": "eval-actor-statistics-office"}]}},
            final_text="We applied the id-first-rule and the type-safe-path.",
        )
        result = score_answer_entities_supported(tr, fixture)
        assert result.passed, result.detail

    def test_an_ordinary_hyphenated_tag_does_not_make_prose_look_like_a_node(self):
        """
        PD1: the vocabulary was built from every string a result contained.

        A node tagged "open-data" donated the prefix "open", and the answer's
        "open-source-first" was then reported as a fabricated node — the false
        accusation the vocabulary rule exists to prevent, moved one step out
        rather than removed. Only id-shaped values may donate a prefix.
        """
        fixture = {
            "nodes": [
                {
                    "id": "eval-actor-statistics-office",
                    "tags": ["open-data", "high-priority"],
                }
            ]
        }
        tr = transcript(
            results={
                "r0": {
                    "nodes": [
                        {
                            "id": "eval-actor-statistics-office",
                            "tags": ["open-data", "high-priority"],
                        }
                    ]
                }
            },
            final_text=(
                "The office runs an open-source-first programme and a "
                "high-trust-low-cost model."
            ),
        )
        result = score_answer_entities_supported(tr, fixture)
        assert result.passed, result.detail

    def test_a_fabricated_uuid_is_caught_and_not_passed_vacuously(self):
        """
        A UUID needs the scorer's own escape, which nothing covered.

        A UUID's leading segment is eight hex characters and is never an
        id-vocabulary prefix, so without the explicit escape it is filtered out
        of the candidates, `checked` comes back empty, and the scorer returns
        PASS with "cites no node id (vacuously supported)" — the whole
        open-vocabulary half dead for UUID ids.
        """
        fabricated = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
        fixture = {"nodes": [{"id": "eval-initiative-metadata-register"}]}
        tr = transcript(
            results={"r0": {"nodes": [{"id": "eval-initiative-metadata-register"}]}},
            final_text=f"It is node {fabricated}.",
        )
        result = score_answer_entities_supported(tr, fixture)
        assert not result.passed, result.detail
        assert fabricated in result.detail
        assert "vacuously" not in result.detail

    def test_a_uuid_the_run_did_return_passes(self):
        real = "3f2504e0-4f89-11d3-9a0c-0305e82c3301"
        tr = transcript(
            results={"r0": {"nodes": [{"id": real}]}},
            final_text=f"It is node {real}.",
        )
        assert score_answer_entities_supported(tr, {"nodes": [{"id": real}]}).passed

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
        # The id must be one the OPEN half cannot see, or this passes without
        # the closed half existing: "on" is a function-word segment, so
        # is_id_shaped rejects it and only the fixture vocabulary can catch it.
        unreachable_by_shape = "task-fix-edge-authentication-on-sspcloud"
        assert not is_id_shaped(unreachable_by_shape), "premise of this test"

        fixture = {"nodes": [{"id": unreachable_by_shape}]}
        tr = transcript(
            results={"r0": {"nodes": [{"id": "eval-resource-metadata-handbook"}]}},
            final_text=f"It belongs to {unreachable_by_shape}.",
        )
        result = score_answer_entities_supported(tr, fixture)
        assert not result.passed
        assert unreachable_by_shape in result.detail

    def test_without_a_fixture_graph_the_closed_half_cannot_help(self):
        """The same citation goes unnoticed, which is what makes the half load-bearing."""
        unreachable_by_shape = "task-fix-edge-authentication-on-sspcloud"
        tr = transcript(
            results={"r0": {"nodes": [{"id": "eval-resource-metadata-handbook"}]}},
            final_text=f"It belongs to {unreachable_by_shape}.",
        )
        assert score_answer_entities_supported(tr, None).passed

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


class TestCaseScorePassedGuards:
    """
    Two guards that covered each other, so each could be deleted unnoticed.

    score_case sets conditions=[] on a run error, so the truthiness guard hid a
    missing run_error check and the run_error check hid a missing truthiness
    guard. Drop both and a run that never happened reads as a pass. Asserted on
    CaseScore directly, where neither can stand in for the other.
    """

    def test_a_score_with_no_conditions_has_not_passed(self):
        assert CaseScore(case_id="x", dimension="completeness").passed is False

    def test_a_score_with_a_run_error_has_not_passed_even_if_conditions_held(self):
        score = CaseScore(
            case_id="x",
            dimension="completeness",
            conditions=[ConditionResult("c", True, "held")],
            run_error="the provider was unreachable",
        )
        assert score.passed is False

    def test_a_score_with_held_conditions_and_no_error_has_passed(self):
        score = CaseScore(
            case_id="x",
            dimension="completeness",
            conditions=[ConditionResult("c", True, "held")],
        )
        assert score.passed is True


class TestNormaliseFieldBoundary:
    """
    Where "empty" stops. The same function's gap produced the `archived`
    vacuous pass, so the boundary is pinned in both directions.
    """

    @pytest.mark.parametrize("value", [None, [], {}, ""])
    def test_absent_and_empty_collapse_together(self, value):
        assert _normalise_field(value) is None

    @pytest.mark.parametrize("value", [0, 0.0, False, [0], {"a": 0}, "0"])
    def test_a_real_value_that_merely_looks_empty_is_preserved(self, value):
        """Collapsing 0 or False would turn a genuine change into "unchanged"."""
        assert _normalise_field(value) is not None


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
