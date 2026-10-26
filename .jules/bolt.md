
## 2024-05-24 - NetworkX Iterable Edge Lookups
**Learning:** In NetworkX, checking multiple nodes' incident edges one by one using a Python for-loop (`for n in nodes: G.out_edges(n)`) is noticeably less efficient than passing the list of valid node IDs directly to `out_edges()` and `in_edges()` (`G.out_edges(nodes)`). NetworkX natively handles an iterable of nodes (`nbunch`) effectively in `O(degree)` time.
**Action:** When working with NetworkX, always pass an iterable of valid nodes to localized edge retrieval functions (`out_edges`/`in_edges`) instead of iterating over individual nodes in a Python loop. Also ensure to call `out_edges` first followed by `in_edges` to maintain typical expected test suite iteration order, and deduplicate edges using a dictionary keyed by ID.
