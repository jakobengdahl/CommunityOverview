---
name: GSIM Lineage and Change Impact
description: Trace data lineage through the GSIM metadata chain and assess the downstream impact of a statistical classification or code list version change.
when-to-use: Activate when a methodology officer or data steward asks to trace data lineage/provenance, explain what a GSIM metadata artefact represents, or determine which metadata artefacts are affected by a classification or code list version change (e.g. NACE Rev.2 -> Rev.2.1, new sex codes).
allowed-tools: get_lineage assess_change_impact get_impact_report get_node_details get_related_nodes search_graph list_node_types
effort: high
version: "1.0"
---

> **Note:** This skill requires profile-specific backend tools (`get_lineage`, `assess_change_impact`, `get_impact_report`) that are not part of the standard tool set. It will only function correctly when those tools are registered for the active profile.

## Context

You work on the ESS GSIM metadata knowledge graph. The relevant node types for lineage and impact analysis are:

- **StatisticalProgramme** — the recurring activity that produces a DataSet
- **DataSet** — published aggregate output
- **DataStructure** — the dimensional schema of a DataSet
- **InstanceVariable** — a concrete variable with a role (identifier or measure)
- **Concept** — the abstract statistical concept an InstanceVariable measures
- **CodeList** — enumerated value set used by coded variables
- **ProductionSolution** — a technical pipeline that processes programme data into a DataSet

Key relationships (data-flow direction):
- `StatisticalProgramme PRODUCES DataSet`
- `ProductionSolution USES StatisticalProgramme`, `ProductionSolution PRODUCES DataSet`
- `DataSet HAS_STRUCTURE DataStructure`
- `DataStructure HAS_VARIABLE InstanceVariable`
- `InstanceVariable MEASURES Concept`
- `InstanceVariable USES_CODE_LIST CodeList`

## Capability A — Lineage exploration and explanation

1. Resolve the target node. If given a name rather than an id, use `search_graph` to find it.
2. Call `get_lineage(node_id, depth=4)` to retrieve the full derivation chain. Do not build a separate view — lineage and impact share one graph.
3. Present the chain in reading order (inputs first, then each step, then the output and its structure).
4. For any node the user asks about, give a plain-language explanation: what it represents, what produced it, and which classifications or code lists apply. Use `get_node_details` / `get_related_nodes` to ground the explanation in actual node metadata.
5. For a **ProductionSolution** node, describe the technical pipeline including its git repository (`repo` field) if present.

## Capability B — Classification / code list change impact

1. Identify the changed CodeList or Concept node (resolve by name with `search_graph` if needed).
2. Determine the change type:
   - **breaking**: codes added, removed, or remapped — downstream aggregations may be affected
   - **annotation**: labels or descriptions only — no structural impact
   - Ask the user if it is unclear.
3. Call `assess_change_impact(node_id, change_type, depth=4, new_version=<label>)`. This walks the graph in reverse from the classification and returns affected artefacts grouped by severity.
4. Report affected artefacts grouped by severity. For each, explain specifically what attribute or mapping must be reviewed as a result of the version change — the relationship path shows why it is affected.
5. When the user wants an export, call `get_impact_report(node_id, change_type, depth, new_version)` and return the structured report listing every affected artefact and its type.

## Operating notes

- Severity is deterministic (rule-based from the graph); do not silently re-grade what the tool returns
- Prefer the dedicated tools over manual multi-hop traversal — one `get_lineage` / `assess_change_impact` call returns the whole subgraph
- If the graph lacks a ProductionSolution or DataStructure between programme and output, say so plainly rather than inventing a chain
- Keep explanations plain-language and audit-oriented; cite node names and ids so findings are traceable
- Do NOT use markdown tables — they do not render in this interface
