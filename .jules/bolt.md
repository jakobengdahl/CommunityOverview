
## 2024-05-14 - Optimize Graph Edge Retrieval
**Learning:** In Python, iterating over a list of node IDs and calling NetworkX graph functions (like `out_edges` and `in_edges`) individually is much slower than passing the entire list of node IDs to the function (batched retrieval). Passing a bunch allows NetworkX to handle the iteration in its optimized core.
**Action:** Always prefer passing iterables directly to NetworkX functions that accept `nbunch` arguments over looping in Python space when bulk retrieving graph metadata.

## 2024-05-14 - NetworkX Batch Edge Retrieval Is Not Faster
**Learning:** Passing a node list to `networkx` `out_edges`/`in_edges` loops over the nodes internally just as Python code does, providing no measurable performance gain. Additionally, it changes the order in which edges are returned, which can break test expectations.
**Action:** Do not attempt to optimize `networkx` edge retrievals by converting loops over nodes to batched `nbunch` calls.
