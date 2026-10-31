
## 2025-02-27 - Batch NetworkX Edge Lookups
**Learning:** Iterating over nodes individually in NetworkX's `out_edges` and `in_edges` introduces significant overhead compared to passing an iterable of valid nodes directly to these methods.
**Action:** To optimize edge retrieval with NetworkX, pass an iterable of pre-filtered valid node IDs directly to `out_edges()` and `in_edges()` for batched multi-node retrieval, rather than querying nodes individually in a Python loop.

## 2025-02-27 - NetworkX Edge Lookups Batching Rejected
**Learning:** Passing a node list to NetworkX `out_edges`/`in_edges` loops over the nodes internally just as iterating in Python does, offering no measurable gain while changing the order edges are returned.
**Action:** Do not batch NetworkX edge queries by passing lists of nodes to `out_edges` or `in_edges`; stick to the existing individual node loops to preserve edge order and avoid unnecessary changes.
