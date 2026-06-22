# US-03 — Lineage Explanation & Methodological Change Impact: Implementation Plan

## Context

US-03 (`docs/sprint_documentation/US-03 LineageExplanation-ChangeImpact.md`) asks for two
capabilities on top of the existing WP12 Metadata Graph prototype:

1. **Lineage explanation** — a methodology officer selects a published Data Set, sees its full
   derivation chain (Input → Process Step → Output) as a graph, and clicks any node to get an
   LLM plain-language explanation inline.
2. **Change impact assessment** — a data steward triggers a classification version change
   (e.g. NACE Rev.2 → Rev.2.1); the system traverses the graph in reverse, highlights all affected
   artefacts grouped by impact severity (breaking vs annotation-only), explains per node what must
   be reviewed, and exports a structured impact report.

The platform already has the right bones: a directional `nx.MultiDiGraph` store, a GSIM-aligned
profile (`config/stockholmsprint/`), a React Flow canvas with node highlight/select states, a
Zustand store, and an LLM provider abstraction. The gaps are: no `ProcessStep` node type, no
classification versioning, only **bidirectional** traversal (no directional forward/reverse
helpers), no "explain a single node" endpoint, and no impact/severity visualization or report export.

This plan extends the **active sprint profile `stockholmsprint`** and the existing single-page
canvas rather than building a separate view, reusing existing patterns end to end.

### Confirmed design decisions
- **Process Step:** add `ProcessStep` as a first-class node type + seed an example lineage chain.
- **Version change:** simulate via a node attribute/metadata update + a "trigger version change" action (no first-class version nodes).
- **Severity:** hybrid — deterministic rules classify severity; LLM generates the per-node "what to review" explanation.
- **UI:** extend the existing `GraphCanvas` with a lineage/impact mode (reuse store, node states, detail dialog) — no new route.

---

## Workstream 1 — Data model & seed data (`config/stockholmsprint/`)

**Add `ProcessStep` node type** to `config/stockholmsprint/schema_config.json` (`schema.node_types`)
following the existing entry shape (`fields`, `category: "domain"`, `color`, `icon`), and add its
color to `presentation.colors`. Suggested icon `GearFill`.

**Add relationship types** to `schema.relationship_types` to express lineage flow in the direction
of data flow (so forward traversal = `out_edges`):
- `INPUT_TO` — Input DataSet → ProcessStep (source = input dataset).
- `PRODUCES_OUTPUT` — ProcessStep → Output DataSet (or reuse existing `PRODUCES`; prefer a distinct
  type so process lineage is filterable). Keep existing `PRODUCES`, `HAS_STRUCTURE`, `HAS_VARIABLE`,
  `MEASURES`, `USES_CODE_LIST` as-is.

**Seed example lineage** into `config/stockholmsprint/graph.json` (the source copied to
`data/active/graph.json` by `start-sprint.sh`). Extend the existing LFS chain so a published
Output DataSet has: one or more Input DataSets → a `ProcessStep` (with `metadata` describing the
methodological choices: classification applied, transformation) → the Output DataSet, which already
fans out `HAS_STRUCTURE → DataStructure → HAS_VARIABLE → InstanceVariable → USES_CODE_LIST → CodeList`.
Add a `CodeList` that plays the "Statistical Classification" role (e.g. a NACE-like list) with
`metadata.version` set, so the version-change scenario has a target. Reuse existing node/edge JSON
structure (`id`, `type`, `name`, `description`, `tags`, `subtypes`, `metadata`).

> No `config_loader.py` code change is needed — node/relationship types are fully config-driven and
> validated by the existing Pydantic `SchemaFileConfig`.

---

## Workstream 2 — Backend traversal (`backend/core/storage.py`, `backend/service/service.py`)

**Add a directional traversal helper to `GraphStorage`** (next to `get_related_nodes`,
storage.py:490). One method covers both lineage and impact:

```python
def traverse_directional(self, node_id, direction, relationship_types=None, depth=3):
    # direction: "forward" -> self.graph.out_edges(...); "reverse" -> self.graph.in_edges(...)
    # layer-by-layer BFS mirroring get_related_nodes, but single-direction + edge-type filter
    # returns {'nodes': [...], 'edges': [...]}
```
Reuse the exact `out_edges(..., keys=True, data=True)` / `in_edges(...)` access pattern already in
`get_related_nodes`. This single helper serves:
- **Forward lineage** of a DataSet to its structure/variables/codelists (`forward`).
- **Reverse lineage** of an Output DataSet to its ProcessStep + Input DataSets (`reverse` over
  `PRODUCES_OUTPUT`/`INPUT_TO`/`PRODUCES`).
- **Reverse impact** from a CodeList outward to dependent variables → structures → datasets
  (`reverse` over `USES_CODE_LIST`, `HAS_VARIABLE`, `HAS_STRUCTURE`).

**Add `GraphStorage.get_lineage_subgraph(dataset_id, depth)`** that composes the helper: reverse
walk over lineage relationships to gather Input/ProcessStep/Output, plus forward walk over
structure relationships to gather the artefact detail — merged into one node/edge set (lineage and
impact operate on the **same** graph, per the AC).

**Add `GraphService` wrappers** in `service.py` following the established pattern
(`_evaluate_graph_access(GRAPH_ACTION_READ, ...)`, `_filter_nodes_and_edges`, `serialize_nodes/edges`,
`{"success": True, "nodes", "edges", "total_*"}`):
- `get_lineage(node_id, depth)` → lineage subgraph for a selected Data Set.
- `assess_change_impact(node_id, change_type, depth)` → see Workstream 3.

---

## Workstream 3 — Impact engine + severity rules (new `backend/service/impact.py`)

A small, testable, pure module (no I/O) consumed by `GraphService.assess_change_impact`:

- Input: the changed CodeList node, a `change_type` (`"breaking"` | `"annotation"`), and the reverse
  impact subgraph from `traverse_directional(direction="reverse", ...)`.
- **Severity rules (deterministic):**
  - `change_type == "annotation"` → all affected nodes are `annotation-only`.
  - `change_type == "breaking"`:
    - direct consumers via `USES_CODE_LIST` (InstanceVariables) → `breaking`.
    - transitive artefacts (DataStructure, derived DataSet) reachable from those → `breaking` if a
      coded identifier/measure is affected, else `annotation-only`. Keep the rule table simple and
      data-driven (relationship type + node type → severity) so it is unit-testable.
- Output: `affected` list of `{id, name, type, severity, relationship_path, recommended_review}` plus
  `groups` keyed by severity. `recommended_review` is filled by the LLM (Workstream 4); the rules
  fill `severity` + the relationship path.

**Version-change simulation:** `assess_change_impact` updates the CodeList node's
`metadata.version` / `metadata.last_change_type` via the existing `update_node`/event-context path
so the change is recorded, then computes impact. No new node types.

---

## Workstream 4 — LLM explanation endpoint (`backend/ui/`)

Add an "explain a single entity" capability reusing `backend/llm_providers.py`
(`create_provider`) and the schema-driven prompt style in `backend/chat_logic.py`. Add an
`explain_node` method (in `chat_service.py` or a focused `explanation_service.py`):

- Build a compact prompt from the node's details (`get_node_details`) + its immediate relationships
  + profile `prompt_prefix` context, asking for a plain-language explanation: what it represents,
  what process produced it, which quality dimensions / classifications apply.
- For a **ProcessStep** node, prompt for the methodological choices and any classification/code list
  applied (scenario step 3).
- For an **affected node** (impact mode), include the change context (old→new version, change_type,
  relationship path) and ask specifically what attribute/mapping must be reviewed (scenario step 7);
  feed the result back into `recommended_review`.
- Respect the existing provider/key selection (`LLM_PROVIDER`, `*_API_KEY`) and degrade gracefully
  when no LLM is configured (return a templated fallback, mirroring `llmAvailable` handling).

---

## Workstream 5 — API + MCP wiring (`backend/service/rest_api.py`, `mcp_tools.py`, `backend/ui/rest_api.py`)

**REST** — add a `_register_lineage_endpoints(router, service)` group (registered in
`create_rest_router`), with Pydantic request models like existing ones:
- `POST /api/lineage` → `{ node_id, depth }` → lineage subgraph.
- `POST /api/impact` → `{ node_id, change_type, depth }` → impact result (nodes/edges + `groups`).
- `POST /api/impact/report` → impact result serialized as a structured report (JSON; also
  text-renderable) — satisfies the export AC.
- `POST /ui/explain` (in `backend/ui/rest_api.py`, alongside `/ui/chat`) → `{ node_id, context? }`
  → `{ explanation }`.

**MCP** — register `get_lineage`, `assess_change_impact`, and `explain_node` via the existing
`@register_tool` pattern in `register_mcp_tools` so the same operations are available to chat/agents.

---

## Workstream 6 — Frontend lineage/impact mode (extend existing canvas)

**API client** (`frontend/web/src/services/api.js`) — add `getLineage(nodeId, depth)`,
`assessImpact(nodeId, changeType, depth)`, `getImpactReport(...)`, `explainNode(nodeId, context)`
following the existing `apiFetch` helpers.

**Store** (`frontend/web/src/store/graphStore.js`) — add lineage/impact state + actions reusing the
existing `updateVisualization` / `highlightedNodeIds` machinery:
- `lineageMode` flag, `affectedSeverity` map (`nodeId → "breaking"|"annotation-only"`),
  `impactResult`, and an `explanationCache` keyed by nodeId.
- actions: `loadLineage(nodeId)`, `runImpact(nodeId, changeType)`, `clearImpact()`.

**Node styling** (`packages/ui-graph-canvas/src/components/CustomNode.{jsx,css}`) — add severity CSS
classes (`.severity-breaking`, `.severity-annotation`) and an `affected` state, driven by a new
`data.severity` prop; `GraphCanvas.jsx` passes `affectedSeverity[nodeId]` into node `data` where it
already computes `data.color`/`isHighlighted` (GraphCanvas.jsx:170-197). This gives the "visually
distinct, grouped by severity" rendering.

**Node click → inline explanation** — extend `NodeDetailDialog.jsx` (opened by the existing
double-click → `setDetailNode` flow, App.jsx:134-187) with an "Explanation" section that calls
`explainNode` (with impact context when in impact mode), shows a loading state, caches the result,
and renders the plain-language text inline. This satisfies "every node clickable → LLM explanation
inline" without a new component.

**Triggers** — add lightweight entry points reusing the existing toolbar/context-menu patterns:
- on a DataSet node: "Explore lineage" → `loadLineage`.
- on a CodeList/classification node: "Trigger version change" → small dialog picking
  `breaking | annotation` → `runImpact`.

**Impact report panel + export** — a panel listing affected artefacts grouped by severity with an
"Export" button that downloads the report as JSON/text, reusing the existing export button pattern
in `FloatingHeader`.

---

## Verification

1. **Run the sprint profile:** `./start-sprint.sh` (loads `stockholmsprint`, copies seed graph to
   `data/active/graph.json`); open `http://localhost:8000/web/`. Ensure `ANTHROPIC_API_KEY` (or
   `OPENAI_API_KEY`) is set so explanations are live; otherwise confirm the templated fallback.
2. **Lineage:** select the published LFS Output DataSet → confirm the Input → ProcessStep → Output
   chain plus its structure/variables/codelists render in one graph with no manual config.
3. **Node explanation:** click several node types (incl. the ProcessStep) → confirm inline
   plain-language explanations; the ProcessStep explanation mentions methodological choices /
   classification applied.
4. **Impact:** trigger a `breaking` version change on the classification CodeList → confirm reverse
   traversal highlights dependent InstanceVariables, DataStructures and derived DataSets, visually
   grouped breaking vs annotation-only; repeat with `annotation` and confirm severity differs.
5. **Affected-node explanation + report:** click an affected node → confirm the "what to review"
   text; export the impact report and confirm valid JSON listing artefacts + types + severity.
6. **Automated tests:**
   - `npm run test:python` — new pytest for `storage.traverse_directional` (forward/reverse/depth),
     `impact.py` severity rules, and the impact-report serializer.
   - `npm run test:unit` — vitest for the new store actions and severity CSS class mapping.
   - `npm run test:e2e` — optional Playwright covering select-dataset → lineage → trigger change →
     export.

## Critical files
- `config/stockholmsprint/schema_config.json`, `config/stockholmsprint/graph.json` — ProcessStep type, relationships, seed lineage.
- `backend/core/storage.py` — `traverse_directional`, `get_lineage_subgraph`.
- `backend/service/service.py` — `get_lineage`, `assess_change_impact` wrappers.
- `backend/service/impact.py` (new) — severity rules + report structure.
- `backend/ui/chat_service.py` (or new `explanation_service.py`) — `explain_node` via `llm_providers.create_provider`.
- `backend/service/rest_api.py`, `backend/service/mcp_tools.py`, `backend/ui/rest_api.py` — endpoints + MCP tools.
- `frontend/web/src/services/api.js`, `frontend/web/src/store/graphStore.js`, `frontend/web/src/components/NodeDetailDialog.jsx`, `packages/ui-graph-canvas/src/components/CustomNode.{jsx,css}`, `packages/ui-graph-canvas/src/components/GraphCanvas.jsx` — lineage/impact mode UI.
