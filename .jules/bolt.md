
## 2024-05-19 - NetworkX batched node queries
**Learning:** In backend/core/storage.py, querying NetworkX for edges inside a python `for` loop per-node (e.g. `for node in nodes: G.out_edges(node)`) adds significant function call overhead. NetworkX natively supports passing an iterable of nodes (`nbunch`) directly to `out_edges` and `in_edges`.
**Action:** When working with NetworkX, always pass a list of valid nodes to edge lookup methods (like `G.out_edges(list_of_nodes)`) rather than looping over nodes individually. This simple structural change yields a ~3-4x performance speedup.
