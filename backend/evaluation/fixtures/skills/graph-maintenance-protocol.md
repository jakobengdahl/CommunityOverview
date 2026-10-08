---
id: graph-maintenance-protocol
name: Graph Maintenance Protocol
description: Mandatory protocol for reading, changing and confirming graph data.
when-to-use: Whenever a request reads, adds, changes or deletes nodes or edges in the graph.
allowed-tools: [search_graph, get_related_nodes, find_similar_nodes, add_nodes, update_node, list_node_types, get_schema]
---

# Graph Maintenance Protocol

These four rules are mandatory. They are not style preferences.

## 1. ID-first execution

Resolve every name to a unique node id **before** any write or relationship
operation. Use `search_graph` or `find_similar_nodes` to obtain the id, then use
that exact id in the write call. Never pass a node id you have not seen in a
tool result during this conversation — a guessed id fails silently or writes to
the wrong node.

If a name matches more than one node, do **not** pick one. Stop, and report the
candidates with their ids so the user can choose.

## 2. Mandatory verification protocol

Never report a change as successful on the strength of the update call alone.
After every write, perform a **retrieval** call (`get_related_nodes` on the
written id, or `search_graph`) and compare what comes back against what you
intended. Only report success once the returned data shows the change.

## 3. Comprehensive object inspection

When the request says "complete", "total", "all" or "full" — for example a full
translation of a node — change **every** text-bearing field the request covers,
not only the primary one. Inspect the whole returned object, including fields
you did not set, before claiming the work is done.

## 4. Discrepancy handling

If the user disputes a change you reported, re-fetch the node immediately and
present the current stored values. Never answer from your memory of the earlier
operation.
