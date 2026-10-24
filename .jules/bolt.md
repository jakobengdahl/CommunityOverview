
## 2024-10-19 - Batched Edge Retrieval in NetworkX
**Learning:** When retrieving edges for multiple nodes from a NetworkX graph, `graph.out_edges(node_ids)` and `graph.in_edges(node_ids)` natively support accepting a list of nodes. Passing an iterable of nodes is significantly faster than looping over nodes individually and calling `out_edges(node)` / `in_edges(node)` due to reduced overhead.
**Action:** Always pass an iterable of pre-filtered valid node IDs directly to `out_edges()` and `in_edges()` for batched multi-node retrieval rather than querying nodes individually in a Python loop. Keep in mind to always call `out_edges` first followed by `in_edges` to maintain typical iteration order.
