## 2024-10-27 - Batching NetworkX Edge Retrieval
**Learning:** In `backend/core/storage.py`, `NetworkX` methods like `out_edges` and `in_edges` support an iterable of nodes (`nbunch`), pushing iteration down to faster native code. Using a Python loop over nodes to query edges individually has high overhead.
**Action:** Always prefer passing an `nbunch` list to NetworkX `out_edges` and `in_edges` to perform batched edge lookups instead of iterating over nodes manually in a Python loop.
