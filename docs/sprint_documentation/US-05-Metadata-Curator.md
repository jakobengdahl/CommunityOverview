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

6.	The curator accepts, modifies, or dismisses each suggestion, and the graph reflects the updated node state.

## Acceptance criteria
- An agent can be activated in the experts menu.
- LLM curation guidance explicitly references the loaded skill's conventions rather than producing generic metadata advice.
-	Multiple skills can be active simultaneously without the LLM conflating their guidance.
- The curator can identify which skill is the source of each suggestion.
- Node state updates from accepted suggestions are reflected in the graph within the session.

## Open questions for prototype validation
- Can skills be used directly instead of through an agent?
- Is there a skill/expertise catalogue UI in the prototype, or would skill selection require a manual configuration step outside the interface?
- Does the prototype support multiple skills/expertise aresas active simultaneously in the same LLM context, and is there a context length constraint that limits how many can be loaded at once?
