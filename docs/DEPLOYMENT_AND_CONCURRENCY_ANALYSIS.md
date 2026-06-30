# Concurrency Analysis — GraphStorage

This document covers the thread-safety and multi-process safety design of `GraphStorage`
(`backend/core/storage.py`). It is referenced from [DEVELOPMENT.md](../backend/DEVELOPMENT.md).

For deployment documentation see [DEPLOYMENT_GUIDE.md](./DEPLOYMENT_GUIDE.md).
For the CI/CD artifact contract see [DEPLOYMENT_CONTRACT.md](./DEPLOYMENT_CONTRACT.md).

---

## Implemented protections

### Threading lock (in-memory protection)

All mutations use a reentrant lock (`threading.RLock`) so the same thread can acquire
the lock multiple times without deadlocking:

```python
class GraphStorage:
    def __init__(self, ...):
        self._lock = threading.RLock()

    def add_nodes(self, nodes, edges):
        with self._lock:          # protects the entire operation
            # ... all logic runs atomically
            self.save()
```

Protected operations: `load()`, `save()`, `add_nodes()`, `update_node()`, `delete_nodes()`.

### File locking (multi-process protection)

Cross-platform file locking prevents corruption when multiple processes share the same
`graph.json` file:

```python
if sys.platform == 'win32':
    import msvcrt   # Windows: msvcrt.locking
else:
    import fcntl    # Unix/Linux/macOS: fcntl.flock
```

- Shared lock for reads (multiple concurrent readers allowed)
- Exclusive lock for writes (only one writer at a time)

### Atomic writes (corruption prevention)

Saves use a write-to-temp-then-rename strategy:

```python
def save(self):
    temp_fd, temp_path = tempfile.mkstemp(...)   # 1. write to temp file
    os.fsync(f.fileno())                          # 2. flush to disk
    os.rename(temp_path, self.json_path)          # 3. atomic rename
```

If the process dies mid-write, the original file is left intact. `os.rename` is atomic on
most filesystems. `fsync()` ensures data reaches disk before the rename.

---

## Remaining limitations

| Aspect | Status | Notes |
|--------|--------|-------|
| Thread safety | ✅ Implemented | `threading.RLock` on all mutations |
| File locking | ✅ Implemented | `fcntl.flock` / `msvcrt.locking` |
| Atomic writes | ✅ Implemented | temp file + rename |
| Session management | ⚠️ Basic | Basic Auth, no per-user tracking |
| Transactions | ⚠️ Not supported | No rollback on partial failure |
| Multi-process | ✅ Works | File lock protects between processes |

The application is safe for concurrent users at the current deployment scale.
Transactions and per-user tracking remain open items for a future storage backend
migration (see [CORE_RUNTIME_AND_EXTENSION_ENABLEMENT.md](./CORE_RUNTIME_AND_EXTENSION_ENABLEMENT.md),
workstream 5).

---

## Test coverage

Concurrent access is tested in `backend/core/tests/test_storage.py`:

```python
class TestGraphStorageConcurrency:
    def test_concurrent_add_nodes_no_data_loss()     # 10 threads, 5 nodes each
    def test_concurrent_update_nodes_no_data_loss()  # 10 threads updating the same node
    def test_concurrent_mixed_operations()           # 20 threads with mixed operations
    def test_atomic_save_prevents_corruption()       # 20 threads saving concurrently
    def test_reload_during_concurrent_writes()       # reload during active writes
```

Run the concurrency tests:

```bash
pytest backend/core/tests/test_storage.py::TestGraphStorageConcurrency -v
```
