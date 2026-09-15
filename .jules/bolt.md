## 2025-02-27 - O(N^2) trap in loop-scoped Set initialization
**Learning:** Initializing sets inside a loop to perform uniqueness checks against an accumulating list causes quadratic performance degradation (O(N^2)), as the set is repeatedly rebuilt from a growing list on every iteration.
**Action:** Always lift deduplication set initialization outside of accumulation loops, and update the sets incrementally inside the loop alongside the list.
## 2025-02-27 - GraphStorage Edge Retrieval Optimization
**Learning:** Iterating over all edges (`self.edges.values()`) for localized queries in `backend/core/storage.py` results in O(|E|) operations, which degrades performance for large, sparse graphs. The underlying NetworkX `MultiDiGraph` provides efficient O(degree) access methods.
**Action:** Replaced full edge scans with `self.graph.subgraph(node_ids).edges(data=True)` and `self.graph.in_edges`/`self.graph.out_edges` to significantly speed up `get_edges_between_nodes` and `get_edges_for_node`.
## 2025-03-09 - NetworkX Edge Lookups via in_edges and out_edges
**Learning:** Iterating through `self.edges.values()` and checking `edge.source` or `edge.target` scales poorly ($O(V+E)$) and causes significant performance degradation when deleting nodes that require cascade-deleting incident edges.
**Action:** Use `self.graph.out_edges(node_id, keys=True)` and `self.graph.in_edges(node_id, keys=True)` with a `set` to deduplicate (for self-loops) instead of full edge scans to quickly find incident edges in $O(\text{degree})$ time.
