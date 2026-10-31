
## 2024-10-31 - NetworkX Batched Edge Retrieval Overhead
**Learning:** Passing a node list to `networkx.out_edges()` or `in_edges()` actually loops over the nodes internally exactly as iterating the nodes manually does in Python, and surprisingly changes the iteration order of the returned edges. There is no measurable performance gain.
**Action:** Do not batch query edge requests in NetworkX by passing a list of nodes to `out_edges`/`in_edges`. Continue iterating over the nodes one by one to ensure the expected ordering is preserved.
