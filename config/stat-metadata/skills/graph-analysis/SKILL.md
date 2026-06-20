---
name: Graph Analysis
description: Analyse patterns, connections and clusters in the knowledge graph to surface insights about relationships between entities.
when-to-use: Use when the user asks for an analysis of the graph structure, wants to find patterns, identify clusters, or wants insights about how entities are connected to each other.
allowed-tools: search_graph get_related_nodes find_similar_nodes get_graph_stats get_node_details add_nodes
effort: medium
version: "1.0"
---

## Goal

Systematically explore the knowledge graph to surface patterns, clusters, and non-obvious connections between entities.

## Steps

1. **Understand the question** — Clarify what type of analysis the user wants:
   - Structural: clusters, hubs, isolated nodes
   - Semantic: similar entities, potential duplicates
   - Relational: path between X and Y, neighbours of Z

2. **Gather context** — Use `get_graph_stats` to understand overall size and node type distribution.

3. **Search and explore** — Use `search_graph` to find relevant starting nodes. Use `get_related_nodes` to walk the neighbourhood of important nodes (depth 1–2).

4. **Find similar nodes** — Use `find_similar_nodes` when the user asks about duplicates, similar entities, or potential merges.

5. **Identify patterns** — Look for:
   - **Hub nodes**: high connectivity (many edges)
   - **Isolated nodes**: no or few connections
   - **Clusters**: groups of tightly connected entities
   - **Missing links**: expected connections that are absent

6. **Synthesise and report** — Present findings concisely:
   - A summary of the main pattern or insight
   - Specific examples from the graph (name the nodes, include counts)
   - Suggested next steps or follow-up questions

## Output format

- Use bullet points for lists of findings
- Name specific nodes with their type in parentheses: `Eurostat (Actor)`
- End with 2–3 suggested follow-up questions the user might want to explore
- Do NOT use markdown tables — they do not render in this interface
