"""
Tests for backend.service.graph_import.validate_import_document — the
whole-document validation pass for graph.json import (REPLACE mode). See
docs/adr/0006-graph-import-replace-mode.md.
"""

import pytest

from backend.service import graph_import


def _doc(nodes, edges=None):
    return {"nodes": nodes, "edges": edges if edges is not None else []}


def _node(node_id="n1", node_type="Actor", **overrides):
    payload = {
        "id": node_id,
        "type": node_type,
        "name": overrides.pop("name", "A node"),
    }
    payload.update(overrides)
    return payload


def _edge(edge_id="e1", source="n1", target="n2", edge_type="BELONGS_TO", **overrides):
    payload = {"id": edge_id, "source": source, "target": target, "type": edge_type}
    payload.update(overrides)
    return payload


class TestValidDocument:
    def test_a_well_formed_document_is_valid(self):
        result = graph_import.validate_import_document(
            _doc(
                [
                    _node("n1", "Actor", name="Alice"),
                    _node("n2", "Initiative", name="Project X"),
                ],
                [_edge("e1", "n1", "n2", "BELONGS_TO")],
            )
        )
        assert result.valid
        assert result.errors == []
        assert {n.id for n in result.nodes} == {"n1", "n2"}
        assert [e.id for e in result.edges] == ["e1"]

    def test_edges_are_optional(self):
        result = graph_import.validate_import_document(_doc([_node("n1")]))
        assert result.valid
        assert result.edges == []

    def test_a_default_edge_type_is_allowed(self):
        doc = _doc(
            [_node("n1"), _node("n2")],
            [{"id": "e1", "source": "n1", "target": "n2"}],
        )
        result = graph_import.validate_import_document(doc)
        assert result.valid
        assert result.edges[0].type_str == "RELATES_TO"


class TestTopLevelShape:
    def test_non_dict_document_is_rejected(self):
        result = graph_import.validate_import_document(["not", "a", "dict"])
        assert not result.valid
        assert result.nodes == []
        assert result.edges == []

    def test_missing_nodes_key_is_rejected(self):
        result = graph_import.validate_import_document({"edges": []})
        assert not result.valid

    def test_nodes_must_be_a_list(self):
        result = graph_import.validate_import_document({"nodes": "not-a-list"})
        assert not result.valid

    def test_edges_must_be_a_list_when_present(self):
        result = graph_import.validate_import_document(
            {"nodes": [_node("n1")], "edges": "not-a-list"}
        )
        assert not result.valid


class TestNodeValidation:
    def test_unknown_node_type_is_rejected(self):
        result = graph_import.validate_import_document(
            _doc([_node("n1", "NotARealNodeType")])
        )
        assert not result.valid
        assert any("NotARealNodeType" in e.message for e in result.errors)
        assert result.nodes == []

    def test_missing_node_id_is_rejected(self):
        result = graph_import.validate_import_document(
            _doc([{"type": "Actor", "name": "No id"}])
        )
        assert not result.valid

    def test_missing_node_type_is_rejected(self):
        result = graph_import.validate_import_document(
            _doc([{"id": "n1", "name": "No type"}])
        )
        assert not result.valid

    def test_duplicate_node_ids_are_rejected(self):
        result = graph_import.validate_import_document(
            _doc([_node("dup", name="First"), _node("dup", name="Second")])
        )
        assert not result.valid
        assert any("duplicate node id" in e.message for e in result.errors)

    def test_a_node_that_fails_field_construction_is_rejected(self):
        # name has a max_length of 200 on the Node model.
        result = graph_import.validate_import_document(
            _doc([_node("n1", name="x" * 500)])
        )
        assert not result.valid

    def test_reports_every_bad_node_not_just_the_first(self):
        result = graph_import.validate_import_document(
            _doc(
                [
                    _node("n1", "NotAType"),
                    _node("n2", "AlsoNotAType"),
                ]
            )
        )
        assert not result.valid
        assert len(result.errors) == 2


class TestEdgeValidation:
    def test_unknown_relationship_type_is_rejected(self):
        result = graph_import.validate_import_document(
            _doc(
                [_node("n1"), _node("n2")],
                [_edge("e1", "n1", "n2", "NOT_A_REAL_RELATIONSHIP")],
            )
        )
        assert not result.valid
        assert any("NOT_A_REAL_RELATIONSHIP" in e.message for e in result.errors)

    def test_duplicate_edge_ids_are_rejected(self):
        result = graph_import.validate_import_document(
            _doc(
                [_node("n1"), _node("n2"), _node("n3")],
                [
                    _edge("dup", "n1", "n2"),
                    _edge("dup", "n1", "n3"),
                ],
            )
        )
        assert not result.valid
        assert any("duplicate edge id" in e.message for e in result.errors)

    def test_dangling_source_reference_is_rejected(self):
        result = graph_import.validate_import_document(
            _doc([_node("n2")], [_edge("e1", "does-not-exist", "n2")])
        )
        assert not result.valid
        assert any("does not reference a node" in e.message for e in result.errors)

    def test_dangling_target_reference_is_rejected(self):
        result = graph_import.validate_import_document(
            _doc([_node("n1")], [_edge("e1", "n1", "does-not-exist")])
        )
        assert not result.valid
        assert any("does not reference a node" in e.message for e in result.errors)

    def test_a_reference_to_the_OLD_live_graph_is_still_dangling(self):
        # v1 is REPLACE-only: an edge may only reference a node id present in
        # THIS document, never one that merely exists in the graph currently
        # running (there is no merge/collision policy in this version).
        result = graph_import.validate_import_document(
            _doc([_node("n1")], [_edge("e1", "n1", "some-id-from-the-old-graph")])
        )
        assert not result.valid

    def test_missing_edge_endpoints_are_rejected(self):
        result = graph_import.validate_import_document(
            _doc([_node("n1")], [{"id": "e1", "source": "n1"}])
        )
        assert not result.valid


class TestApplicability:
    def test_applicability_violation_is_surfaced(self, monkeypatch):
        def _deny(*args, **kwargs):
            return {"allowed": False, "message": "not allowed between these types"}

        monkeypatch.setattr(
            graph_import.config_loader, "relationship_type_allows_node_types", _deny
        )

        result = graph_import.validate_import_document(
            _doc(
                [_node("n1", "Actor"), _node("n2", "Initiative")],
                [_edge("e1", "n1", "n2", "BELONGS_TO")],
            )
        )
        assert not result.valid
        assert any(
            "not allowed between these types" in e.message for e in result.errors
        )


class TestSizeLimits:
    def test_too_many_nodes_is_rejected(self, monkeypatch):
        monkeypatch.setattr(graph_import, "MAX_IMPORT_NODES", 2)
        result = graph_import.validate_import_document(
            _doc([_node("n1"), _node("n2"), _node("n3")])
        )
        assert not result.valid

    def test_too_many_edges_is_rejected(self, monkeypatch):
        monkeypatch.setattr(graph_import, "MAX_IMPORT_EDGES", 1)
        result = graph_import.validate_import_document(
            _doc(
                [_node("n1"), _node("n2"), _node("n3")],
                [_edge("e1", "n1", "n2"), _edge("e2", "n2", "n3")],
            )
        )
        assert not result.valid


class TestReportedIssueCap:
    def test_problems_are_counted_but_only_the_first_are_kept(self, monkeypatch):
        monkeypatch.setattr(graph_import, "MAX_REPORTED_ISSUES", 5)
        nodes = [_node("n1"), _node("n2")]
        edges = [_edge(f"e{i}", "n1", "ghost") for i in range(40)]
        result = graph_import.validate_import_document(_doc(nodes, edges))
        assert not result.valid
        assert result.error_count == 40
        assert [e.location for e in result.errors] == [f"edges[{i}]" for i in range(5)]
        assert result.nodes == [] and result.edges == []
        assert result.to_dict()["error_count"] == 40
        assert len(result.to_dict()["errors"]) == 5

    def test_a_shape_error_reports_its_own_count(self):
        result = graph_import.validate_import_document({"edges": []})
        assert result.error_count == len(result.errors) == 1

    def test_the_default_cap_bounds_a_document_of_bad_edges(self):
        edges = [_edge(f"e{i}", "n1", "ghost") for i in range(3_000)]
        result = graph_import.validate_import_document(_doc([_node("n1")], edges))
        assert result.error_count == 3_000
        assert len(result.errors) == graph_import.MAX_REPORTED_ISSUES

    @pytest.mark.parametrize("unconfigured_end", ["source", "target"])
    def test_an_unconfigured_node_type_is_reported_once_not_per_edge(
        self, unconfigured_end
    ):
        long_type = "X" * 100_000
        nodes = [_node("big", long_type), _node("n2", "Initiative")]
        ends = ("big", "n2") if unconfigured_end == "source" else ("n2", "big")
        edges = [_edge(f"e{i}", *ends, "IMPLEMENTS") for i in range(200)]
        result = graph_import.validate_import_document(_doc(nodes, edges))
        assert result.error_count == 1
        assert result.errors[0].location == "nodes[0]"

    def test_an_inapplicable_edge_message_is_cut(self, monkeypatch):
        monkeypatch.setattr(
            graph_import.config_loader,
            "relationship_type_allows_node_types",
            lambda *args, **kwargs: {"allowed": False, "message": "m" * 100_000},
        )
        result = graph_import.validate_import_document(
            _doc([_node("n1"), _node("n2")], [_edge("e1", "n1", "n2")])
        )
        assert len(result.errors[0].message) <= 600


class TestInvalidEdgeMessage:
    def test_an_edge_with_many_bad_fields_gets_a_bounded_message(self):
        bad = [1] * 5
        edge = _edge("e1")
        edge.update({f: bad for f in ("label", "metadata", "archived", "created_at")})
        edge["revision"] = bad
        result = graph_import.validate_import_document(
            _doc([_node("n1"), _node("n2")], [edge])
        )
        message = result.errors[0].message
        assert message.startswith("invalid edge: 5 validation errors")
        assert len(message) <= 520
