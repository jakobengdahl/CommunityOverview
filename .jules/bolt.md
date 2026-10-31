## 2026-10-31 - NetworkX batched node queries rejection
**Learning:** Passing a node list to networkx `out_edges`/`in_edges` loops over the nodes internally just as the current code does, so there is no measurable gain, and it changes the order edges come back in.
**Action:** Do not attempt to optimize `get_incident_edges` (or similar networkx multi-node queries) by simply passing a list of nodes instead of looping, as it yields no real performance gain and can break assumptions on edge order.
