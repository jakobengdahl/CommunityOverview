## 2025-02-27 - O(N^2) trap in loop-scoped Set initialization
**Learning:** Initializing sets inside a loop to perform uniqueness checks against an accumulating list causes quadratic performance degradation (O(N^2)), as the set is repeatedly rebuilt from a growing list on every iteration.
**Action:** Always lift deduplication set initialization outside of accumulation loops, and update the sets incrementally inside the loop alongside the list.
## 2025-02-27 - GraphStorage Edge Retrieval Optimization
**Learning:** Iterating over all edges (`self.edges.values()`) for localized queries in `backend/core/storage.py` results in O(|E|) operations, which degrades performance for large, sparse graphs. The underlying NetworkX `MultiDiGraph` provides efficient O(degree) access methods.
**Action:** Replaced full edge scans with `self.graph.subgraph(node_ids).edges(data=True)` and `self.graph.in_edges`/`self.graph.out_edges` to significantly speed up `get_edges_between_nodes` and `get_edges_for_node`.
## 2025-02-28 - Optimize node deletion graph traversals
**Learning:** In `backend/core/storage.py`, deleting a node triggers a check for all its incident edges to delete them first. Previously, this was done using an $O(|E|)$ scan iterating over all edges in `self.edges.values()`. For large graphs, this led to massive performance degradation during bulk deletions.
**Action:** When finding edges incident to a specific node, leverage the underlying NetworkX `MultiDiGraph` via `self.graph.out_edges(node_id)` and `self.graph.in_edges(node_id)` which runs in $O(\text{degree})$ time. Ensure `keys=True` is passed to retrieve edge IDs and deduplicate self-loops with a `set`. Always process `out_edges` then `in_edges` to maintain test suite expectations.
