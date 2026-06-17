# User Story: US-01 — Concept Extraction and Registry Matching

| Attribute | Value |
| :--- | :--- |
| **Skill** | Document parsing + semantic search over the metadata registry |
| **Actor** | Statistical Analyst |
| **Status** | Ready for Development |

## User story

As a statistical analyst, I want to upload a methodological document and explore the extracted concepts and their registry matches through an interactive graph, so that I can visually identify reuse opportunities and definition gaps before formalising new metadata.

## Scenario steps

1- The analyst uploads a PDF or Word document (e.g., a survey questionnaire or technical report) through the document upload component.

2 - The LLM parses the document and extracts candidate concepts, preserving their section of origin (e.g., "Geographic scope", "Unit of observation").

3 - Each candidate concept is matched against the metadata registry — Represented Variables, Concepts, Value Domains — using semantic search, returning a confidence score per match.

4 - The result is rendered as a graph where document sections appear as cluster groups, matched registry entities appear as linked nodes with edge weight reflecting match confidence, and unmatched candidates appear as isolated nodes.

5 - The analyst clicks an unmatched node and the LLM generates a draft definition following GSIM structure.

6 - The analyst clicks a matched node and the LLM explains the match rationale and highlights attribute differences between the document description and the registry entry.

7 - The analyst can adjust a confidence threshold slider to filter out weak matches and focus on confirmed reuse or confirmed gaps.

## Acceptance criteria

- The upload component accepts at minimum PDF and DOCX formats.
- Extracted concepts are grouped in the graph by document section.
- Each edge between a document concept and a registry entity carries a visible confidence indicator.
- Isolated nodes (unmatched) are visually distinct from matched nodes.
- Clicking any node triggers an LLM explanation inline, without leaving the graph view.
- The confidence filter updates the graph in real time.

## Open questions for prototype validation

- Does the current upload component pass document content to the LLM context, or only the filename?
- Is the registry accessible as a searchable store (vector or keyword) at runtime, or is it embedded in the LLM prompt as static context?
- Does the graph component support cluster grouping, or only flat node-edge layout?
- Can edge weight or style be varied dynamically based on a numeric score?
