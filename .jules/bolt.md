## 2024-11-20 - Fast NetworkX localized edge retrievals
**Learning:** `graph.out_edges` and `graph.in_edges` in NetworkX support batched multi-node retrieval, making `get_incident_edges` significantly faster (by ~30% in tests) when provided a list of valid node IDs instead of iterating through nodes individually.
**Action:** Always leverage NetworkX's batched node operations when searching for incident edges from multiple node inputs, ensuring to filter `valid_nodes` before passing them to the batched functions to prevent KeyErrors on invalid nodes.

## 2024-11-20 - NetworkX batched edges vs iteration order
**Learning:** While batching NetworkX operations (like passing `nbunch` to `out_edges` or `in_edges`) yields microsecond performance improvements, it silently alters the returned edge order (e.g., all out-edges first across the whole batch, then all in-edges, vs interleaved node-by-node). If downstream callers rely on implicit iteration ordering (or test suites assert exact sequence matches), this optimization causes behavioural changes that often do not justify the tiny performance gain in real-world workloads where application-level filtering dominates time.
**Action:** Do not micro-optimize NetworkX edge iterations to use `nbunch` batching if it changes edge return order, unless a critical bottleneck demands it and explicit sorting is acceptable.
