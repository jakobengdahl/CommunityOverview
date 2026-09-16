#!/usr/bin/env python3
"""Strip runtime/system state from a profile's example seed data.

SavedView, VisualizationView, EventSubscription, Agent and Skill nodes are
instance state, not example content: they are created by people using a running
deployment. A snapshot taken from a live instance carries them along, and they
then ship as if they were part of the profile.

The file's ``metadata.journal_id`` is instance state too: it names the lineage
the graph journal beside the file belongs to. A seed that keeps it hands the
same lineage to every instance seeded from it, so a journal from one would
replay onto another instead of being refused. Exports through the app never
carry it; a graph.json copied by hand does.

Usage: strip_profile_runtime_nodes.py <graph.json> [--write]
"""

import json
import sys
from collections import Counter
from typing import Any, Dict

RUNTIME_TYPES = {
    "SavedView",
    "VisualizationView",
    "EventSubscription",
    "Agent",
    "Skill",
}

RUNTIME_METADATA_KEYS = {"journal_id"}


def strip(graph: Dict[str, Any]) -> Counter:
    """Remove runtime state from ``graph`` in place; return what was removed.

    Raises ValueError if the result would leave a dangling edge.
    """
    doomed = {n["id"] for n in graph["nodes"] if n["type"] in RUNTIME_TYPES}
    removed = Counter(n["type"] for n in graph["nodes"] if n["id"] in doomed)

    graph["nodes"] = [n for n in graph["nodes"] if n["id"] not in doomed]
    graph["edges"] = [
        e
        for e in graph["edges"]
        if e["source"] not in doomed and e["target"] not in doomed
    ]

    metadata = graph.get("metadata")
    if isinstance(metadata, dict):
        for key in RUNTIME_METADATA_KEYS:
            if key in metadata:
                del metadata[key]
                removed[f"metadata.{key}"] += 1

    ids = {n["id"] for n in graph["nodes"]}
    dangling = [
        e["id"]
        for e in graph["edges"]
        if e["source"] not in ids or e["target"] not in ids
    ]
    if dangling:
        raise ValueError(f"{len(dangling)} dangling edges after strip")
    return removed


def main(argv: list) -> int:
    path = argv[1]
    write = "--write" in argv

    with open(path, encoding="utf-8") as f:
        graph = json.load(f)

    before_nodes, before_edges = len(graph["nodes"]), len(graph["edges"])
    try:
        removed = strip(graph)
    except ValueError as exc:
        sys.exit(f"ABORT: {exc}")

    print(f"=== {path} ===")
    print(
        f"nodes {before_nodes} -> {len(graph['nodes'])}   "
        f"edges {before_edges} -> {len(graph['edges'])}"
    )
    for what, count in sorted(removed.items()):
        print(f"  removed {what}: {count}")
    print("  dangling edges: 0")

    if write:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(graph, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print("  written")
    else:
        print("  (dry run)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
