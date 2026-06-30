# Sprint Documentation — Metadata Prototype

> **Note:** These documents are historical artifacts from the Stockholm Sprint
> prototype phase. They describe the original design goals and user stories for
> the ESS statistical metadata proof of concept. The current implementation has
> evolved significantly — see the main [README](../../README.md) and
> [docs/PROFILES.md](../PROFILES.md) for the current feature set.

---

## Description

This project explored using an AI-powered knowledge graph as a foundation for
metadata management in statistical offices. Users interact with statistical
metadata (variables, concepts, data structures, code lists, etc.) through
natural language, while the underlying graph captures and enforces relationships
defined by standards like GSIM and SDMX.

Compared to traditional metadata registries (rigid database-driven catalogues),
this approach supports flexible exploration, AI-assisted entity extraction from
documents, and collaborative knowledge building across organizations.

## Input and Output

**Input:** Natural language queries and/or uploaded documents describing statistical metadata.

**Domain:** The European Statistical System (ESS) — mapping statistical offices (NSIs),
statistical programmes (e.g. Labour Force Survey, Party Preference Survey), datasets,
data structures, variables, concepts, unit types, and code lists.

**Output:** A navigable, visual knowledge graph and conversational answers grounded
in the graph's content.

## Architecture

A profile-based knowledge graph platform with a React frontend (React Flow
visualization) and a FastAPI + NetworkX backend. The stat-metadata profile
defines domain node types (Actor, StatisticalProgramme, DataSet, DataStructure,
InstanceVariable, Concept, UnitType, CodeList) and relationship types modelled
after GSIM.

The AI layer (Claude or any OpenAI-compatible LLM with tool calling) provides
natural language interaction, entity extraction from uploaded documents, and
duplicate detection.

Configuration is fully driven by a profile schema — no code changes are needed
to adapt the domain model.

## User Stories (Sprint)

- [US-01](./US-01-Concept-Extraction.md) — Concept extraction from documents
- [US-02](./US-02-Standard-Facilitator.md) — Standard facilitator agent
- [US-03](./US-03-LineageExplanation-ChangeImpact.md) — Lineage explanation and change impact
- [US-04](./US-04-Concept-Harmonizer.md) — Concept harmonizer
- [US-05](./US-05-Metadata-Curator.md) — Metadata curator
- [US-06](./US-06-Dissemination.md) — Dissemination
