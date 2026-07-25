---
name: Impact Analysis
description: Trace which nodes in the graph are downstream of a given node, and assess what is affected when that node changes.
when-to-use: Use when the user wants to know what depends on a node, what would be affected if a node changed, or wants to understand the reach of a change through the graph.
allowed-tools: search_graph get_node_details get_related_nodes find_similar_nodes
effort: medium
version: "1.0"
---

## Goal

Identify which nodes in the graph depend on (directly or indirectly) a given starting node, and explain what the effect of a change to that node would be.

## Steps

1. **Identify the starting node** — Resolve by name if needed using `search_graph`. Ask the user to clarify if there are multiple matches.

2. **Map forward dependencies** — Use `get_related_nodes` to walk outward from the starting node (depth 2–3). This gives the set of nodes that the starting node feeds into or influences.

3. **Map reverse dependencies** — Use `get_related_nodes` with reverse direction to find nodes that the starting node depends on (its upstream sources).

4. **Classify impact** — For each downstream node:
   - **Direct**: connected by a single edge
   - **Indirect**: reachable in 2+ hops
   - **Severity hint**: if the change affects structure or identity (rename, merge, delete), the impact is likely breaking for dependents; if it's descriptive only (labels, descriptions), the impact is annotation-only

5. **Explain the impact** — For each affected node, describe specifically what attribute or relationship ties it to the changed node, and what would need to be reviewed or updated.

6. **Report** — Present findings as:
   - The changed node
   - Directly affected nodes (with relationship paths)
   - Indirectly affected nodes (with hop count)
   - Suggested actions for the most critical dependencies

## Operating notes

- Focus on nodes the user can act on — skip system nodes (SavedView, EventSubscription) unless asked
- Cite node names and types: `LFS DataSet (DataSet)`, not just "a node"
- If the graph lacks the expected connections to trace impact, say so rather than inventing a chain
- Keep the report audit-oriented: traceable, specific, actionable
