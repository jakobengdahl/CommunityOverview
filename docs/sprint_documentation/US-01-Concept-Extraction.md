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

4 - If there are existing nodes in the metadata graph that the AI-assistant has identified, these are presented in the visualisation together with guidance from the ai-assistant on possible ways to add the relevant identified nodes from the document so that they are compliant with the (GSIM-based) metadata model. 

5 - The statistical producer can navigate and dive deeper into the nodes matched by the ai-assistant and see what connections to pre-existing nodes that were identified.

6 - The statistical producer can highlight one or more matched nodes and ask the AI assistant to explain details about match rationale etc.

7 - The statistical producer can give instructions to the ai-assistant on which identified new nodes and connections that should be added or give more general instructions such as "add everything matched with a high confidence score".

## Acceptance criteria

- The upload component accepts at minimum PDF and DOCX formats.
- Extracted concepts are described by the ai-assistant with information about confidence regarding matches.
- Clicking any node allows the user to ask questions to the AI assistant specifically about these.

## Open questions for prototype validation

- Does the current upload component pass document content to the AI assistant context, or only the filename?
- Is the metadata graph accessible as a searchable store (vector or keyword) at runtime, or is it embedded in the AI assistant prompt as static context?
- Does the graph component support cluster grouping, or only flat node-edge layout?
- Can edge weight or style be varied dynamically based on a numeric score?
