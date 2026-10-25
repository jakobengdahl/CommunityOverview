
## 2024-10-25 - Batched Edge Lookups in NetworkX
**Learning:** In GraphStorage `get_incident_edges`, iterating over multiple nodes and making individual `out_edges`/`in_edges` queries for each results in an O(|V|) Python loop which is noticeably slow for large sets of nodes. NetworkX natively supports passing an iterable of valid node IDs directly to `out_edges()` and `in_edges()`.
**Action:** When querying incident edges for multiple nodes, always pass a list of valid node IDs directly into the NetworkX query to perform a single batched lookup (O(degree)). Deduplicate using a dictionary keyed by `edge.id` as the NetworkX edge attribute dictionaries themselves are unhashable.
