"""
Tests for the case set and the dimension table.

The shipped cases and fixtures are data, and a wrong expectation in them is
indistinguishable from a model failure when a report is read months later. These
tests pin the properties that keep a report meaningful: every case is
checkable, declares a condition that actually scores the dimension it claims,
and points at fixtures that exist.
"""

import json
from pathlib import Path

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
    reported_only_dimensions,
    unscored_dimensions,
)


class TestShippedCases:
    def test_the_case_set_is_the_size_the_docs_state(self):
        """
        Exact, not a lower bound.

        docs/SKILL_EVALUATION.md says "Nine cases"; a tenth would make the doc
        quietly wrong, which is the same drift the dimension-table test exists
        to stop.
        """
        import re
        from pathlib import Path

        cases = load_cases()
        assert len(cases) == 9

        doc = (
            Path(__file__).resolve().parents[3] / "docs" / "SKILL_EVALUATION.md"
        ).read_text(encoding="utf-8")
        stated = re.search(r"^([A-Z][a-z]+) cases in `backend", doc, re.M)
        assert stated, "the docs no longer state the case count in the expected form"
        words = {
            "Eight": 8,
            "Nine": 9,
            "Ten": 10,
            "Eleven": 11,
            "Twelve": 12,
        }
        assert words.get(stated.group(1)) == len(cases), (
            f"docs say {stated.group(1)!r} cases, the set has {len(cases)}"
        )

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

    def test_the_ambiguous_fixture_is_actually_ambiguous(self):
        """
        `skill-adherence-ambiguous-name-halts` exists because of this ambiguity.

        Renaming either node so the two no longer share a name was undetectable:
        the case would then ask a model to halt on nothing and score it as
        failing adherence, while the test covering it still passed because its
        scripted model writes, which is forbidden either way.
        """
        case = next(
            c for c in load_cases() if c.id == "skill-adherence-ambiguous-name-halts"
        )
        graph = json.loads(case.graph_path().read_text(encoding="utf-8"))
        names = [node["name"] for node in graph["nodes"]]
        duplicated = {name for name in names if names.count(name) > 1}
        assert duplicated, (
            f"{case.graph} has no duplicated node name, so the case it backs "
            "asks the model to halt on an ambiguity that is not there"
        )
        assert any(name in case.prompt for name in duplicated), case.prompt

    def test_a_prompt_asking_about_a_relationship_has_that_relationship(self):
        """
        A third fixture premise, of the same class as the two already pinned.

        Two cases prompt "which initiative PRODUCES the Metadata Handbook".
        Retyping that edge to RELATES_TO was undetectable, and the question then
        has no answer — both cases would measure nothing while scoring the model
        on it.
        """
        for case in load_cases():
            graph = json.loads(case.graph_path().read_text(encoding="utf-8"))
            edge_types = {edge.get("type") for edge in graph.get("edges") or []}
            for word, relationship in (
                ("produces", "PRODUCES"),
                ("belong", "BELONGS_TO"),
            ):
                if word in case.prompt.lower():
                    assert relationship in edge_types, (
                        f"case {case.id!r} asks about {word!r} but {case.graph} "
                        f"has no {relationship} edge (types present: "
                        f"{sorted(t for t in edge_types if t)})"
                    )

    def test_the_id_resolution_case_requires_a_read_before_its_write(self):
        """
        Its sequence IS the ID-first rule; trimming it was undetectable.

        The dispatch test asserts condition names, not their contents, and
        nothing else read the sequence — so ["search_graph", "update_node"]
        could become ["update_node"] and the case would stop testing the thing
        it is named for.
        """
        case = next(c for c in load_cases() if c.id == "id-resolution-before-write")
        sequence = case.expect.required_call_sequence
        assert len(sequence) >= 2, sequence
        write_positions = [i for i, name in enumerate(sequence) if name in WRITE_TOOLS]
        assert write_positions, f"{sequence} contains no write"
        assert write_positions[0] > 0, (
            f"{sequence} lets the write come first, so it no longer requires a "
            "read before the write"
        )

    def test_fixture_nodes_carry_no_field_the_node_model_drops(self):
        """
        A fixture is the ground truth a case is judged against.

        A key the model does not have (``communities`` is not a Node field, and
        the serializer silently drops it) is dead data there: it invites a case
        to assert on something the system never stores, and it differs between
        the raw fixture and the graph's own serialization of it.
        """
        from backend.core.models import Node

        known = set(Node.model_fields) | {"edges"}
        for case in load_cases():
            graph = json.loads(case.graph_path().read_text(encoding="utf-8"))
            for node in graph["nodes"]:
                unknown = set(node) - known
                assert not unknown, f"{case.graph}: node {node['id']} has {unknown}"

    def test_a_case_expecting_a_node_state_names_a_node_in_its_own_fixture(self):
        """An expectation about a node the fixture lacks can never pass."""
        for case in load_cases():
            graph = json.loads(case.graph_path().read_text(encoding="utf-8"))
            present = {node["id"] for node in graph["nodes"]}
            referenced = set(case.expect.final_node_state) | set(
                case.expect.final_node_fields_changed
            )
            assert referenced <= present, case.id

    def test_no_expected_node_state_is_already_true_in_the_fixture(self):
        """
        An expectation the fixture already satisfies passes against a model
        that did nothing.

        The sibling test above pins that the node exists; nothing pinned that
        the expected VALUE differs from the one the fixture ships. A case
        asking for a summary the node already carries would score
        `completeness` and `post_write_verification` green on a run where the
        model answered in prose and called no tools — the same false pass the
        baseline-snapshot fix removed from the other direction. The validator
        cannot catch this, because a case is validated before its fixture is
        loaded, so it is pinned here over the shipped cases.
        """
        for case in load_cases():
            graph = json.loads(case.graph_path().read_text(encoding="utf-8"))
            nodes = {node["id"]: node for node in graph["nodes"]}
            for node_id, fields in case.expect.final_node_state.items():
                for key, want in fields.items():
                    got = nodes[node_id].get(key)
                    assert got != want, (
                        f"{case.id}: {node_id}.{key} already is {want!r} in "
                        f"{case.graph}, so the expectation passes without a write"
                    )


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

    @pytest.mark.parametrize("field", ["updated_at", "created_at", "id"])
    @pytest.mark.parametrize(
        "expectation", ["final_node_fields_changed", "final_node_state"]
    )
    def test_a_case_naming_a_timestamp_as_evidence_of_change_is_rejected(
        self, field, expectation
    ):
        """
        `updated_at` moves on every write, so it would pass for any write at all.

        `id` and `created_at` never move, so a "changed" expectation naming one
        could never pass, while an exact-value expectation naming one passes
        without the model doing anything. Either way the case reports something
        other than whether the requested change was made — and the serializer
        adds all three to every exported node, so they are easy to reach for.

        Parametrised over BOTH expectation fields: only the "changed" half was
        exercised, so the exact-value half the validator's own docstring
        motivates could be deleted with the suite green.
        """
        value = (
            {"eval-actor-statistics-office": [field]}
            if expectation == "final_node_fields_changed"
            else {"eval-actor-statistics-office": {field: "anything"}}
        )
        with pytest.raises(ValidationError, match="cannot evidence"):
            AcceptanceCase(
                id="x",
                dimension="completeness",
                prompt="p",
                graph="metadata-pilot-small.json",
                expect=self._expect(**{expectation: value}),
            )

    def test_a_case_naming_a_real_content_field_is_accepted(self):
        AcceptanceCase(
            id="x",
            dimension="completeness",
            prompt="p",
            graph="metadata-pilot-small.json",
            expect=self._expect(
                final_node_fields_changed={"eval-actor-statistics-office": ["summary"]}
            ),
        )

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

    def test_reported_only_dimensions_are_not_called_fully_scored(self):
        """
        A measurement with no threshold is not a dimension the harness scores.

        latency and tokens were FULL with no conditions and caveats reading
        "Reported, never pass/fail" — a reported-only number presented as fully
        scored, which is the overstatement G9 forbids.
        """
        assert reported_only_dimensions() == ["latency", "token_profile"]
        for key in reported_only_dimensions():
            assert DIMENSIONS[key].mechanical is Mechanical.REPORTED
            assert not DIMENSIONS[key].measured_by
            assert key not in mechanically_scored_dimensions()

    def test_every_dimension_falls_in_exactly_one_class(self):
        buckets = (
            set(mechanically_scored_dimensions())
            | set(reported_only_dimensions())
            | set(unscored_dimensions())
        )
        assert buckets == set(DIMENSIONS)
        assert len(mechanically_scored_dimensions()) + len(
            reported_only_dimensions()
        ) + len(unscored_dimensions()) == len(DIMENSIONS)

    def test_the_markdown_table_in_the_docs_matches_this_table(self):
        """
        docs/SKILL_EVALUATION.md claims the two cannot drift. This makes it true.

        Without it the claim was only as good as whoever last edited the doc,
        and a dimension described there as scored more mechanically than it is
        is exactly the defect G9 names.
        """
        import re

        doc = (
            Path(__file__).resolve().parents[3] / "docs" / "SKILL_EVALUATION.md"
        ).read_text(encoding="utf-8")

        rows = re.findall(r"^\| `([a-z_]+)` \| \*\*(.+?)\*\* \|", doc, re.M)
        assert rows, "no dimension table found in docs/SKILL_EVALUATION.md"

        documented = {key: label for key, label in rows}
        assert set(documented) == set(DIMENSIONS), (
            f"doc table and DIMENSIONS disagree on which dimensions exist: "
            f"only in doc {sorted(set(documented) - set(DIMENSIONS))}, "
            f"only in code {sorted(set(DIMENSIONS) - set(documented))}"
        )
        for key, label in documented.items():
            expected = {
                Mechanical.FULL: "full",
                Mechanical.PARTIAL: "partial",
                Mechanical.REPORTED: "reported",
                Mechanical.NONE: "not scored",
            }[DIMENSIONS[key].mechanical]
            assert label == expected, (
                f"doc table says {key!r} is {label!r}, code says {expected!r}"
            )

    def test_the_cli_does_not_describe_the_unscored_dimension_as_measured(self):
        """
        `--dimensions` is where an operator reads what the harness measures.

        Branching on `measured_by` alone put hallucination and the reported-only
        dimensions in the same bucket, so the one dimension deliberately left
        unscored printed as "reported per run" — the overstatement G9 forbids,
        in the output most likely to be quoted.
        """
        import io
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
        from scripts.run_skill_eval import print_dimensions

        captured = io.StringIO()
        original = sys.stdout
        sys.stdout = captured
        try:
            print_dimensions()
        finally:
            sys.stdout = original
        output = captured.getvalue()

        hallucination_block = output.split("hallucination  [none]")[1].split("\n\n")[0]
        assert "deliberately not scored" in hallucination_block
        assert "reported per run" not in hallucination_block
        for key in reported_only_dimensions():
            block = output.split(f"{key}  [reported]")[1].split("\n\n")[0]
            assert "no pass condition" in block

    def test_every_dimension_keeps_the_level_the_docs_state(self):
        """
        Pins the level, not just the key set.

        Promoting skill_selection or skill_adherence from PARTIAL to FULL made
        the code contradict the docs table while the whole suite stayed green:
        the "partially scored dimensions say what is left out" test only
        inspects dimensions ALREADY marked partial, so a promotion escapes it.
        A dimension claiming to be scored more mechanically than it is, is the
        exact overstatement G9 forbids.
        """
        assert {key: dim.mechanical.value for key, dim in DIMENSIONS.items()} == {
            "skill_selection": "partial",
            "skill_adherence": "partial",
            "tool_call_validity": "full",
            "id_resolution": "full",
            "post_write_verification": "full",
            "completeness": "full",
            "hallucination": "none",
            "unsupported_entity_reference": "full",
            "latency": "reported",
            "token_profile": "reported",
        }

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

    def test_no_table_entry_is_inert(self):
        """
        An entry mapping to no paths is indistinguishable from absence.

        It extracts nothing, so the tool is silently unchecked while the table
        reads as if it were covered — and the path-validity test below iterates
        nothing for it.
        """
        empty = sorted(tool for tool, paths in ID_BEARING_ARGS.items() if not paths)
        assert not empty, f"inert ID_BEARING_ARGS entries: {empty}"

    def test_the_verification_caveat_names_the_read_tools_that_exist(self):
        """
        The caveat said a read-back can "only" come through two tools.

        find_similar_nodes is advertised AND in DEFAULT_VERIFY_READ_TOOLS, so a
        case author reading the caveat would have believed a read-back through
        it does not count. Pinned against the default set rather than a list
        retyped in prose.
        """
        caveat = DIMENSIONS["post_write_verification"].caveat
        for tool in DEFAULT_VERIFY_READ_TOOLS:
            assert tool in caveat, (
                f"{tool} is an accepted read-back tool but the caveat omits it"
            )
        assert "get_node_details" in caveat, "the caveat should say why it is excluded"

    def test_the_verification_caveat_mentions_the_presence_only_scope(self):
        """
        The behaviour change reached none of its prose surfaces last round.

        A removal-only case is now rejected as mis-specified; a reader of the
        caveat had no way to know that.
        """
        from backend.evaluation.scoring import _PRESENCE_VERIFIABLE_WRITES

        caveat = DIMENSIONS["post_write_verification"].caveat
        assert "presence" in caveat.lower()
        for tool in ("add_nodes", "update_node"):
            assert tool in caveat, tool
        assert "unarchive" in caveat
        # And the set the prose describes is the set the code uses.
        assert _PRESENCE_VERIFIABLE_WRITES == {
            "add_nodes",
            "update_node",
            "unarchive_nodes",
            "unarchive_edges",
        }

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
