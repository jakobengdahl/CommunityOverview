# US-06 — Interactive Graph Exploration of Statistical Metadata

| Attribute | Value |
| :--- | :--- |
| **Skill** | Graph visualisation + LLM-powered node explanation |
| **Actor** | Dissemination officer, Data steward |
| **Status** | Ready for Development |

## User story
As a dissemination officer, I want to explore the relationships between a Data Product and its linked entities — Variables, Classifications, Populations, Process Steps — through an interactive graph, where I can click any node to request an LLM-generated plain-language explanation of that entity and its role in the statistical process, so that I can both understand the production chain visually and extract narrative content for publication without manual authoring.

## Scenario steps

1. The data steward shares with the dissemination officer a saved graph view scoped to the Labour Force Survey Data Product, containing its linked Variables, Classifications, Population, and key Process Steps.

2. The dissemination officer opens the graph and gets an overview of the production chain: nodes are colour-coded by entity type and connected by labelled edges that show how each entity relates to the Data Product.

3. The dissemination officer clicks on the variable node and asks: *"Explain this variable for a non-specialist audience."* The AI returns a plain-language paragraph describing what employment status measures, how it is collected, and why it matters for the published indicator.

4. The dissemination officer clicks on the Population node and asks: *"Who does this survey cover and who is excluded?"* The AI reads the node attributes (age range, geographic scope, institutional coverage) and generates a concise eligibility summary ready to paste into a publication note.

5. The dissemination officer navigates to a Codelist node and asks: *"How is this classification structured and has it changed recently?"* The AI explains the hierarchy of the codelist and, if a version change is recorded in the graph, surfaces it as a methodological caveat for the publication.

6. The dissemination officer selects a set of nodes — the Data Product, two Variables, and the Population — and asks: *"Generate a methodology summary for these elements."* The AI composes a coherent multi-paragraph text that the officer reviews, edits inline, and saves as a narrative attachment linked to the Data Product node.

7. The data steward reviews the saved narrative, confirms it is consistent with the validated graph content, and marks it as approved for dissemination.

## Acceptance criteria

- The dissemination officer can open a scoped graph view shared by the data steward without needing to configure the graph from scratch.
- Nodes are visually differentiated by entity type through colour coding and labelled edges.
- Clicking any node and submitting a natural language question returns an LLM-generated plain-language explanation grounded in that node's attributes and relationships.
- A multi-node selection triggers a combined narrative generation covering all selected entities in a coherent text.
- Generated narratives can be edited inline and saved as a text attachment linked to a specific node in the graph.
- The data steward can review and approve saved narratives without leaving the graph interface.
- No technical metadata knowledge is required from the dissemination officer to navigate the graph or generate explanations.

## Open questions for prototype validation

- Can the graph support scoped views shared between users that open pre-filtered to a specific Data Product and its linked entities?
- Can generated text be saved as a typed attachment node (e.g. `NarrativeNote`) linked to a Data Product node, or would this require a new node type in `schema_config.json`?
- How does the LLM ground its explanations — does it read node attributes directly from the graph API, or does it rely on context passed through the chat session?
