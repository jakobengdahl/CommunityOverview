# US-04 — Mapping Variables and Codelists Across the Statistical System

| Attribute | Value |
| :--- | :--- |
| **Skill** | AI-assisted variables and codelists mapping |
| **Actor** | Statistical producer, Metadata expert|
| **Status** | Ready for Development |

## User story
As a Statistical producer in a National Statistical System, I want to query the metadata graph to identify variables that measure the same real-world concept across different producers, compare the codelists and definitions they use, detect divergences, and obtain AI-assisted proposals for concept mappings or convergence paths, so that I can drive harmonization efforts without having to manually cross-reference methodology documents from each institution.

## Scenario steps

1. The statistical producer types in the chat panel: *"Show me all variables related to employment status and the codelists they use."* The system returns three nodes: `EMPSTAT` from the Labour Force Survey (linked to the ILO codelist), `SIT_LAB` from the Health Survey (linked to a national legacy codelist), and `COT_REG` from the Social Security Register (no codelist assigned).

2. The graph displays the three variable nodes side by side, each connected to its parent operation and to any codelist node it references. The metadata expert reviews the result and verifies that all three measure the same real-world concept but with different codelists — and one has none at all.

3. The statistical producer clicks on `SIT_LAB` and asks: *"How does this definition differ from the ILO standard?"* The AI compares the two codelist nodes and explains that the national legacy codelist merges ILO categories 2 and 3 ("unemployed" and "inactive") into a single code, making the series non-comparable with `EMPSTAT`.

4. The statistical producer asks: *"Suggest a mapping between the national legacy codelist and the ILO codelist."* The AI proposes a concordance table as a new Mapping node, flagging the merged category as a partial equivalence requiring a methodological note.

5. The metadata expert opens the same graph view, reviews the proposed Mapping node, confirms the partial equivalence, and adds a note recommending that future editions of the Health Survey split the merged code. The Mapping node is marked as validated.

6. The metadata expert saves the concordance as a RELATES_TO edge between the two codelist nodes, preserving both in the graph. The expert then reviews which codelist each operation should adopt going forward and annotates the recommendation as a node property, no codelist is removed until a formal governance decision is made. Both codelists could be preserved also.

## Acceptance criteria
- A Statistical producer can query in natural language which variables across producers link to the same concept, without prior knowledge of the graph schema.
- The system surfaces divergences in codelist references for the same concept node automatically, without requiring manual comparison.
- The AI can propose concordance mappings between two codelists covering the same domain, presented as candidate Mapping nodes pending expert review.
- An impact query on a classification change returns the full downstream list of affected variables, operations, datasets and indicators.
- Approved mappings and harmonized definitions are persisted in the graph with attribution (actor, date, authority reference).
- Both codelists are retained in the graph; the concordance between them is saved as a relationship edge, with the metadata expert recording a recommendation on which codelist each operation should adopt going forward.
- A harmonization status summary (aligned / partial / divergent) is queryable at any time for any concept node or for the full system.

## Open questions for prototype validation
- Does the graph data model support a "Concept" node type as a shared abstraction above individual variables, or would this require a schema extension in `schema_config.json`?
- Can a RELATES_TO edge between two codelist nodes carry structured payload (code-level concordance table) and a free-text recommendation property, or should the concordance be modelled as an intermediate Mapping node with its own attributes?
- Is there a mechanism to assign a "harmonization status" property (aligned / partial / divergent) to concept nodes, queryable in aggregate across the graph?
- How is provenance recorded on approved nodes — as node properties (`approved_by`, `approved_date`, `authority`) or as metadata on the relationship edge?
