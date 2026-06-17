# User Story: US-01 — Concept Extraction and Graph Matching

| Attribute | Value |
| :--- | :--- |
| **Skill** | Document parsing + semantic search over the metadata graph |
| **Actor** | Statistical Analyst |
| **Status** | Work in Progress |

## User story

As a statistical producer, I want to upload a document related to a statistical product and explore the extracted concepts and their graph matches through an interactive graph, so that I can visually identify reuse opportunities and definition gaps before formalising and adding new metadata to the graph.

## Scenario steps

1- The statistical producer uploads a PDF or Word document (e.g., a survey questionnaire or technical report) through the document upload component in the ai-assistent.

2 - The AI assistant parses the document and extracts candidate concepts, preserving their section of origin (e.g., "Variables", "Unit of observation").

3 - Each candidate concept is matched against the metadata graph structure and content — Represented Variables, Concepts, Value Domains — using semantic search, returning a confidence score per match.

4 - If there are existing nodes in the metadata graph that the AI-assistant has identified, these are presented in the visualisation together with guidance from the ai-assistant on possible ways to add the relevant identified nodes from the document and how to connect these to the existing metadata nodes in the graph. 

5 - The statistical producer can navigate and dive  an unmatched node and the AI assistant generates a draft definition following GSIM structure.

6 - The analyst clicks a matched node and the AI assistant explains the match rationale and highlights attribute differences between the document description and the graph entry.

7 - The analyst can adjust a confidence threshold slider to filter out weak matches and focus on confirmed reuse or confirmed gaps.

## Acceptance criteria

- The upload component accepts at minimum PDF and DOCX formats.
- Extracted concepts are grouped in the graph by document section.
- Each edge between a document concept and a graph entity carries a visible confidence indicator.
- Isolated nodes (unmatched) are visually distinct from matched nodes.
- Clicking any node triggers an AI assistant explanation inline, without leaving the graph view.
- The confidence filter updates the graph in real time.

## Open questions for prototype validation

- Does the current upload component pass document content to the AI assistant context, or only the filename?
- Is the metadata graph accessible as a searchable store (vector or keyword) at runtime, or is it embedded in the AI assistant prompt as static context?
- Does the graph component support cluster grouping, or only flat node-edge layout?
- Can edge weight or style be varied dynamically based on a numeric score?
