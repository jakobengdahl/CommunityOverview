## 2024-11-20 - Fast NetworkX localized edge retrievals
**Learning:** `graph.out_edges` and `graph.in_edges` in NetworkX support batched multi-node retrieval, making `get_incident_edges` significantly faster (by ~30% in tests) when provided a list of valid node IDs instead of iterating through nodes individually.
**Action:** Always leverage NetworkX's batched node operations when searching for incident edges from multiple node inputs, ensuring to filter `valid_nodes` before passing them to the batched functions to prevent KeyErrors on invalid nodes.
