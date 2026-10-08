"""
Tests for the case set and the dimension table.

The shipped cases and fixtures are data, and a wrong expectation in them is
indistinguishable from a model failure when a report is read months later. These
tests pin the properties that keep a report meaningful: every case is
checkable, declares a condition that actually scores the dimension it claims,
and points at fixtures that exist.
"""

import json

import pytest
from pydantic import ValidationError

from backend.evaluation.cases import (
    DEFAULT_VERIFY_READ_TOOLS,
    ID_BEARING_ARGS,
    WRITE_TOOLS,
    AcceptanceCase,
    ExpectedBehaviour,
    load_cases,
)
from backend.evaluation.dimensions import (
    DIMENSIONS,
    Mechanical,
    mechanically_scored_dimensions,
    unscored_dimensions,
)


class TestShippedCases:
    def test_the_case_set_loads(self):
        assert len(load_cases()) >= 8

    def test_every_case_declares_at_least_one_pass_condition(self):
        for case in load_cases():
            assert case.expect.declared_conditions(), case.id

    def test_every_case_explains_itself(self):
        """A report is read by someone who was not here; notes are not optional."""
        for case in load_cases():
            assert len(case.notes) > 40, case.id

    def test_case_ids_are_unique(self):
        ids = [case.id for case in load_cases()]
        assert len(ids) == len(set(ids))

    def test_every_scorable_dimension_is_covered_by_a_case_or_reported_per_run(self):
        """
        The coverage claim, made checkable.

        A dimension is covered when some case names it, or when it is reported
        on every run and so needs no case of its own (latency, token_profile),
        or when it is deliberately unscored (hallucination). Anything else is a
        dimension the harness claims to measure and does not.
        """
        claimed = {case.dimension for case in load_cases()}
        reported_per_run = {"latency", "token_profile"}
        for key, dim in DIMENSIONS.items():
            if dim.mechanical is Mechanical.NONE:
                continue
            assert key in claimed or key in reported_per_run, (
                f"dimension {key!r} is scorable but no case measures it"
            )

    def test_skill_selection_is_covered_by_a_discriminating_pair(self):
        """
        One selection case proves nothing.

        With a single case, a model with a fixed favourite first call scores a
        pass without having read either skill. Two cases sharing the same
        injected skills and expecting different first calls is the cheapest
        design that cannot be passed that way.
        """
        cases = [c for c in load_cases() if c.dimension == "skill_selection"]
        assert len(cases) >= 2
        assert len({tuple(sorted(c.skills)) for c in cases}) == 1
        expected_first = {c.expect.discriminating_first_call for c in cases}
        assert len(expected_first) == len(cases)

    def test_no_case_requires_a_tool_the_assistant_does_not_advertise(self):
        """
        A case may only name tools the model is actually offered.

        get_node_details and get_graph_stats are executable in ChatService's
        tools map but absent from ChatProcessor's tool_definitions, so a case
        requiring either would fail for a reason that says nothing about the
        model.
        """
        from unittest.mock import patch

        with patch("backend.ui.chat_logic.create_provider"):
            from backend.ui.chat_logic import ChatProcessor

            advertised = {t["name"] for t in ChatProcessor({}).tool_definitions}

        for case in load_cases():
            named = (
                set(case.expect.required_call_sequence)
                | set(case.expect.forbidden_calls)
                | set(case.expect.verify_read_tools)
            )
            if case.expect.discriminating_first_call:
                named.add(case.expect.discriminating_first_call)
            unknown = named - advertised
            assert not unknown, f"case {case.id!r} names unadvertised tool(s) {unknown}"

    def test_fixture_graphs_are_well_formed(self):
        for case in load_cases():
            graph = json.loads(case.graph_path().read_text(encoding="utf-8"))
            assert graph["nodes"], case.graph
            for node in graph["nodes"]:
                assert node.get("id") and node.get("type") and node.get("name")

    def test_a_case_expecting_a_node_state_names_a_node_in_its_own_fixture(self):
        """An expectation about a node the fixture lacks can never pass."""
        for case in load_cases():
            graph = json.loads(case.graph_path().read_text(encoding="utf-8"))
            present = {node["id"] for node in graph["nodes"]}
            referenced = set(case.expect.final_node_state) | set(
                case.expect.final_node_fields_changed
            )
            assert referenced <= present, case.id


class TestCaseValidation:
    def _expect(self, **kwargs):
        return ExpectedBehaviour(**kwargs)

    def test_an_expectation_with_no_condition_is_rejected(self):
        with pytest.raises(ValidationError, match="no pass condition"):
            ExpectedBehaviour()

    def test_an_unknown_dimension_is_rejected(self):
        with pytest.raises(ValidationError, match="unknown dimension"):
            AcceptanceCase(
                id="x",
                dimension="vibes",
                prompt="p",
                graph="metadata-pilot-small.json",
                expect=self._expect(tool_calls_valid=True),
            )

    def test_a_case_claiming_a_dimension_it_does_not_check_is_rejected(self):
        """
        The quiet way a suite overstates its coverage.

        Claiming id_resolution while only checking call validity would report
        coverage of a dimension nothing exercised.
        """
        with pytest.raises(ValidationError, match="declares none of its conditions"):
            AcceptanceCase(
                id="x",
                dimension="id_resolution",
                prompt="p",
                graph="metadata-pilot-small.json",
                expect=self._expect(tool_calls_valid=True),
            )

    def test_a_blank_prompt_is_rejected(self):
        with pytest.raises(ValidationError):
            AcceptanceCase(
                id="x",
                dimension="tool_call_validity",
                prompt="   ",
                graph="g.json",
                expect=self._expect(tool_calls_valid=True),
            )

    def test_a_missing_fixture_graph_is_rejected_at_load_time(self, tmp_path):
        """Discovered before any paid provider call, not during the suite."""
        cases_file = tmp_path / "cases.json"
        cases_file.write_text(
            json.dumps(
                [
                    {
                        "id": "x",
                        "dimension": "tool_call_validity",
                        "prompt": "p",
                        "graph": "does-not-exist.json",
                        "expect": {"tool_calls_valid": True},
                    }
                ]
            )
        )
        with pytest.raises(ValueError, match="missing fixture graph"):
            load_cases(cases_file=cases_file, graphs_dir=tmp_path)

    def test_a_missing_fixture_skill_is_rejected_at_load_time(self, tmp_path):
        (tmp_path / "g.json").write_text('{"nodes": [], "edges": []}')
        cases_file = tmp_path / "cases.json"
        cases_file.write_text(
            json.dumps(
                [
                    {
                        "id": "x",
                        "dimension": "tool_call_validity",
                        "prompt": "p",
                        "graph": "g.json",
                        "skills": ["nope.md"],
                        "expect": {"tool_calls_valid": True},
                    }
                ]
            )
        )
        with pytest.raises(ValueError, match="missing fixture skill"):
            load_cases(cases_file=cases_file, graphs_dir=tmp_path, skills_dir=tmp_path)

    def test_duplicate_case_ids_are_rejected(self, tmp_path):
        (tmp_path / "g.json").write_text('{"nodes": [], "edges": []}')
        entry = {
            "id": "same",
            "dimension": "tool_call_validity",
            "prompt": "p",
            "graph": "g.json",
            "expect": {"tool_calls_valid": True},
        }
        cases_file = tmp_path / "cases.json"
        cases_file.write_text(json.dumps([entry, dict(entry)]))
        with pytest.raises(ValueError, match="duplicate case id"):
            load_cases(cases_file=cases_file, graphs_dir=tmp_path)

    def test_a_cases_file_that_is_not_an_array_is_rejected(self, tmp_path):
        cases_file = tmp_path / "cases.json"
        cases_file.write_text(json.dumps({"cases": []}))
        with pytest.raises(ValueError, match="must contain a JSON array"):
            load_cases(cases_file=cases_file)


class TestDimensionTable:
    def test_hallucination_is_not_reported_as_mechanically_scored(self):
        """
        The deliberate gap.

        Shipping a number called "hallucination rate" would make an unstated
        definition look like a measurement. If this ever flips to scored, the
        definition has to be written down and agreed first.
        """
        assert DIMENSIONS["hallucination"].mechanical is Mechanical.NONE
        assert "hallucination" in unscored_dimensions()
        assert "hallucination" not in mechanically_scored_dimensions()
        assert not DIMENSIONS["hallucination"].measured_by

    def test_the_narrow_entity_check_is_named_separately_from_hallucination(self):
        narrow = DIMENSIONS["unsupported_entity_reference"]
        assert narrow.mechanical is Mechanical.FULL
        assert narrow.measured_by == ["answer_entities_supported"]
        assert "not called a hallucination rate" in narrow.caveat

    def test_every_dimension_states_its_caveat(self):
        for key, dim in DIMENSIONS.items():
            assert len(dim.caveat) > 40, key
            assert dim.title, key

    def test_partially_scored_dimensions_say_what_is_left_out(self):
        for key, dim in DIMENSIONS.items():
            if dim.mechanical is Mechanical.PARTIAL:
                assert dim.measured_by, key
                assert "not" in dim.caveat.lower(), key

    def test_every_measured_by_field_exists_on_the_expectation_model(self):
        """A caveat pointing at a field that does not exist scores nothing."""
        fields = set(ExpectedBehaviour.model_fields)
        for key, dim in DIMENSIONS.items():
            unknown = set(dim.measured_by) - fields
            assert not unknown, f"{key} points at non-existent field(s) {unknown}"

    def test_the_covered_dimensions_are_the_ones_the_task_set_out_to_measure(self):
        """Pins the dimension set so a silent removal shows up as a failure."""
        assert set(DIMENSIONS) == {
            "skill_selection",
            "skill_adherence",
            "tool_call_validity",
            "id_resolution",
            "post_write_verification",
            "completeness",
            "hallucination",
            "unsupported_entity_reference",
            "latency",
            "token_profile",
        }


class TestToolTables:
    def test_every_id_bearing_and_write_tool_is_actually_advertised(self):
        from unittest.mock import patch

        with patch("backend.ui.chat_logic.create_provider"):
            from backend.ui.chat_logic import ChatProcessor

            advertised = {t["name"] for t in ChatProcessor({}).tool_definitions}

        assert set(ID_BEARING_ARGS) <= advertised
        assert set(WRITE_TOOLS) <= advertised
        assert set(DEFAULT_VERIFY_READ_TOOLS) <= advertised

    def test_every_write_tool_has_an_id_argument_table_entry(self):
        """A write whose ids are not extracted would pass ID-first silently."""
        assert set(WRITE_TOOLS) <= set(ID_BEARING_ARGS)

    def test_the_verification_read_tools_exclude_get_node_details(self):
        """
        The subject under test cannot call it.

        get_node_details is in ChatService's tools map but is not advertised to
        the model, so requiring it would measure the integration, not the model.
        """
        assert "get_node_details" not in DEFAULT_VERIFY_READ_TOOLS

    def test_id_argument_paths_name_real_schema_properties(self):
        """A typo in a path silently extracts no ids and so never fails."""
        from unittest.mock import patch

        with patch("backend.ui.chat_logic.create_provider"):
            from backend.ui.chat_logic import ChatProcessor

            schemas = {
                t["name"]: t.get("input_schema", {})
                for t in ChatProcessor({}).tool_definitions
            }

        for tool, paths in ID_BEARING_ARGS.items():
            properties = set((schemas[tool].get("properties") or {}))
            for path in paths:
                root = path.split(".")[0].removesuffix("[]")
                assert root in properties, f"{tool}: no property {root!r}"
