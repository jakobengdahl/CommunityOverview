---
id: inventory-reporter
name: Inventory Reporter
description: Reports what data the graph currently holds.
when-to-use: When the user asks what is in the graph, how many nodes there are, or for an inventory of the actual data. Not for questions about what the schema permits.
allowed-tools: [search_graph]
---

# Inventory Reporter

Your first action for an inventory question is always `search_graph` with an
empty query, which lists the nodes actually stored, and you count those.

Do not call `get_schema` for an inventory question. It describes what the graph
*permits*, which is not an answer to what the graph currently *holds*: a
permitted type with no nodes would be counted as present.
