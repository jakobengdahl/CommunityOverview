"""
Tests for the runner: the shipped cases, end to end, against a scripted model.

These run the real ChatService — the product's system prompt, tool definitions
and tool-execution loop — with only the provider replaced. That is what makes
them worth having: a case that scores correctly here would score correctly
against a real model, because everything between the prompt and the score is
the same code.
"""

import json

import pytest

from backend.evaluation.runner import (
    build_report,
    build_skills_context,
    run_case,
    run_suite,
)


def case_row_of(report):
    return report["cases"][0]


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


class TestEveryDeclaredConditionIsEvaluated:
    """
    A dispatch site that silently stops firing is the worst failure here.

    The case still declares its condition, the condition is never evaluated, and
    the dimension reports `scored: false` instead of failing — so a model that
    violated it reads as one nothing was checked on. Asserting the condition
    NAMES, not just the verdict, is what makes that visible: a missing dispatch
    changes the set, while a passing run looks identical either way.
    """

    @pytest.mark.parametrize(
        "case_id,expected",
        [
            ("tool-call-validity-read-path", {"tool_calls_valid"}),
            (
                "id-resolution-before-write",
                {
                    "tool_calls_valid",
                    "ids_resolved_from_results",
                    "required_call_sequence",
                },
            ),
            (
                "post-write-verification",
                {
                    "tool_calls_valid",
                    "ids_resolved_from_results",
                    "verify_after_write",
                    "final_node_state",
                },
            ),
            (
                "completeness-full-translation",
                {
                    "tool_calls_valid",
                    "ids_resolved_from_results",
                    "final_node_fields_changed",
                },
            ),
            (
                "skill-adherence-ambiguous-name-halts",
                {"tool_calls_valid", "required_call_sequence", "forbidden_calls"},
            ),
            (
                "skill-selection-inventory-question",
                {"tool_calls_valid", "discriminating_first_call"},
            ),
            (
                "unsupported-entity-reference",
                {"tool_calls_valid", "answer_entities_supported"},
            ),
            ("token-profile-multi-step-traversal", {"tool_calls_valid"}),
        ],
    )
    def test_each_shipped_case_evaluates_exactly_what_it_declares(
        self, profile, case_by_id, case_id, expected
    ):
        case = case_by_id[case_id]
        score = run_case(
            case,
            profile,
            provider_factory=_factory([[("search_graph", {"query": "x"})], "done"]),
        )
        assert {c.name for c in score.conditions} == expected
        assert set(case.expect.declared_conditions()) == expected

    def test_a_dimension_no_condition_covers_is_not_reported_as_passing(
        self, profile, case_by_id
    ):
        """
        G4's main clause: unscored and passing must never look alike.

        The only coverage was of the deliberately-unscored hallucination row;
        the "this case declares no condition for this dimension" branch — the
        one that fires for eight of ten dimensions on every case — had none.
        """
        score = run_case(
            case_by_id["tool-call-validity-read-path"],
            profile,
            provider_factory=_factory([[("search_graph", {"query": "x"})], "done"]),
        )
        assert score.dimensions["tool_call_validity"].passed is True
        for key in ("completeness", "id_resolution", "post_write_verification"):
            assert score.dimensions[key].scored is False, key
            assert score.dimensions[key].passed is None, key


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
    def test_a_fixture_the_graph_layer_rejects_is_a_run_error_not_a_model_failure(
        self, profile, case_by_id, tmp_path
    ):
        """
        The same conflation G7 prevents on the provider side.

        Without a run_error the case still fails — every condition fails against
        an empty transcript — but it fails as if the MODEL had done nothing,
        when in fact the run never started.
        """
        import json

        (tmp_path / "metadata-pilot-small.json").write_text(
            json.dumps({"nodes": [{"id": "x"}], "edges": []}), encoding="utf-8"
        )
        score = run_case(
            case_by_id["tool-call-validity-read-path"],
            profile,
            provider_factory=_factory(["unused"]),
            graphs_dir=tmp_path,
        )
        assert score.run_error is not None, "a rejected fixture must be a run error"
        assert score.run_error.startswith("fixture setup failed:")
        assert score.conditions == []
        assert not score.passed

    def test_a_provider_that_cannot_be_built_loses_only_its_own_case(
        self, profile, case_by_id
    ):
        """
        A credential unset (or revoked mid-suite) must not take the suite down.

        The CLI checks credentials up front, but a library caller has no such
        gate, and a MissingCredentialError propagating out of run_suite would
        discard every case already scored.
        """
        from backend.config.model_profiles import MissingCredentialError

        def refuse(_profile):
            raise MissingCredentialError("EVAL_HARNESS_TEST_KEY is not set")

        result = run_suite(
            profile,
            cases=[
                case_by_id["tool-call-validity-read-path"],
                case_by_id["unsupported-entity-reference"],
            ],
            provider_factory=refuse,
        )
        assert result.total == 2
        assert result.run_errors == 2
        assert all("provider unavailable" in s.run_error for s in result.scores)
        assert not any(s.passed for s in result.scores)

    def test_the_report_summary_counts_run_errors_beside_passes(
        self, profile, case_by_id
    ):
        """
        A provider outage must not read as a quality difference.

        The per-case run_error was always there, but the summary showed only
        passed/total — so at the level a reader compares two providers, an
        endpoint that was down looked like a model that failed every case.
        """
        from backend.evaluation.tests.conftest import ScriptedProvider

        result = run_suite(
            profile,
            cases=[case_by_id["tool-call-validity-read-path"]],
            provider_factory=lambda _p: ScriptedProvider(["x"], raise_on_turn=0),
        )
        summary = build_report(result)["summary"]
        assert summary["run_errors"] == 1
        assert summary["passed"] == 0
        assert summary["reported_only_dimensions"] == ["latency", "token_profile"]

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


class TestFixturePathIsolation:
    def test_each_run_uses_a_graph_path_that_does_not_outlive_it(
        self, profile, case_by_id, monkeypatch
    ):
        """
        Two suites comparing two providers must not clobber each other.

        A fixed path under the system temp dir still rewrites the fixture per
        case, so G6's substance held — but two concurrent runs would share one
        file, which is exactly how someone compares two models.
        """
        import backend.evaluation.runner as runner_module

        seen = []
        original = runner_module._build_chat_service

        def record(graph_file):
            seen.append(graph_file)
            return original(graph_file)

        monkeypatch.setattr(runner_module, "_build_chat_service", record)

        case = case_by_id["tool-call-validity-read-path"]
        for _ in range(2):
            run_case(
                case,
                profile,
                provider_factory=_factory([[("search_graph", {"query": "x"})], "done"]),
            )

        assert len(seen) == 2
        assert seen[0] != seen[1], "two runs shared one graph file"
        for path in seen:
            assert not path.exists(), "the run's graph file outlived the run"
            assert not path.parent.exists(), "the run's temp directory was leaked"


class TestCompletenessBaseline:
    """
    A model that does nothing must fail every completeness expectation.

    The baseline a change is judged against is the fixture as the graph layer
    serializes it, not the raw JSON — the two differ in both directions, and
    either difference read as a change the model made.
    """

    class _DoesNothing:
        def create_completion(self, messages, system_prompt, tools, max_tokens=4096):
            from backend.llm.llm_providers import LLMResponse

            return LLMResponse(
                content=[{"type": "text", "text": "I did nothing."}],
                stop_reason="end_turn",
            )

        def format_tool_definitions(self, tools):
            return tools

    @pytest.mark.parametrize(
        "fields,why",
        [
            (["subtypes", "aliases", "metadata"], "serializer ADDS these defaults"),
            (["communities"], "a key neither the fixture nor the Node model has"),
            (["summary"], "an ordinary content field, present in both"),
        ],
    )
    def test_an_idle_model_fails_whatever_fields_a_case_names(
        self, profile, fields, why
    ):
        from backend.evaluation.cases import AcceptanceCase, ExpectedBehaviour

        case = AcceptanceCase(
            id="probe",
            dimension="completeness",
            prompt="translate this node completely",
            graph="metadata-pilot-small.json",
            expect=ExpectedBehaviour(
                final_node_fields_changed={"eval-actor-statistics-office": fields}
            ),
            notes=(
                "probe case asserting a serializer artefact cannot read as a change "
                f"the model made: {why}"
            ),
        )
        score = run_case(case, profile, provider_factory=lambda _p: self._DoesNothing())
        assert not score.passed, f"{fields} passed against a model that did nothing"
        assert score.dimensions["completeness"].passed is False


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
        for key in ("latency", "token_profile"):
            assert report["dimensions"][key]["mechanically_scored"] == "reported"
            assert case_row_of(report)["dimensions"][key]["scored"] is False
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
