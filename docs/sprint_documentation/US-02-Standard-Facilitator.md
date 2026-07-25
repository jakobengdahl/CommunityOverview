# US-02 — Bridging Producers and Metadata Experts through a Shared Graph Workspace

| Attribute | Value |
| :--- | :--- |
| **Skill** | Collaborative graph editing + SDMX export via custom service |
| **Actor** | Statistical producer, Metadata expert |
| **Status** | Ready for Development |

## User story
As a statistical producer, I want to add new concepts and relationships to a shared metadata graph in plain language, have them reviewed and reconciled by a metadata expert within the same workspace, and then export the agreed result as a valid SDMX artefact, so that I can formalise new metadata without needing to know SDMX vocabulary or structural constraints upfront.

## Scenario steps
1.	The producer opens the metadata graph and adds a new node or relationship using free-text labels, without any requirement to conform to SDMX terminology at this stage.

2.	The LLM suggests a plain-language description for the new element and flags any apparent conflicts with existing nodes already present in the graph.

3.	The new element appears in the graph in a distinct pending state, visually differentiated from validated nodes.

4.	The metadata expert reviews the pending element in the same graph view, can annotate it with SDMX-aligned terminology, and either approves it, proposes a reformulation, or maps it to an existing registry concept.

5.	The producer and expert iterate within the graph: adding, renaming, or merging nodes; with the LLM available to both as a translation layer between plain language and SDMX constructs.

6.	Once both parties reach a consensus, the agreed subgraph is submitted to a custom prototype service that maps the validated elements to the appropriate SDMX artefact types (Concept, ConceptScheme, DataStructureDefinition, Codelist, etc.).

7.	The service returns a preview of the SDMX-formatted output for final review before registration.

## Acceptance criteria
- A producer can add nodes and edges using free text with no prior SDMX knowledge required.
- Pending elements are visually distinct from validated ones throughout the workflow.
- The Metadata can suggest SDMX-aligned reformulations for any plain-language element on request.
- The graph supports at minimum two concurrent roles (producer, metadata expert) acting on the same state.
- The custom service maps validated graph elements to correct SDMX artefact types without manual encoding.
- The SDMX output preview is readable within the graph interface before submission.

## Open questions for prototype validation
- Does the graph component support node states (e.g., pending, validated, rejected), or would this require an extension to the current data model?
- Is there a mechanism for two actors to annotate the same node independently, or does the current prototype support only a single user session?
- How is the custom SDMX service invoked,  as a direct API call from the React front end, or mediated through a backend the prototype already exposes?
