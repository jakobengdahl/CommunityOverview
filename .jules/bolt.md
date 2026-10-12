
## 2024-05-18 - Fast Batched NetworkX Queries
**Learning:** In backend/core/storage.py, fetching edges per-node in a loop is significantly slower than passing a list of valid node IDs to the NetworkX batched methods (`self.graph.out_edges(nodes)`).
**Action:** When working with multiple node queries in NetworkX, always filter valid nodes first and pass them as a batch to native edge retrieval methods instead of doing loops in python. Ensure `out_edges` is called first, followed by `in_edges` to respect test suite assumptions about edge iteration order.
