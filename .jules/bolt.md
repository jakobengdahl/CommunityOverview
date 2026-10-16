## 2024-05-24 - NetworkX Batched Edge Retrieval
**Learning:** NetworkX's `out_edges` and `in_edges` support passing a sequence of nodes (`nbunch`) to retrieve edges for all nodes in the sequence at once. Using a Python loop to call these functions for individual nodes introduces significant overhead for O(degree) localized lookups.
**Action:** Always prefer batched queries (e.g., `G.out_edges(list_of_nodes)`) over iterating in Python when fetching localized edges from NetworkX to drastically improve performance (4-5x faster in benchmarks).
