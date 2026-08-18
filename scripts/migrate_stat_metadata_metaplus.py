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

Re-running CONVERGES rather than merely skipping: derived items are created,
updated and removed to match metadata.codes, which stays the source of truth.
Items a human authored are never rewritten or deleted.

Usage:
  migrate_stat_metadata_metaplus.py <graph.json> [--write] [--report report.json]
                                    [--no-items] [--enrich demo_enrichment.json]
"""

import argparse
import hashlib
import json
import os
import uuid
from collections import Counter

CLASSIFICATION_SUBTYPE_MARKER = "Statistical Classification"
CLASSIFICATION_HINTS = ("classification", "klassifikation", "standard")
CLASSIFICATION_NAME_HINTS = ("nace", "coicop", "nuts", "isco", "isic", "cpa")


def is_classification(node):
    """Only an explicit declaration counts. Name or tag alone is not enough."""
    meta = node.get("metadata") or {}
    if meta.get("is_classification") is True:
        return True
    if CLASSIFICATION_SUBTYPE_MARKER in (node.get("subtypes") or []):
        return True
    return False


def looks_like_classification_but_unconfirmed(node):
    """Signals a human should look, without being enough to act on.

    Tags are matched as substrings, not by equality: the tag that actually
    occurs is `statistical-classification`, which an equality test misses — and
    missing it means the node is filed as an ordinary code list instead of being
    surfaced for review, which is the one outcome this check exists to prevent.
    """
    tags = [t.lower() for t in (node.get("tags") or [])]
    name = (node.get("name") or "").lower()
    return any(h in tag for tag in tags for h in CLASSIFICATION_HINTS) or any(
        k in name for k in CLASSIFICATION_NAME_HINTS
    )


def add_subtype(node, subtype):
    subs = list(node.get("subtypes") or [])
    if subtype not in subs:
        subs.append(subtype)
    node["subtypes"] = subs


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
    by_key = {}
    for node in graph["nodes"]:
        by_key.setdefault((node.get("name"), node.get("type")), []).append(node)

    for entry in enrichment.get("nodes", []):
        name = entry["match_name"]
        # Match the post-migration type as well as the declared one. Pinning only
        # the original type made the file inert after the first run: the node is
        # a Classification by then, so a later correction to the enrichment —
        # a fixed label, an added division — could never reach it again.
        targets = []
        for node_type in (entry["match_type"], "Classification"):
            targets.extend(by_key.get((name, node_type), []))
        if not targets:
            applied.append(
                {
                    "match": [name, entry["match_type"]],
                    "status": "no matching node — skipped",
                }
            )
            continue

        for node in targets:
            changed = []
            for field in ("description", "summary"):
                if field in entry and not (node.get(field) or "").strip():
                    node[field] = entry[field]
                    changed.append(field)
            meta = dict(node.get("metadata") or {})
            for key, value in (entry.get("metadata") or {}).items():
                # `setdefault` would treat a present-but-falsy value as already
                # set — is_classification: false and codes: [] are exactly the
                # stub shapes enrichment exists to repair, and both are falsy.
                if key not in meta or meta[key] in (None, "", [], {}, False):
                    if meta.get(key) != value:
                        meta[key] = value
                        changed.append(f"metadata.{key}")
            node["metadata"] = meta
            tags = list(node.get("tags") or [])
            for tag in entry.get("tags_add", []):
                if tag not in tags:
                    tags.append(tag)
                    changed.append(f"tag:{tag}")
            node["tags"] = tags
            applied.append(
                {
                    "match": [name, node.get("type")],
                    "id": node["id"],
                    # Reported from what actually changed. Claiming "enriched"
                    # unconditionally hid the case where nothing was applied and
                    # the node then failed the conversion rule anyway.
                    "status": "enriched"
                    if changed
                    else "already complete — nothing applied",
                    "changed": changed,
                }
            )
    return applied


def _sync_items(graph, parent, report):
    """Make the derived items match metadata.codes, in both directions.

    Creating only what is missing does not converge: a removed code leaves an
    orphan linked to the classification, a relabelled code never propagates, and
    display_order goes stale and starts colliding. metadata.codes is documented
    as the source of truth, which invites exactly that curation.

    Items a human authored (no metadata.generated) are never rewritten or
    removed — but they do claim their code, so no duplicate is generated
    alongside one.
    """
    nodes, edges = graph["nodes"], graph["edges"]
    by_id = {n["id"]: n for n in nodes}
    cid = parent["id"]

    raw = (parent.get("metadata") or {}).get("codes") or []
    if not isinstance(raw, list):
        return
    # Enumerate the filtered list: indexing the raw one leaks the positions of
    # entries that were skipped, leaving gaps in display_order.
    wanted = {}
    for order, entry in enumerate(
        [e for e in raw if isinstance(e, dict) and "code" in e]
    ):
        wanted[str(entry["code"])] = (order, entry)

    existing = {}
    for edge in edges:
        if edge.get("type") != "HAS_ITEM" or edge.get("source") != cid:
            continue
        item = by_id.get(edge.get("target"))
        if item is None or item.get("type") != "ClassificationItem":
            continue
        # Keyed on the code, not on the generated id: an item authored by hand
        # or under an older id scheme is invisible to an id-based check, and a
        # second item for the same code would be created next to it.
        code = str((item.get("metadata") or {}).get("code"))
        existing.setdefault(code, []).append((edge, item))

    for code, (order, entry) in wanted.items():
        label = entry.get("label") or code
        found = existing.get(code)
        if found:
            _, item = found[0]
            meta = item.get("metadata") or {}
            if meta.get("generated") is not True:
                continue
            if item.get("name") != label or meta.get("display_order") != order:
                item["name"] = label
                meta["display_order"] = order
                item["metadata"] = meta
                report["classification_items_updated"].append(
                    {
                        "id": item["id"],
                        "code": code,
                        "name": label,
                        "classification": parent.get("name"),
                    }
                )
            continue

        item_id = stable_item_id(cid, code)
        nodes.append(
            {
                "id": item_id,
                "type": "ClassificationItem",
                "name": label,
                "description": "",
                "summary": "",
                "tags": [],
                "subtypes": [],
                "metadata": {"code": code, "display_order": order, "generated": True},
                "created_at": parent.get("created_at"),
                "updated_at": parent.get("updated_at"),
            }
        )
        edges.append(
            {
                "id": stable_item_id(cid, f"edge:{code}"),
                "source": cid,
                "target": item_id,
                "type": "HAS_ITEM",
                "label": "",
                "metadata": {},
                "created_at": parent.get("created_at"),
            }
        )
        report["classification_items_created"].append(
            {
                "id": item_id,
                "code": code,
                "name": label,
                "classification": parent.get("name"),
            }
        )

    for code, found in existing.items():
        if code in wanted:
            continue
        for edge, item in found:
            if (item.get("metadata") or {}).get("generated") is not True:
                report["authored_items_kept"].append(
                    {
                        "id": item["id"],
                        "code": code,
                        "classification": parent.get("name"),
                    }
                )
                continue
            graph["nodes"] = [n for n in graph["nodes"] if n["id"] != item["id"]]
            graph["edges"] = [e for e in graph["edges"] if e["id"] != edge["id"]]
            nodes, edges = graph["nodes"], graph["edges"]
            report["classification_items_removed"].append(
                {"id": item["id"], "code": code, "classification": parent.get("name")}
            )


def migrate(graph, create_items=True):
    nodes = graph["nodes"]
    edges = graph["edges"]
    report = {
        "variables_migrated": [],
        "variables_already_migrated": 0,
        "classifications_converted": [],
        "classification_items_created": [],
        "classification_items_updated": [],
        "classification_items_removed": [],
        "authored_items_kept": [],
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
        meta = dict(node.get("metadata") or {})
        subtype = (
            "ClassificationVersion" if meta.get("version") else "ClassificationSeries"
        )
        add_subtype(node, subtype)
        node["subtypes"] = [
            s for s in node["subtypes"] if s != CLASSIFICATION_SUBTYPE_MARKER
        ]
        # Record the decision on the node. Dropping the marker subtype was the
        # only evidence for a node that declared itself that way, leaving the
        # conversion unreproducible: revert the type and it no longer declares
        # anything, and it is filed as an ordinary code list rather than
        # surfaced for review.
        meta["is_classification"] = True
        node["metadata"] = meta
        report["classifications_converted"].append(
            {
                "id": node["id"],
                "name": node.get("name"),
                "subtypes": node.get("subtypes"),
                "version": meta.get("version"),
            }
        )

    # ---- 3. Derived items converge on metadata.codes --------------------------
    if create_items:
        for parent in [n for n in graph["nodes"] if n["type"] == "Classification"]:
            _sync_items(graph, parent, report)

    nodes, edges = graph["nodes"], graph["edges"]

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
    edge_ids = [e["id"] for e in edges]
    if len(set(edge_ids)) != len(edge_ids):
        dupes = [i for i, c in Counter(edge_ids).items() if c > 1]
        raise SystemExit(f"ABORT: duplicate edge ids produced: {dupes[:5]}")
    if any(n["type"] == "InstanceVariable" for n in nodes):
        raise SystemExit("ABORT: InstanceVariable nodes remain after migration")

    report["counts_after"] = {
        "nodes": len(nodes),
        "edges": len(edges),
        "by_type": dict(Counter(n["type"] for n in nodes)),
    }
    return graph, report


def write_graph(path, graph):
    """Write through a temp file in the same directory, then replace.

    A plain open(path, "w") truncates the target before the first byte is
    written, so an interrupt or a full disk mid-dump leaves the graph truncated
    with no copy anywhere — and this runs against live data.
    """
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(graph, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


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

    with open(args.graph, encoding="utf-8") as handle:
        graph = json.load(handle)

    before_nodes = len(graph["nodes"])
    before_edges = len(graph["edges"])

    enrichment_log = []
    if args.enrich:
        with open(args.enrich, encoding="utf-8") as handle:
            enrichment_log = enrich(graph, json.load(handle))

    graph, report = migrate(graph, create_items=not args.no_items)
    report["enrichment"] = enrichment_log

    print(f"=== {args.graph} ===")
    print(
        f"nodes {before_nodes} -> {report['counts_after']['nodes']}   "
        f"edges {before_edges} -> {report['counts_after']['edges']}"
    )
    for entry in enrichment_log:
        print(f"enrichment: {entry['match'][0]!r} — {entry['status']}")
    print(
        f"InstanceVariable -> Variable      : {len(report['variables_migrated'])}"
        f"  (already migrated: {report['variables_already_migrated']})"
    )
    print(
        f"CodeList -> Classification        : {len(report['classifications_converted'])}"
    )
    for item in report["classifications_converted"]:
        print(f"    • {item['name']}  subtypes={item['subtypes']}")
    print(
        f"ClassificationItem created        : {len(report['classification_items_created'])}"
    )
    print(
        f"ClassificationItem updated        : {len(report['classification_items_updated'])}"
    )
    print(
        f"ClassificationItem removed        : {len(report['classification_items_removed'])}"
    )
    if report["authored_items_kept"]:
        print(
            f"Authored items left alone         : {len(report['authored_items_kept'])}"
        )
    print(f"CodeList kept as CodeList         : {len(report['codelists_kept'])}")
    print(
        f"Left for manual review            : {len(report['left_for_manual_review'])}"
    )
    for item in report["left_for_manual_review"]:
        print(f"    • {item['name']} — {item['reason']}")

    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
        print(f"\nReport: {args.report}")

    if args.write:
        write_graph(args.graph, graph)
        print(f"Written: {args.graph}")
    else:
        print("\n(dry run — pass --write to apply)")


if __name__ == "__main__":
    main()
