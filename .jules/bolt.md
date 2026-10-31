
## 2026-10-31 - NetworkX Node List Parameter is a Trap
**Learning:** Passing a node list to `networkx.out_edges`/`in_edges` does not actually perform a fast native batched lookup; NetworkX simply loops over the nodes internally just as standard Python code does. Furthermore, it changes the order in which edges are returned.
**Action:** When attempting to optimize localized edge lookups, do not rely on passing a list of nodes to NetworkX edge querying methods to gain performance. This optimization has been proven ineffective and alters expected iteration order.
