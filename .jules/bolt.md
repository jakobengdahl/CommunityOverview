## 2025-02-27 - O(N^2) trap in loop-scoped Set initialization
**Learning:** Initializing sets inside a loop to perform uniqueness checks against an accumulating list causes quadratic performance degradation (O(N^2)), as the set is repeatedly rebuilt from a growing list on every iteration.
**Action:** Always lift deduplication set initialization outside of accumulation loops, and update the sets incrementally inside the loop alongside the list.
## 2025-02-27 - GraphStorage Edge Retrieval Optimization
**Learning:** Iterating over all edges (`self.edges.values()`) for localized queries in `backend/core/storage.py` results in O(|E|) operations, which degrades performance for large, sparse graphs. The underlying NetworkX `MultiDiGraph` provides efficient O(degree) access methods.
**Action:** Replaced full edge scans with `self.graph.subgraph(node_ids).edges(data=True)` and `self.graph.in_edges`/`self.graph.out_edges` to significantly speed up `get_edges_between_nodes` and `get_edges_for_node`.
## 2026-09-14 - Optimized localized edge lookup in node deletion
**Learning:** Full edge scans in `delete_nodes` (`self.edges.items()`) and `_external_delete_node` severely degraded performance on large graphs.
**Action:** Use NetworkX graph methods `out_edges` and `in_edges` for O(degree) localized lookups when determining incident edges for deletion, while using a set to deduplicate self-loops.
