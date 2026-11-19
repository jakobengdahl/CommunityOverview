# Node shapes phase 6b-4a: synchronous live session writers under the writing marker

Part of phase 6 in [NODE_SHAPES.md](../NODE_SHAPES.md#migration-and-small-implementation-phases).
A shape library commit that also changes a session (6b-3) adopts its staged
state into the live `Session` while it holds the session backend's
transaction. A live writer in `session_manager.py` snapshots the session,
mutates it, persists it and on failure restores the snapshot. If a library
commit adopted its state between that snapshot and the rollback, the rollback
would erase a change the library file already holds. This slice marks the
synchronous writers, which run their whole span on the calling thread.

## The marker

`SessionStore.writing(session_id)` is a context manager that marks a live
mutation of one session; `SessionStore.is_writing(session_id)` reports
whether any such section is open.

- **Entry** takes the backend's `transaction()` for the moment it records the
  marker. It therefore waits for a library commit in flight to finish, and a
  commit that checks the marker under its transaction sees either no writer
  or this one, never a writer that has already taken its rollback snapshot.
  Entry does not hold the transaction afterwards, so the writer's own persist
  (on any thread) is not blocked by it.
- **Refusal:** `ShapeLibrary`, given the store as `writers`, refuses a
  mutation for a session that is being written with `LibraryConflict`,
  before the library or the session is touched. The caller retries.
- **Nesting:** sections count per session. Since entry has released the
  transaction before the section body runs, a writer called inside another
  writer's section takes the transaction afresh and only raises the count, so
  it does not deadlock; its exit only lowers the count, so it does not end the
  marker. The marker clears only when the count reaches zero, at the
  outermost section's exit, on success or on error.

## Which writers take it

Each section covers the writer's whole read-check-mutate-persist-rollback
span, so the writer changes no live session state outside it:

| Writer | Covered span |
|---|---|
| `_apply_op_sync` (every synchronous op path: layout, node refs, annotations, groups, rename, describe, write mode, pushed results) | snapshot, apply, persist and rollback |
| `_replay_action` (undo/redo) | from the record lookup and conflict checks through the record flag, history revision and deleted-id changes, the nested `_apply_op_sync`, and their restore on failure |

Broadcasts and lease or claim bookkeeping after a commit stay outside the
marker: they do not change persisted session state.

## Out of scope

- `apply_ops` (6b-4b): its persist runs on a worker thread behind an await,
  so a cancellation there needs its own handling before the marker can clear.
- `stage_graph_change` and `announce_layer_change` (6b-4c): their op is
  applied after the graph layer change has committed, so a failure to take
  the marker must not propagate from there.
- The marker is per process, like the live sessions it protects; another
  process has its own in-memory sessions and reads files under
  `.sessions.lock` (6b-2).
- Session creation, copying and deletion: they replace whole documents
  rather than mutate a live one under a rollback snapshot.
- Revision invalidation and reconnect (6c); the API, editor wiring and
  permissions that turn library mutations on (6d), which pass the store as
  `writers`; performance and accessibility gates (6e). Library mutations stay
  disabled.
- No change to phase 4 operation semantics, sync ordering or resync rebase,
  the blocking Windows lock (6b-1), read locking (6b-2), or commit order and
  staged-state adoption (6b-3).
