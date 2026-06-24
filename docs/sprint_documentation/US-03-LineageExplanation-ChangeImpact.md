# US-03 — Lineage Explanation and Methodological Change Impact Assessment

| Attribute | Value |
| :--- | :--- |
| **Skill** | Provenance traversal + change impact propagation |
| **Actor** | Methodology Officer, Data Steward |
| **Status** | Ready for Development |

## User story
As a methodology officer, I want to select a published Data Set and explore its full derivation chain through an interactive graph, and when a classification version changes I want the system to identify all downstream metadata artefacts affected by that change, so that I can answer quality and auditability questions and assess methodological impact without manually navigating the metadata system.

## Scenario steps
1.	The methodology officer selects a published Data Set and the system renders its lineage as a directed graph, tracing the chain from Input Data Sets through Process Steps to the selected Output Data Set, following GSIM provenance relationships.

2.	The officer clicks any node in the lineage graph and the LLM generates a plain-language explanation of that entity: what it represents, what transformation or process produced it, and what quality dimensions apply.

3.	The officer clicks a Process Step node and the LLM describes the methodological choices made at that step, including any classification or code list applied.

4.	Separately, the data steward selects a Statistical Classification and triggers a version change (e.g., NACE Rev. 2 to NACE Rev. 2.1).

5.	The system traverses the graph in the reverse direction — from the changed Classification outward — and highlights all affected nodes: Codelists, Represented Variables, Data Structure Definitions, and derived Data Sets.

6.	The affected nodes are rendered in a distinct visual state in the graph, grouped by impact severity (breaking change vs. annotation-only change).

7.	The officer clicks any affected node and the LLM explains specifically what attribute or mapping needs to be reviewed or updated as a result of the version change.

8.	The officer can export a structured impact report listing all affected artefacts, their type.

## Acceptance criteria
- The lineage graph renders the full Input → Process Step → Output chain for any selected Data Set without manual configuration.
- Every node in the lineage graph is clickable and returns an LLM plain-language explanation inline.
- A classification version change triggers automatic traversal of all downstream dependencies.
- Affected nodes are visually distinct and grouped by impact severity in the graph.
- The impact report is exportable in at minimum a structured text or JSON format.
- Lineage traversal and impact propagation operate on the same graph structure without requiring separate views.

## Open questions for prototype validation
- Does the current graph model store directional edges sufficient to support both forward lineage traversal (Input → Output) and reverse impact traversal (Classification → dependent artefacts)?
- Is classification versioning modelled in the prototype's data layer, or would version change need to be simulated as a manual node attribute update?
- How is impact severity determined — is there a rule set in the prototype, or would the LLM infer it from the relationship type and artefact combination?
- Does the prototype support exporting subgraphs or node lists, or would the impact report require a new output component?
- Are Process Steps currently represented as first-class nodes in the graph, or are they implicit in the edges between Data Sets?
