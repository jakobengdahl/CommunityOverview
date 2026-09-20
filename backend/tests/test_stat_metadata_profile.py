"""Integrity checks for the bundled stat-metadata profile."""

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
PROFILE_DIR = REPO_ROOT / "config" / "stat-metadata"


def _load_json(name: str) -> dict:
    return json.loads((PROFILE_DIR / name).read_text(encoding="utf-8"))


def _node_type_candidates(node: dict) -> set[str]:
    return {node["type"], *node.get("subtypes", [])}


def _allows(rules: list[str], node: dict) -> bool:
    return not rules or "*" in rules or bool(set(rules) & _node_type_candidates(node))


def test_stat_metadata_coding_edges_match_configured_profile_rules():
    schema = _load_json("schema_config.json")["schema"]
    graph = _load_json("graph.json")
    nodes = {node["id"]: node for node in graph["nodes"]}
    checked_relationships = {"USES_CODE_LIST", "USES_CLASSIFICATION"}

    assert schema["relationship_types"]["USES_CLASSIFICATION"]["source_types"] == [
        "InstanceVariable",
        "ValueDomain",
    ]
    assert schema["relationship_types"]["USES_CLASSIFICATION"]["target_types"] == [
        "Classification"
    ]

    violations = []
    for edge in graph["edges"]:
        if edge["type"] not in checked_relationships:
            continue

        source = nodes.get(edge.get("source"))
        target = nodes.get(edge.get("target"))
        if source is None or target is None:
            continue

        relationship_config = schema["relationship_types"].get(edge["type"], {})
        source_rules = relationship_config.get("source_types", [])
        target_rules = relationship_config.get("target_types", [])
        if _allows(source_rules, source) and _allows(target_rules, target):
            continue

        violations.append(
            {
                "edge_id": edge["id"],
                "type": edge["type"],
                "source": source["type"],
                "source_subtypes": source.get("subtypes", []),
                "target": target["type"],
                "target_subtypes": target.get("subtypes", []),
            }
        )

    assert violations == []


def test_stat_metadata_code_list_edges_do_not_target_classifications():
    graph = _load_json("graph.json")
    nodes = {node["id"]: node for node in graph["nodes"]}

    offenders = [
        edge["id"]
        for edge in graph["edges"]
        if edge["type"] == "USES_CODE_LIST"
        and nodes.get(edge.get("target"), {}).get("type") == "Classification"
    ]

    assert offenders == []


def test_stat_metadata_register_population_demo_slice_is_present():
    graph = _load_json("graph.json")
    nodes = {node["id"]: node for node in graph["nodes"]}
    edges = {edge["id"]: edge for edge in graph["edges"]}

    assert nodes["register-total-population"]["type"] == "DataSet"
    assert "Register" in nodes["register-total-population"]["subtypes"]
    assert "RegisterVariant" in nodes["register-variant-resident-persons"]["subtypes"]
    assert "RegisterVersion" in nodes["register-version-resident-persons-2025"]["subtypes"]
    assert nodes["population-registered-residents-sweden"]["type"] == "Population"

    expected_edges = {
        "edge-popstats-produces-total-population-register": (
            "bf0d2ed9-851f-4351-b388-fd8a14db12a4",
            "register-total-population",
            "PRODUCES",
        ),
        "edge-total-population-register-has-resident-variant": (
            "register-total-population",
            "register-variant-resident-persons",
            "HAS_VARIANT",
        ),
        "edge-resident-variant-has-2025-version": (
            "register-variant-resident-persons",
            "register-version-resident-persons-2025",
            "HAS_VERSION",
        ),
        "edge-resident-version-has-population": (
            "register-version-resident-persons-2025",
            "population-registered-residents-sweden",
            "HAS_POPULATION",
        ),
        "edge-registered-residents-of-person": (
            "population-registered-residents-sweden",
            "9a4aee96-ff85-4c21-8da2-460f165cc486",
            "OF_UNIT_TYPE",
        ),
        "edge-resident-version-has-region-variable": (
            "register-version-resident-persons-2025",
            "d26ea773-50c2-4eda-a2e0-eceed76550ae",
            "HAS_VARIABLE",
        ),
        "edge-pop-region-uses-nuts": (
            "d26ea773-50c2-4eda-a2e0-eceed76550ae",
            "cl-region",
            "USES_CLASSIFICATION",
        ),
    }

    for edge_id, (source, target, edge_type) in expected_edges.items():
        edge = edges[edge_id]
        assert (edge["source"], edge["target"], edge["type"]) == (
            source,
            target,
            edge_type,
        )

    assert edges["edge-resident-version-has-population"]["metadata"] == {
        "reference_date": "2025-12-31",
        "coverage_basis": "Resident registration at reference date",
    }
    assert "question_hint" in edges["edge-pop-region-uses-nuts"]["metadata"]


def test_stat_metadata_register_population_edges_match_profile_rules():
    schema = _load_json("schema_config.json")["schema"]
    graph = _load_json("graph.json")
    nodes = {node["id"]: node for node in graph["nodes"]}
    checked_edge_ids = {
        "edge-total-population-register-has-resident-variant",
        "edge-resident-variant-has-2025-version",
        "edge-resident-version-has-population",
        "edge-registered-residents-of-person",
        "edge-resident-version-has-region-variable",
        "edge-pop-region-uses-nuts",
    }

    violations = []
    for edge in graph["edges"]:
        if edge["id"] not in checked_edge_ids:
            continue

        source = nodes[edge["source"]]
        target = nodes[edge["target"]]
        relationship_config = schema["relationship_types"][edge["type"]]
        if _allows(
            relationship_config.get("source_types", []), source
        ) and _allows(relationship_config.get("target_types", []), target):
            continue

        violations.append(edge["id"])

    assert violations == []
