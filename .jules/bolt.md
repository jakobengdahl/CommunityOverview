
## 2026-11-01 - NetworkX Batched Edge Retrieval Rejection
**Learning:** An attempt to optimize edge retrieval by passing a list of nodes to NetworkX's `out_edges` and `in_edges` (instead of iterating manually in Python) was rejected. Reviewers noted that NetworkX internally just loops over the nodes anyway, meaning there is no measurable performance gain. Additionally, this approach altered the return order of edges, which the system relied on.
**Action:** Do not use `out_edges`/`in_edges` batch retrieval as a micro-optimization in this codebase, as it breaks expected return order without meaningful performance benefits.
