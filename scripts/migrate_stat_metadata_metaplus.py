#!/usr/bin/env python3
"""Idempotent MetaPlus step-1 data migration for a stat-metadata graph.json.

Transforms, preserving every node id, edge id, subtype and unknown metadata key:

  1. InstanceVariable -> Variable, gaining subtype "InstanceVariable".
  2. CodeList -> Classification, but only for nodes that clearly declare
     themselves a formal statistical classification (metadata.is_classification
     is true, or subtype "Statistical Classification"). A version string promotes
     the node to subtype ClassificationVersion, otherwise ClassificationSeries.
  3. ClassificationItem nodes + HAS_ITEM edges for classifications that carry
     real codes in metadata.codes. No code, level or hierarchy is ever invented,
     and metadata.codes is left in place.

Anything ambiguous is left untouched and listed in the report. Where a demo node
is ambiguous only because it is a stub, --enrich supplies the missing metadata
from a reviewable file before the rule runs, instead of relaxing the rule.

Re-running is a no-op: every step tests for the end state before acting.

Usage:
  migrate_stat_metadata_metaplus.py <graph.json> [--write] [--report report.json]
                                    [--no-items] [--enrich demo_enrichment.json]
"""

import json
import uuid
import argparse
import hashlib
from collections import Counter

CLASSIFICATION_SUBTYPE_MARKER = "Statistical Classification"


def is_classification(node):
    """Only an explicit declaration counts. Name or tag alone is not enough."""
    meta = node.get("metadata") or {}
    if meta.get("is_classification") is True:
        return True
    if CLASSIFICATION_SUBTYPE_MARKER in (node.get("subtypes") or []):
        return True
    return False


def looks_like_classification_but_unconfirmed(node):
    """Signals a human should look, without being enough to act on."""
    tags = [t.lower() for t in (node.get("tags") or [])]
    name = (node.get("name") or "").lower()
    hints = ("classification", "klassifikation", "standard")
    return any(h in tags for h in hints) or any(
        k in name for k in ("nace", "coicop", "nuts", "isco", "isic", "cpa")
    )


def add_subtype(node, subtype):
    subs = list(node.get("subtypes") or [])
    if subtype not in subs:
        subs.append(subtype)
        node["subtypes"] = subs
        return True
    node["subtypes"] = subs
    return False


def stable_item_id(classification_id, code):
    """Deterministic id so re-running never creates a second item for a code."""
    seed = f"{classification_id}:{code}"
    return str(uuid.UUID(hashlib.sha256(seed.encode()).hexdigest()[:32]))


def enrich(graph, enrichment):
    """Fill in metadata a demo node was missing, before anything is classified.

    Kept separate from the conversion rule on purpose: the rule stays strict, and
    every node that only passes it because of an enrichment is visible in one
    reviewable file rather than hidden in a loosened heuristic.
    """
    applied = []
    by_name = {}
    for node in graph["nodes"]:
        by_name.setdefault((node.get("name"), node.get("type")), []).append(node)

    for entry in enrichment.get("nodes", []):
        key = (entry["match_name"], entry["match_type"])
        targets = by_name.get(key, [])
        if not targets:
            applied.append({"match": key, "status": "no matching node — skipped"})
            continue
        for node in targets:
            for field in ("description", "summary"):
                if field in entry and not (node.get(field) or "").strip():
                    node[field] = entry[field]
            meta = dict(node.get("metadata") or {})
            for k, v in (entry.get("metadata") or {}).items():
                meta.setdefault(k, v)
            node["metadata"] = meta
            tags = list(node.get("tags") or [])
            for t in entry.get("tags_add", []):
                if t not in tags:
                    tags.append(t)
            node["tags"] = tags
            applied.append({"match": key, "id": node["id"], "status": "enriched"})
    return applied


def migrate(graph, create_items=True):
    nodes = graph["nodes"]
    edges = graph["edges"]
    by_id = {n["id"]: n for n in nodes}
    report = {
        "variables_migrated": [],
        "variables_already_migrated": 0,
        "classifications_converted": [],
        "classification_items_created": [],
        "left_for_manual_review": [],
        "codelists_kept": [],
        "counts_before": {
            "nodes": len(nodes),
            "edges": len(edges),
            "by_type": dict(Counter(n["type"] for n in nodes)),
        },
    }

    # ---- 1. InstanceVariable -> Variable -------------------------------------
    for node in nodes:
        if node["type"] == "InstanceVariable":
            node["type"] = "Variable"
            add_subtype(node, "InstanceVariable")
            report["variables_migrated"].append(
                {
                    "id": node["id"],
                    "name": node.get("name"),
                    "subtypes": node.get("subtypes"),
                }
            )
        elif node["type"] == "Variable" and "InstanceVariable" in (
            node.get("subtypes") or []
        ):
            report["variables_already_migrated"] += 1

    # ---- 2. CodeList -> Classification ---------------------------------------
    converted_ids = []
    for node in nodes:
        if node["type"] != "CodeList":
            continue
        if not is_classification(node):
            entry = {"id": node["id"], "name": node.get("name")}
            if looks_like_classification_but_unconfirmed(node):
                entry["reason"] = (
                    "name or tags suggest a classification, but no "
                    "metadata.is_classification and no "
                    f"'{CLASSIFICATION_SUBTYPE_MARKER}' subtype — not converted"
                )
                report["left_for_manual_review"].append(entry)
            else:
                report["codelists_kept"].append(entry)
            continue

        node["type"] = "Classification"
        meta = node.get("metadata") or {}
        subtype = (
            "ClassificationVersion" if meta.get("version") else "ClassificationSeries"
        )
        add_subtype(node, subtype)
        # The marker subtype has served its purpose now that the type says it.
        node["subtypes"] = [
            s for s in node["subtypes"] if s != CLASSIFICATION_SUBTYPE_MARKER
        ]
        converted_ids.append(node["id"])
        report["classifications_converted"].append(
            {
                "id": node["id"],
                "name": node.get("name"),
                "subtypes": node.get("subtypes"),
                "version": meta.get("version"),
            }
        )

    # Nodes already of type Classification from an earlier run are eligible too.
    for node in nodes:
        if node["type"] == "Classification" and node["id"] not in converted_ids:
            converted_ids.append(node["id"])

    # ---- 3. ClassificationItem nodes from real codes --------------------------
    if create_items:
        existing_item_ids = {n["id"] for n in nodes}
        existing_has_item = {
            (e["source"], e["target"]) for e in edges if e.get("type") == "HAS_ITEM"
        }
        for cid in converted_ids:
            parent = by_id.get(cid)
            if parent is None:
                continue
            codes = (parent.get("metadata") or {}).get("codes") or []
            if not isinstance(codes, list):
                continue
            for order, entry in enumerate(codes):
                if not isinstance(entry, dict) or "code" not in entry:
                    continue
                item_id = stable_item_id(cid, entry["code"])
                if item_id not in existing_item_ids:
                    nodes.append(
                        {
                            "id": item_id,
                            "type": "ClassificationItem",
                            "name": entry.get("label") or entry["code"],
                            "description": "",
                            "summary": "",
                            "tags": [],
                            "subtypes": [],
                            "metadata": {
                                "code": entry["code"],
                                "display_order": order,
                                # Flags that this node was derived from the parent's
                                # metadata.codes rather than authored directly.
                                "generated": True,
                            },
                            "created_at": parent.get("created_at"),
                            "updated_at": parent.get("updated_at"),
                        }
                    )
                    existing_item_ids.add(item_id)
                    report["classification_items_created"].append(
                        {
                            "id": item_id,
                            "code": entry["code"],
                            "name": entry.get("label") or entry["code"],
                            "classification": parent.get("name"),
                        }
                    )
                if (cid, item_id) not in existing_has_item:
                    edges.append(
                        {
                            "id": stable_item_id(cid, f"edge:{entry['code']}"),
                            "source": cid,
                            "target": item_id,
                            "type": "HAS_ITEM",
                            "label": "",
                            "metadata": {},
                            "created_at": parent.get("created_at"),
                        }
                    )
                    existing_has_item.add((cid, item_id))

    # ---- validation -----------------------------------------------------------
    ids = {n["id"] for n in nodes}
    dangling = [
        e["id"] for e in edges if e["source"] not in ids or e["target"] not in ids
    ]
    if dangling:
        raise SystemExit(
            f"ABORT: {len(dangling)} edges point at missing nodes: {dangling[:5]}"
        )
    if len(ids) != len(nodes):
        raise SystemExit("ABORT: duplicate node ids produced")
    if any(n["type"] == "InstanceVariable" for n in nodes):
        raise SystemExit("ABORT: InstanceVariable nodes remain after migration")

    report["counts_after"] = {
        "nodes": len(nodes),
        "edges": len(edges),
        "by_type": dict(Counter(n["type"] for n in nodes)),
    }
    return graph, report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("graph")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--report")
    ap.add_argument("--no-items", action="store_true")
    ap.add_argument(
        "--enrich",
        help="JSON file supplying metadata that demo nodes are missing, applied "
        "before classification detection",
    )
    args = ap.parse_args()

    with open(args.graph, encoding="utf-8") as f:
        graph = json.load(f)

    before_nodes = len(graph["nodes"])
    before_edges = len(graph["edges"])

    enrichment_log = []
    if args.enrich:
        with open(args.enrich, encoding="utf-8") as f:
            enrichment_log = enrich(graph, json.load(f))

    graph, report = migrate(graph, create_items=not args.no_items)
    report["enrichment"] = enrichment_log

    print(f"=== {args.graph} ===")
    print(
        f"nodes {before_nodes} -> {report['counts_after']['nodes']}   "
        f"edges {before_edges} -> {report['counts_after']['edges']}"
    )
    for e in enrichment_log:
        print(f"enrichment: {e['match'][0]!r} — {e['status']}")
    print(
        f"InstanceVariable -> Variable      : {len(report['variables_migrated'])}"
        f"  (already migrated: {report['variables_already_migrated']})"
    )
    print(
        f"CodeList -> Classification        : {len(report['classifications_converted'])}"
    )
    for c in report["classifications_converted"]:
        print(f"    • {c['name']}  subtypes={c['subtypes']}")
    print(
        f"ClassificationItem created        : {len(report['classification_items_created'])}"
    )
    print(f"CodeList kept as CodeList         : {len(report['codelists_kept'])}")
    print(
        f"Left for manual review            : {len(report['left_for_manual_review'])}"
    )
    for c in report["left_for_manual_review"]:
        print(f"    • {c['name']} — {c['reason']}")

    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"\nReport: {args.report}")

    if args.write:
        with open(args.graph, "w", encoding="utf-8") as f:
            json.dump(graph, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print(f"Written: {args.graph}")
    else:
        print("\n(dry run — pass --write to apply)")


if __name__ == "__main__":
    main()
