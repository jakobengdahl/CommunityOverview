## 2025-02-27 - O(N^2) trap in loop-scoped Set initialization
**Learning:** Initializing sets inside a loop to perform uniqueness checks against an accumulating list causes quadratic performance degradation (O(N^2)), as the set is repeatedly rebuilt from a growing list on every iteration.
**Action:** Always lift deduplication set initialization outside of accumulation loops, and update the sets incrementally inside the loop alongside the list.
## 2025-02-27 - GraphStorage Edge Retrieval Optimization
**Learning:** Iterating over all edges (`self.edges.values()`) for localized queries in `backend/core/storage.py` results in O(|E|) operations, which degrades performance for large, sparse graphs. The underlying NetworkX `MultiDiGraph` provides efficient O(degree) access methods.
**Action:** Replaced full edge scans with `self.graph.subgraph(node_ids).edges(data=True)` and `self.graph.in_edges`/`self.graph.out_edges` to significantly speed up `get_edges_between_nodes` and `get_edges_for_node`.
## 2025-03-01 - O(|E|) edge scan removed from _external_delete_node
**Learning:** Found that external node deletion was scanning all edges in the graph `for edge in self.edges.values()` to find incident edges, which causes O(|E|) performance degradation per node deleted. NetworkX's `self.graph.out_edges` and `in_edges` provides O(degree) localized lookups.
**Action:** When searching for edges connected to a specific node, always use NetworkX's localized edge retrieval instead of iterating over the entire `edges` dict. Use a `set` to avoid duplicates for self-loops and call `out_edges` before `in_edges` for test suite ordering.
