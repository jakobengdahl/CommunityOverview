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
| Session management | ⚠️ Basic | Basic Auth, anonymous guest identity; shared sessions are single-instance (see below) |
| Transactions | ⚠️ Not supported | No rollback on partial failure |
| Multi-process | ✅ Works | File lock protects between processes |

The application is safe for concurrent users at the current deployment scale.
Transactions and per-user tracking remain open items for a future storage backend
migration (see [CORE_RUNTIME_AND_EXTENSION_ENABLEMENT.md](./CORE_RUNTIME_AND_EXTENSION_ENABLEMENT.md),
workstream 5).

---

## Shared sessions — single-instance constraint & SaaS seams

The multi-user shared-session feature (see
[MULTI_USER_SESSIONS_DESIGN.md](./MULTI_USER_SESSIONS_DESIGN.md)) is
**single-instance by design in the open core**. Realtime fan-out (SSE),
presence and selection claims are held in process memory, so two browsers
collaborate correctly only when they are served by the **same** backend
process. Running more than one core replica behind a load balancer would split
collaborators across processes: each would see a consistent-but-partial view,
because neither the event bus nor presence crosses process boundaries.

Session **content** is persisted (one JSON file per session under
`data/sessions/<id>.json`, atomic temp+rename writes with the same file-lock
discipline as the graph store) and survives a restart, so a single-instance
deployment loses only ephemeral state (the live op stream, roster and claims) on
a bounce — clients reconnect and resync from a snapshot. There is **no automatic
session retention/eviction** (design D13): a session lives until explicitly
deleted.

Two seams keep the core single-instance while letting the SaaS layer scale out
(design §3.2, D5) — the core ships only the in-process implementations:

| Seam | Core implementation | SaaS replacement |
|------|---------------------|------------------|
| `SessionPersistenceBackend` (`core/session_store.py`) | `FileSessionPersistenceBackend` — one JSON file per session | Shared DB-backed store (e.g. Postgres) so every replica reads/writes the same session state |
| `SessionEventBus` (`core/session_hub.py`) | `InProcessEventBus` — per-subscriber asyncio queues, one process | Redis (or equivalent) pub/sub bus so applied ops, presence and claims fan out across replicas |

The core's only obligation to SaaS is to keep these two seams stable and to pass
through an optional identity context on the session endpoints when present.
Everything else about multi-instance scale-out (shared DB, Redis fan-out,
account-bound history, workspace ACLs) lives behind that boundary and is out of
scope for the open core.

The REST/ops surface is bounded to keep a single instance healthy under load:
each op batch is capped by op count (≤ 500) and body size (≤ 256 KB → `413` —
an op carrying a validated embedded image is budgeted separately instead, and
the session's cumulative document/image size is also checked per batch), and
a per-client token bucket (200 burst, 100 ops/s refill → `429`) throttles a
runaway client (design §3.9).

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
