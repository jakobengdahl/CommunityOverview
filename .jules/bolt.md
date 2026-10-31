## 2024-05-15 - Batched Incident Edge Lookup
**Learning:** Passing a node list to networkx `out_edges`/`in_edges` loops over the nodes internally just as standard Python looping does, so there is no measurable gain. Furthermore, it changes the order edges are returned, which can break expectations or tests.
**Action:** Do not use batched list passing to NetworkX edge views as a performance optimization. Stick to standard node iteration for predictable order.
