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

    assert schema["relationship_types"]["USES_CLASSIFICATION"][
        "source_types"
    ] == ["InstanceVariable", "ValueDomain"]
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
