# US-05 — Guided Metadata Curation using Domain Knowledge Skills
| Attribute | Value |
| :--- | :--- |
| **Skill** | group	Domain knowledge base + GSIM conformance + metadata completeness |
| **Actor** | Metadata Curator, Domain expert |
| **Status** | Draft |

## User story
As a metadata curator, I want to load a domain knowledge skill — such as a Population Statistics skill containing established concepts, standard classifications, and methodological conventions for that domain — and use it as a reference layer while reviewing a Variable or Concept System in the metadata graph, so that the LLM guidance I receive during curation is grounded in domain-specific expertise rather than generic metadata rules.

## Scenario steps
1.	The curator selects one or more metadata artefacts in the graph — a Variable, a Concept System, or a subgraph grouping — as the scope for the curation session.

2.	The domain expert creates a skill specifying when to use and the content. Content could be rules and language terms specific to the domain.

3.	The skill content is selected by creating an agent that makes use of the skill.

4.	The skill is ativated in the graph assistant.

5.	The curator clicks a node and the LLM evaluates it against the loaded skill — flagging where the artefact's definition, classification, or attribute values diverge from the domain conventions encoded in the skill.

6.	For each flagged issue the LLM explains the divergence in plain language, citing the relevant convention from the skill as the basis for the suggestion.

7.	Where the loaded skill contains example artefacts or templates, the LLM can propose a reformulation of the node under review modelled on those examples.

8.	The curator accepts, modifies, or dismisses each suggestion, and the graph reflects the updated node state.

## Acceptance criteria
- A skill can be loaded as an md file and its content is demonstrably active in the LLM responses for that session.
- LLM curation guidance explicitly references the loaded skill's conventions rather than producing generic metadata advice.
-	Multiple skills can be active simultaneously without the LLM conflating their guidance.
- The curator can identify which skill is the source of each suggestion.
- Node state updates from accepted suggestions are reflected in the graph within the session.

## Open questions for prototype validation
- How are skills stored and passed to the LLM — as files injected into the system prompt, as retrieval chunks, or as static context blocks?
- Is there a skill catalogue UI in the prototype, or would skill selection require a manual configuration step outside the interface?
- Does the prototype support multiple skills active simultaneously in the same LLM context, and is there a context length constraint that limits how many can be loaded at once?
