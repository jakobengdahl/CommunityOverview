"""
Tests for the runner: the shipped cases, end to end, against a scripted model.

These run the real ChatService — the product's system prompt, tool definitions
and tool-execution loop — with only the provider replaced. That is what makes
them worth having: a case that scores correctly here would score correctly
against a real model, because everything between the prompt and the score is
the same code.
"""

import json

from backend.evaluation.runner import (
    build_report,
    build_skills_context,
    run_case,
    run_suite,
)


def _factory(turns):
    from backend.evaluation.tests.conftest import ScriptedProvider

    provider = ScriptedProvider(turns)
    return lambda _profile: provider


class TestRunCaseGoodModel:
    def test_a_model_following_the_protocol_passes_the_id_resolution_case(
        self, profile, case_by_id
    ):
        score = run_case(
            case_by_id["id-resolution-before-write"],
            profile,
            provider_factory=_factory(
                [
                    [("search_graph", {"query": "Metadata Register Modernisation"})],
                    [
                        (
                            "update_node",
                            {
                                "node_id": "eval-initiative-metadata-register",
                                "updates": {
                                    "tags": ["metadata", "modernisation", "priority"]
                                },
                            },
                        )
                    ],
                    "Tag added to eval-initiative-metadata-register.",
                ]
            ),
        )
        assert score.passed, [(c.name, c.detail) for c in score.conditions]
        assert score.dimensions["id_resolution"].passed is True

    def test_a_model_that_verifies_passes_the_verification_case_and_changed_the_graph(
        self, profile, case_by_id
    ):
        score = run_case(
            case_by_id["post-write-verification"],
            profile,
            provider_factory=_factory(
                [
                    [("search_graph", {"query": "Metadata Handbook"})],
                    [
                        (
                            "update_node",
                            {
                                "node_id": "eval-resource-metadata-handbook",
                                "updates": {
                                    "summary": "Guidance for documenting statistical metadata"
                                },
                            },
                        )
                    ],
                    [
                        (
                            "get_related_nodes",
                            {"node_id": "eval-resource-metadata-handbook"},
                        )
                    ],
                    "Confirmed from the stored data.",
                ]
            ),
        )
        assert score.passed, [(c.name, c.detail) for c in score.conditions]
        # final_node_state passing proves the real graph was mutated, not just
        # that the model said the right words.
        assert score.dimensions["post_write_verification"].passed is True

    def test_a_complete_translation_passes_the_completeness_case(
        self, profile, case_by_id
    ):
        score = run_case(
            case_by_id["completeness-full-translation"],
            profile,
            provider_factory=_factory(
                [
                    [("search_graph", {"query": "National Statistics Office"})],
                    [
                        (
                            "update_node",
                            {
                                "node_id": "eval-actor-statistics-office",
                                "updates": {
                                    "name": "Statistiska centralbyran",
                                    "description": "Det nationella organet for officiell statistik.",
                                    "summary": "Nationell statistikproducent",
                                },
                            },
                        )
                    ],
                    "Translated.",
                ]
            ),
        )
        assert score.passed, [(c.name, c.detail) for c in score.conditions]

    def test_halting_on_an_ambiguous_name_passes_the_adherence_case(
        self, profile, case_by_id
    ):
        score = run_case(
            case_by_id["skill-adherence-ambiguous-name-halts"],
            profile,
            provider_factory=_factory(
                [
                    [("search_graph", {"query": "Metadata Register"})],
                    "Two initiatives share that name. Which did you mean?",
                ]
            ),
        )
        assert score.passed, [(c.name, c.detail) for c in score.conditions]

    def test_both_skill_selection_cases_pass_for_a_model_that_reads_when_to_use(
        self, profile, case_by_id
    ):
        inventory = run_case(
            case_by_id["skill-selection-inventory-question"],
            profile,
            provider_factory=_factory(
                [[("search_graph", {"query": ""})], "Three nodes."]
            ),
        )
        schema = run_case(
            case_by_id["skill-selection-schema-question"],
            profile,
            provider_factory=_factory([[("get_schema", {})], "These types."]),
        )
        assert inventory.passed and schema.passed


class TestRunCaseBadModel:
    def test_a_guessed_id_fails_even_though_the_write_succeeds(
        self, profile, case_by_id
    ):
        """The core failure the harness exists to make visible."""
        score = run_case(
            case_by_id["id-resolution-before-write"],
            profile,
            provider_factory=_factory(
                [
                    [
                        (
                            "update_node",
                            {
                                "node_id": "eval-initiative-metadata-register",
                                "updates": {"tags": ["priority"]},
                            },
                        )
                    ],
                    "Done.",
                ]
            ),
        )
        assert not score.passed
        assert score.dimensions["id_resolution"].passed is False

    def test_claiming_success_without_reading_back_fails_verification(
        self, profile, case_by_id
    ):
        score = run_case(
            case_by_id["post-write-verification"],
            profile,
            provider_factory=_factory(
                [
                    [("search_graph", {"query": "Metadata Handbook"})],
                    [
                        (
                            "update_node",
                            {
                                "node_id": "eval-resource-metadata-handbook",
                                "updates": {
                                    "summary": "Guidance for documenting statistical metadata"
                                },
                            },
                        )
                    ],
                    "Saved.",
                ]
            ),
        )
        assert not score.passed
        assert score.dimensions["post_write_verification"].passed is False

    def test_a_partial_translation_reported_as_complete_fails(
        self, profile, case_by_id
    ):
        score = run_case(
            case_by_id["completeness-full-translation"],
            profile,
            provider_factory=_factory(
                [
                    [("search_graph", {"query": "National Statistics Office"})],
                    [
                        (
                            "update_node",
                            {
                                "node_id": "eval-actor-statistics-office",
                                "updates": {"name": "Statistiska centralbyran"},
                            },
                        )
                    ],
                    "Fully translated into Swedish.",
                ]
            ),
        )
        assert not score.passed
        assert score.dimensions["completeness"].passed is False

    def test_picking_one_of_two_same_named_nodes_fails_adherence(
        self, profile, case_by_id
    ):
        score = run_case(
            case_by_id["skill-adherence-ambiguous-name-halts"],
            profile,
            provider_factory=_factory(
                [
                    [("search_graph", {"query": "Metadata Register"})],
                    [
                        (
                            "update_node",
                            {
                                "node_id": "eval-initiative-metadata-register-phase-two",
                                "updates": {"summary": "Active phase"},
                            },
                        )
                    ],
                    "Updated the active phase.",
                ]
            ),
        )
        assert not score.passed
        assert score.dimensions["skill_adherence"].passed is False

    def test_a_fabricated_id_in_the_answer_fails(self, profile, case_by_id):
        score = run_case(
            case_by_id["unsupported-entity-reference"],
            profile,
            provider_factory=_factory(
                [
                    [("search_graph", {"query": "Metadata Handbook"})],
                    "Produced by eval-initiative-metadata-registry-programme.",
                ]
            ),
        )
        assert not score.passed
        assert score.dimensions["unsupported_entity_reference"].passed is False

    def test_a_model_with_a_favourite_first_call_fails_one_of_the_selection_pair(
        self, profile, case_by_id
    ):
        """Why the pair exists: a fixed first call cannot pass both."""

        def always_search():
            return _factory([[("search_graph", {"query": ""})], "ok"])

        inventory = run_case(
            case_by_id["skill-selection-inventory-question"],
            profile,
            provider_factory=always_search(),
        )
        schema = run_case(
            case_by_id["skill-selection-schema-question"],
            profile,
            provider_factory=always_search(),
        )
        assert inventory.passed
        assert not schema.passed


class TestRunCaseRobustness:
    def test_a_provider_that_raises_is_recorded_as_a_run_error_not_a_crash(
        self, profile, case_by_id
    ):
        from backend.evaluation.tests.conftest import ScriptedProvider

        score = run_case(
            case_by_id["tool-call-validity-read-path"],
            profile,
            provider_factory=lambda _p: ScriptedProvider(["x"], raise_on_turn=0),
        )
        assert score.run_error is not None
        assert not score.passed
        assert score.conditions == []

    def test_a_run_mutates_only_its_own_copy_of_the_fixture(self, profile, case_by_id):
        """
        Each case starts from the committed fixture, never from a previous run.

        A graph that carried writes between cases would make a case's result
        depend on which cases ran before it, and on a suite re-run would compare
        two models against different starting states.
        """
        case = case_by_id["post-write-verification"]
        before = case.graph_path().read_text(encoding="utf-8")

        write = [
            [("search_graph", {"query": "Metadata Handbook"})],
            [
                (
                    "update_node",
                    {
                        "node_id": "eval-resource-metadata-handbook",
                        "updates": {
                            "summary": "Guidance for documenting statistical metadata"
                        },
                    },
                )
            ],
            [("get_related_nodes", {"node_id": "eval-resource-metadata-handbook"})],
            "done",
        ]
        first = run_case(case, profile, provider_factory=_factory(write))
        # The write really did land, in this run's own graph...
        assert first.passed, [(c.name, c.detail) for c in first.conditions]
        # ...and the committed fixture on disk is untouched.
        assert case.graph_path().read_text(encoding="utf-8") == before

        # A second run sees the fixture's original value, not the first run's write.
        second = run_case(
            case,
            profile,
            provider_factory=_factory(
                [[("search_graph", {"query": "Metadata Handbook"})], "done"]
            ),
        )
        assert second.tool_calls == ["search_graph"]
        assert not second.passed  # it never wrote, so verification cannot hold


class TestSkillsContext:
    def test_a_fixture_skill_is_rendered_into_the_production_prompt_shape(self):
        from backend.evaluation.cases import SKILLS_DIR

        context = build_skills_context([SKILLS_DIR / "graph-maintenance-protocol.md"])
        assert "--- SKILLS ---" in context
        assert '<skill name="Graph Maintenance Protocol">' in context
        assert "When to use:" in context
        assert "ID-first execution" in context
        # Frontmatter is parsed, not pasted through as body text.
        assert "---\nid: graph-maintenance-protocol" not in context

    def test_no_skills_means_no_injected_context(self):
        assert build_skills_context([]) is None

    def test_the_skill_text_reaches_the_model_system_prompt(self, profile, case_by_id):
        from backend.evaluation.tests.conftest import ScriptedProvider

        provider = ScriptedProvider([[("search_graph", {"query": "x"})], "done"])
        run_case(
            case_by_id["id-resolution-before-write"],
            profile,
            provider_factory=lambda _p: provider,
        )
        assert "ID-first execution" in provider.received_system_prompts[0]

    def test_both_skills_reach_the_model_in_a_selection_case(self, profile, case_by_id):
        from backend.evaluation.tests.conftest import ScriptedProvider

        provider = ScriptedProvider([[("get_schema", {})], "done"])
        run_case(
            case_by_id["skill-selection-schema-question"],
            profile,
            provider_factory=lambda _p: provider,
        )
        prompt = provider.received_system_prompts[0]
        assert "Schema Explainer" in prompt and "Inventory Reporter" in prompt


class TestSuiteAndReport:
    def test_run_suite_scores_every_case_it_is_given(self, profile, case_by_id):
        cases = [
            case_by_id["tool-call-validity-read-path"],
            case_by_id["unsupported-entity-reference"],
        ]
        result = run_suite(
            profile,
            cases=cases,
            provider_factory=_factory([[("search_graph", {"query": "x"})], "done"]),
        )
        assert result.total == 2
        assert result.profile_id == profile.id
        assert result.model == profile.model

    def test_the_report_carries_scores_and_the_unscored_dimension(
        self, profile, case_by_id
    ):
        result = run_suite(
            profile,
            cases=[case_by_id["tool-call-validity-read-path"]],
            provider_factory=_factory([[("search_graph", {"query": "x"})], "done"]),
        )
        report = build_report(result)

        assert report["summary"]["unscored_dimensions"] == ["hallucination"]
        assert report["dimensions"]["hallucination"]["mechanically_scored"] == "none"
        case_row = report["cases"][0]
        assert case_row["tool_calls"] == ["search_graph"]
        assert case_row["dimensions"]["hallucination"]["scored"] is False
        assert "latency_ms" in case_row

    def test_the_report_is_json_serialisable(self, profile, case_by_id):
        result = run_suite(
            profile,
            cases=[case_by_id["tool-call-validity-read-path"]],
            provider_factory=_factory([[("search_graph", {"query": "x"})], "done"]),
        )
        json.dumps(build_report(result))

    def test_the_report_marks_tokens_unreported_when_the_endpoint_omits_them(
        self, profile, case_by_id
    ):
        """An endpoint that reports no usage must not read as zero tokens."""
        result = run_suite(
            profile,
            cases=[case_by_id["tool-call-validity-read-path"]],
            provider_factory=_factory([[("search_graph", {"query": "x"})], "done"]),
        )
        tokens = build_report(result)["cases"][0]["tokens"]
        assert tokens["reported"] is False
        assert tokens["total"] is None

    def test_the_report_carries_reported_token_counts(self, profile, case_by_id):
        from backend.evaluation.tests.conftest import ScriptedProvider

        provider = ScriptedProvider(
            [[("search_graph", {"query": "x"})], "done"],
            usage={"prompt_tokens": 300, "completion_tokens": 20},
        )
        result = run_suite(
            profile,
            cases=[case_by_id["tool-call-validity-read-path"]],
            provider_factory=lambda _p: provider,
        )
        tokens = build_report(result)["cases"][0]["tokens"]
        assert tokens["reported"] is True
        assert tokens["total"] == 640  # two provider calls
