
## 2024-05-14 - Optimize Graph Edge Retrieval
**Learning:** In Python, iterating over a list of node IDs and calling NetworkX graph functions (like `out_edges` and `in_edges`) individually is much slower than passing the entire list of node IDs to the function (batched retrieval). Passing a bunch allows NetworkX to handle the iteration in its optimized core.
**Action:** Always prefer passing iterables directly to NetworkX functions that accept `nbunch` arguments over looping in Python space when bulk retrieving graph metadata.
