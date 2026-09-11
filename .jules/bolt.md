## 2025-02-27 - O(N^2) trap in loop-scoped Set initialization
**Learning:** Initializing sets inside a loop to perform uniqueness checks against an accumulating list causes quadratic performance degradation (O(N^2)), as the set is repeatedly rebuilt from a growing list on every iteration.
**Action:** Always lift deduplication set initialization outside of accumulation loops, and update the sets incrementally inside the loop alongside the list.
## 2025-02-27 - GraphStorage Edge Retrieval Optimization
**Learning:** Iterating over all edges (`self.edges.values()`) for localized queries in `backend/core/storage.py` results in O(|E|) operations, which degrades performance for large, sparse graphs. The underlying NetworkX `MultiDiGraph` provides efficient O(degree) access methods.
**Action:** Replaced full edge scans with `self.graph.subgraph(node_ids).edges(data=True)` and `self.graph.in_edges`/`self.graph.out_edges` to significantly speed up `get_edges_between_nodes` and `get_edges_for_node`.
## $(date +%Y-%m-%d) - O(|E|) to O(degree) Edge Lookup Optimization
**Learning:** Found a significant O(|E|) scaling bottleneck in `backend/core/storage.py` within `_external_delete_node()`. The previous code iterated over every edge in the graph (`self.edges.values()`) to find edges incident to a specific node. Graph structures with high edge density degrade heavily on global edge iterations during single node mutations.
**Action:** Always prefer localized lookup using NetworkX (`self.graph.out_edges(node_id)` and `self.graph.in_edges(node_id)`) inside graph structures, providing near O(1)/O(degree) complexity instead of O(|E|). Use `set` to deduplicate incident edges, specifically to gracefully handle self-loops correctly.
