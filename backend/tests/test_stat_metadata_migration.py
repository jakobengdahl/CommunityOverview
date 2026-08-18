"""Regression tests for the MetaPlus step-1 migration script.

Every case here is one a review caught after the script had already been run
against a live graph, which is the reason they are pinned rather than assumed.
"""

import importlib.util
import json
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "migrate_stat_metadata_metaplus.py"


def load_module():
    spec = importlib.util.spec_from_file_location("metaplus_migration", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mig = load_module()


def classification(codes, node_id="cls-1", name="NACE"):
    return {
        "id": node_id,
        "type": "CodeList",
        "name": name,
        "subtypes": ["Statistical Classification"],
        "tags": [],
        "metadata": {"version": "Rev. 2", "codes": codes},
    }


def graph_with(*nodes, edges=None):
    return {"nodes": list(nodes), "edges": list(edges or [])}


def items_of(graph, parent_id):
    by_id = {n["id"]: n for n in graph["nodes"]}
    return [
        by_id[e["target"]]
        for e in graph["edges"]
        if e.get("type") == "HAS_ITEM" and e["source"] == parent_id
    ]


def test_a_removed_code_removes_its_derived_item():
    graph = graph_with(
        classification([{"code": "A", "label": "Ay"}, {"code": "B", "label": "Bee"}])
    )
    graph, _ = mig.migrate(graph)
    assert {i["metadata"]["code"] for i in items_of(graph, "cls-1")} == {"A", "B"}

    graph["nodes"][0]["metadata"]["codes"] = [{"code": "B", "label": "Bee"}]
    graph, report = mig.migrate(graph)

    assert {i["metadata"]["code"] for i in items_of(graph, "cls-1")} == {"B"}
    assert len(report["classification_items_removed"]) == 1


def test_a_relabelled_code_propagates():
    graph = graph_with(classification([{"code": "A", "label": "Old"}]))
    graph, _ = mig.migrate(graph)
    graph["nodes"][0]["metadata"]["codes"] = [{"code": "A", "label": "New"}]
    graph, report = mig.migrate(graph)

    assert items_of(graph, "cls-1")[0]["name"] == "New"
    assert len(report["classification_items_updated"]) == 1


def test_display_order_stays_contiguous_after_curation():
    graph = graph_with(classification([{"code": "A"}, {"code": "B"}, {"code": "C"}]))
    graph, _ = mig.migrate(graph)
    graph["nodes"][0]["metadata"]["codes"] = [
        {"code": "Z"},
        {"code": "B"},
        {"code": "C"},
    ]
    graph, _ = mig.migrate(graph)

    orders = sorted(i["metadata"]["display_order"] for i in items_of(graph, "cls-1"))
    assert orders == [0, 1, 2]


def test_display_order_skips_no_positions_for_malformed_entries():
    graph = graph_with(classification(["junk", {"label": "no code"}, {"code": "07"}]))
    graph, _ = mig.migrate(graph)
    assert items_of(graph, "cls-1")[0]["metadata"]["display_order"] == 0


def test_an_authored_item_is_never_rewritten_or_duplicated():
    authored = {
        "id": "authored-1",
        "type": "ClassificationItem",
        "name": "Hand written",
        "subtypes": [],
        "tags": [],
        "metadata": {"code": "A"},
    }
    edge = {
        "id": "e1",
        "source": "cls-1",
        "target": "authored-1",
        "type": "HAS_ITEM",
        "metadata": {},
    }
    graph = graph_with(
        classification([{"code": "A", "label": "Generated"}]), authored, edges=[edge]
    )
    graph, report = mig.migrate(graph)

    items = items_of(graph, "cls-1")
    assert len(items) == 1, "a second item was generated alongside the authored one"
    assert items[0]["name"] == "Hand written"
    assert not report["classification_items_created"]


def test_conversion_records_the_decision_on_the_node():
    """Dropping the marker subtype must not leave the conversion unreproducible."""
    graph = graph_with(classification([]))
    graph, _ = mig.migrate(graph)
    node = graph["nodes"][0]

    assert node["type"] == "Classification"
    assert node["metadata"]["is_classification"] is True
    assert mig.CLASSIFICATION_SUBTYPE_MARKER not in node["subtypes"]


def test_enrichment_reports_nothing_applied_when_nothing_changed():
    node = {
        "id": "n1",
        "type": "CodeList",
        "name": "COICOP 2018",
        "subtypes": [],
        "tags": [],
        "metadata": {"is_classification": True, "codes": [{"code": "01"}]},
    }
    graph = graph_with(node)
    log = mig.enrich(
        graph,
        {
            "nodes": [
                {
                    "match_name": "COICOP 2018",
                    "match_type": "CodeList",
                    "metadata": {"is_classification": True},
                }
            ]
        },
    )
    assert log[0]["status"] == "already complete — nothing applied"


def test_enrichment_repairs_a_present_but_falsy_value():
    """is_classification: false and codes: [] are the stub shapes it exists for."""
    node = {
        "id": "n1",
        "type": "CodeList",
        "name": "COICOP 2018",
        "subtypes": [],
        "tags": [],
        "metadata": {"is_classification": False, "codes": []},
    }
    graph = graph_with(node)
    mig.enrich(
        graph,
        {
            "nodes": [
                {
                    "match_name": "COICOP 2018",
                    "match_type": "CodeList",
                    "metadata": {"is_classification": True, "codes": [{"code": "01"}]},
                }
            ]
        },
    )
    assert node["metadata"]["is_classification"] is True
    assert node["metadata"]["codes"] == [{"code": "01"}]


def test_enrichment_still_reaches_an_already_converted_node():
    """Pinning only the pre-migration type made the file inert after run one."""
    node = {
        "id": "n1",
        "type": "Classification",
        "name": "COICOP 2018",
        "subtypes": ["ClassificationVersion"],
        "tags": [],
        "metadata": {"is_classification": True},
    }
    graph = graph_with(node)
    log = mig.enrich(
        graph,
        {
            "nodes": [
                {
                    "match_name": "COICOP 2018",
                    "match_type": "CodeList",
                    "metadata": {"purpose": "added later"},
                }
            ]
        },
    )
    assert node["metadata"]["purpose"] == "added later"
    assert log[0]["status"] == "enriched"


@pytest.mark.parametrize(
    "tag", ["classification", "statistical-classification", "official classification"]
)
def test_hyphenated_tags_still_reach_manual_review(tag):
    node = {
        "id": "n1",
        "type": "CodeList",
        "name": "Something",
        "subtypes": [],
        "tags": [tag],
        "metadata": {},
    }
    graph = graph_with(node)
    _, report = mig.migrate(graph)
    assert [c["name"] for c in report["left_for_manual_review"]] == ["Something"]


def test_write_is_atomic(tmp_path):
    """A truncate-then-write against a live graph has no recovery path."""
    target = tmp_path / "graph.json"
    target.write_text(json.dumps({"nodes": [], "edges": []}))
    mig.write_graph(str(target), {"nodes": [{"id": "x"}], "edges": []})

    assert json.loads(target.read_text())["nodes"] == [{"id": "x"}]
    assert not (tmp_path / "graph.json.tmp").exists()


def test_duplicate_edge_ids_abort_before_any_write():
    graph = graph_with(
        {
            "id": "a",
            "type": "Concept",
            "name": "A",
            "subtypes": [],
            "tags": [],
            "metadata": {},
        },
        edges=[
            {
                "id": "dup",
                "source": "a",
                "target": "a",
                "type": "RELATES_TO",
                "metadata": {},
            },
            {
                "id": "dup",
                "source": "a",
                "target": "a",
                "type": "RELATES_TO",
                "metadata": {},
            },
        ],
    )
    with pytest.raises(SystemExit, match="duplicate edge ids"):
        mig.migrate(graph)


def test_removal_takes_every_edge_touching_the_item():
    """A leftover edge would leave a dangling reference and abort the migration."""
    graph = graph_with(classification([{"code": "A"}]))
    graph, _ = mig.migrate(graph)
    item_id = items_of(graph, "cls-1")[0]["id"]
    graph["nodes"].append(
        {
            "id": "other",
            "type": "Concept",
            "name": "O",
            "subtypes": [],
            "tags": [],
            "metadata": {},
        }
    )
    graph["edges"].append(
        {
            "id": "ref1",
            "source": "other",
            "target": item_id,
            "type": "RELATES_TO",
            "metadata": {},
        }
    )
    graph["nodes"][0]["metadata"]["codes"] = []

    graph, _ = mig.migrate(graph)  # must not raise
    assert not any(e["id"] == "ref1" for e in graph["edges"])


def test_a_duplicate_code_is_reported_and_leaves_no_order_gap():
    graph = graph_with(
        classification([{"code": "A"}, {"code": "B"}, {"code": "A"}, {"code": "C"}])
    )
    graph, report = mig.migrate(graph)

    orders = sorted(i["metadata"]["display_order"] for i in items_of(graph, "cls-1"))
    assert orders == [0, 1, 2]
    assert report["duplicate_codes"][0]["codes"] == ["A"]


def test_codes_of_mixed_type_do_not_silently_collapse():
    graph = graph_with(classification([{"code": 1}, {"code": "1"}, {"code": "2"}]))
    graph, report = mig.migrate(graph)

    orders = sorted(i["metadata"]["display_order"] for i in items_of(graph, "cls-1"))
    assert orders == [0, 1]
    assert report["duplicate_codes"], "the collapsed code was dropped without a word"


def test_malformed_codes_never_delete_anything():
    graph = graph_with(classification([{"code": "A"}]))
    graph, _ = mig.migrate(graph)
    graph["nodes"][0]["metadata"]["codes"] = {"not": "a list"}

    graph, report = mig.migrate(graph)
    assert len(items_of(graph, "cls-1")) == 1
    assert report["malformed_codes"]


def test_an_item_without_a_code_is_left_alone():
    orphan = {
        "id": "no-code",
        "type": "ClassificationItem",
        "name": "Codeless",
        "subtypes": [],
        "tags": [],
        "metadata": {"generated": True},
    }
    edge = {
        "id": "e1",
        "source": "cls-1",
        "target": "no-code",
        "type": "HAS_ITEM",
        "metadata": {},
    }
    graph = graph_with(
        classification([{"code": None, "label": "Null code"}]), orphan, edges=[edge]
    )

    graph, report = mig.migrate(graph)
    assert any(n["id"] == "no-code" for n in graph["nodes"])
    assert report["items_without_code"]


def test_enrichment_does_not_overwrite_a_legitimate_zero():
    node = {
        "id": "n1",
        "type": "CodeList",
        "name": "COICOP 2018",
        "subtypes": [],
        "tags": [],
        "metadata": {"level_count": 0},
    }
    graph = graph_with(node)
    mig.enrich(
        graph,
        {
            "nodes": [
                {
                    "match_name": "COICOP 2018",
                    "match_type": "CodeList",
                    "metadata": {"level_count": 99},
                }
            ]
        },
    )
    assert node["metadata"]["level_count"] == 0


def test_enrichment_logs_one_entry_per_node():
    node = {
        "id": "n1",
        "type": "Classification",
        "name": "COICOP 2018",
        "subtypes": [],
        "tags": [],
        "metadata": {},
    }
    graph = graph_with(node)
    log = mig.enrich(
        graph,
        {
            "nodes": [
                {
                    "match_name": "COICOP 2018",
                    "match_type": "Classification",
                    "metadata": {"purpose": "x"},
                }
            ]
        },
    )
    assert len(log) == 1
