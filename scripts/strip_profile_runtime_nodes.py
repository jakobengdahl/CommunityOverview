#!/usr/bin/env python3
"""Strip runtime/system nodes from a profile's example seed data.

SavedView, VisualizationView, EventSubscription, Agent and Skill nodes are
instance state, not example content: they are created by people using a running
deployment. A snapshot taken from a live instance carries them along, and they
then ship as if they were part of the profile.

Usage: strip_runtime_nodes.py <graph.json> [--write]
"""

import json
import sys
from collections import Counter

RUNTIME_TYPES = {
    "SavedView",
    "VisualizationView",
    "EventSubscription",
    "Agent",
    "Skill",
}

path = sys.argv[1]
write = "--write" in sys.argv

with open(path, encoding="utf-8") as f:
    graph = json.load(f)

before_nodes, before_edges = len(graph["nodes"]), len(graph["edges"])
doomed = {n["id"] for n in graph["nodes"] if n["type"] in RUNTIME_TYPES}
removed = Counter(n["type"] for n in graph["nodes"] if n["id"] in doomed)

graph["nodes"] = [n for n in graph["nodes"] if n["id"] not in doomed]
graph["edges"] = [
    e for e in graph["edges"] if e["source"] not in doomed and e["target"] not in doomed
]

ids = {n["id"] for n in graph["nodes"]}
dangling = [
    e["id"] for e in graph["edges"] if e["source"] not in ids or e["target"] not in ids
]
if dangling:
    sys.exit(f"ABORT: {len(dangling)} dangling edges after strip")

print(f"=== {path} ===")
print(
    f"nodes {before_nodes} -> {len(graph['nodes'])}   edges {before_edges} -> {len(graph['edges'])}"
)
for t, c in sorted(removed.items()):
    print(f"  removed {t}: {c}")
print("  dangling edges: 0")

if write:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(graph, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("  written")
else:
    print("  (dry run)")
