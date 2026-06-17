# Metadata Prototype

## Description
This project is a prototype exploring the use of an AI-powered knowledge graph as a foundation for metadata management in statistical offices. 

The idea is to enable users interact with statistical metadata (variables, concepts, data structures, code lists, etc.) through natural language, while the underlying graph captures and enforces the relationships defined by standards like GSIM and SDMX. 

Compared to traditional metadata registries (e.g. rigid database-driven catalogues), this approach supports flexible exploration, AI-assisted entity extraction from documents, and collaborative knowledge building across organizations.


## Input and output
The input is natural language queries and/or uploaded documents describing statistical metadata.

For our proof of concept, the domain is the European Statistical System (ESS) — mapping statistical offices (NSIs), statistical programmes (e.g. Labour Force Survey, Party Preference Survey), datasets, data structures, variables, concepts, unit types, and code lists.

The output is a navigable, visual knowledge graph and conversational answers grounded in the graph's content.

## Evaluation criteria
Discoverability: can users find related metadata (e.g. "which variables measure labour market status?") faster than in existing registries?
Interoperability: does the graph model align with GSIM/SDMX well enough to support real metadata exchange workflows?
Collaboration: can multiple statistical offices contribute to and benefit from a shared metadata landscape?
Flexibility: how easily can the schema be adapted to other statistical domains beyond the ESS seed data?

## Architecture
The software is a profile-based knowledge graph platform with a React frontend (React Flow visualization) and a FastAPI + NetworkX backend. The Stockholm Sprint profile defines 8 domain node types (Actor, StatisticalProgramme, DataSet, DataStructure, InstanceVariable, Concept, UnitType, CodeList) and 9 relationship types modelled after GSIM. 

The AI layer (Claude or any OpenAI-compatible LLM with tool calling) provides natural language interaction, entity extraction from uploaded documents, and duplicate detection. 

Configuration is fully driven by a profile schema — no code changes are needed to adapt the domain model.

## Evaluation summary

| Criterion | Assessment |
|---|---|
| Efficiency gain | high in discoverability and cross-office collaboration, low/none in replacing existing registries for authoritative publication |
| Reusability | high — profile system supports any metadata domain without code changes |
| Data accessibility |  |
| On-prem compatibility | medium/high — requires an LLM endpoint that supports tool calling (can be self-hosted) |
| Low-hanging fruit for NSIs |  |
| Evaluation robustness |  |
| Feasibility | medium — seed data demonstrates the concept, but real-world coverage requires curation effort |
| Lifespan | medium/high — standards-aligned schema (GSIM/SDMX) and LLM-agnostic design reduce lock-in |
| Cost effectiveness |  |

## TODO
Graph seeded with ESS actors and their relationships
Statistical programmes linked to datasets and data structures
Variables, concepts, unit types, and code lists modelled and connected
Natural language queries resolve against the metadata graph
AI extracts entities and relationships from uploaded documents
Duplicate detection flags overlapping definitions across offices
Expert agents (Metadata Expert, ESS Expert) provide domain-specific guidance
Federation support for connecting metadata graphs across organizations

