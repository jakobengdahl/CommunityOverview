# Node shapes phase 6b-4 and 6c: live session writers and library invalidation

Part of phase 6 in [NODE_SHAPES.md](../NODE_SHAPES.md#migration-and-small-implementation-phases).
A shape library commit that also changes a session (6b-3) adopts its staged
state into the live `Session` while it holds the session backend's
transaction. A live writer in `session_manager.py` snapshots the session,
mutates it, persists it and on failure restores the snapshot. If a library
commit adopted its state between that snapshot and the rollback, the rollback
would erase a change the library file already holds. Slice 6b-4a marks the
synchronous writers, which run their whole span on the calling thread; 6b-4b
marks `apply_ops`, whose persist runs on a worker thread behind an await;
6b-4c marks the staging and layer writers, whose op follows a graph layer
change.

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
| `apply_ops` (browser batches, REST `/ops`, async rename) | from the rollback snapshot through the batch's ops, budget check, off-thread persist and rollback |
| `stage_graph_change` (staged graph writes) | from the session lookup and sequence check through the layer commit, the op and its persist |
| `announce_layer_change` (discard and merge of staged changes) | the op and its persist |

Broadcasts and lease or claim bookkeeping after a commit stay outside the
marker: they do not change persisted session state.

## Staging and layer writers

A staging or layer writer applies its op after a graph layer change has
committed, so a failure after that commit point must not be reported as if
nothing had changed. Each writer takes one of two strategies:

- **`stage_graph_change` takes the marker before the commit point.** The
  layer commit is a callback it runs itself and stamps with the next session
  sequence number, so the sequence check, the commit and the op that takes
  that number are one section. A marker that cannot be taken raises before
  the layer is touched. After the commit, a failed persist is logged and the
  op still fans out, as before.
- **`announce_layer_change` catches and logs a post-commit failure.** Its
  callers change the layer (discard, merge) before calling it, so the marker
  cannot precede that commit. It takes the marker around the op and its
  persist; failing to take the marker is caught and logged like a failed op,
  and the call returns no op instead of raising.

## Cancellation in `apply_ops`

A cancelled task cannot stop the save thread it started, so `apply_ops` waits
for that thread to finish before it leaves its section: the marker and the
per-session lock stay held until the outcome of the save is known. A save that
lands is then committed like any other batch (claims, leases and broadcasts)
and the task ends in `CancelledError`; a save that fails rolls the session back
and raises the save's error, as without a cancellation. The save is awaited
as an executor future, not a task, so loop teardown that cancels every task
cannot end the wait early either.

Rolling back on `CancelledError` instead was rejected: the save may still
land after the rollback, leaving memory behind disk, and the marker would
clear while that write was in flight, so a library commit could adopt state
into the session and then be overwritten by it.

## Library invalidation and reconnect

Slice 6c tells clients when a saved shape they render has changed.

- **Invalidation:** after a library commit, `ShapeLibrary` rebuilds the
  reference index from the persisted sessions and calls its `invalidate`
  callback, which `SessionManager.publish_library_invalidation` publishes as
  `{"type": "library_invalidated", "key", "revision"}` on the stream of each
  session that references the entry, and of no other. Like a summon it is
  ephemeral: not sequenced, not replayed, subject to the same subscriber queue
  bound. It names no definition and no other session. A failure to publish is
  logged; the commit stands. A session under the writing marker may reference
  the entry only in memory, so it is judged again once its outermost write
  section exits, on its persisted or rolled-back state, and notified if it
  then references the entry.
- **Fetch:** `GET /api/shape-library` returns the current entries (no
  tombstones, no owners) and the library revision. A deleted key is absent,
  so its references resolve to the built-in card, never to a lower-priority
  shape or the old definition.
- **Reconnect ordering:** every snapshot and catch-up carries
  `library_revision`, read off the event loop before the payload is built;
  `null` when the library cannot be read, which the stream survives. The
  browser sync client hands that revision, or an invalidation's, to the host
  before it handles the event; so does a shape operation adding a `saved:`
  reference, as `null`, since the entry may have changed while the session
  did not reference it. A `null` revision always fetches; while the host
  fetches a newer library, that event and every later stream event wait, in
  order. So the snapshot or missed operations are resolved against the
  fetched library, never the one from before the disconnect. A failed fetch
  drops the cached library: saved references render the built-in card until
  the next revision arrives.
- **Loading and switching:** the host shows saved shapes only once the
  current session's stream has confirmed their revision, so a closed session
  loaded later resolves the current library. Closing the client on a session
  switch drops its held events and any fetch that settles afterwards. The
  fetched library only updates the canvas; nothing is sent back as a session
  operation.

The bus is per process, like the marker: with several instances sharing
session storage, a commit invalidates only streams on its own instance, and
others learn the revision on their next connect.

## Out of scope

- The marker is per process, like the live sessions it protects; another
  process has its own in-memory sessions and reads files under
  `.sessions.lock` (6b-2).
- Session creation, copying and deletion: they replace whole documents
  rather than mutate a live one under a rollback snapshot.
- The API, editor wiring and permissions that turn library mutations on
  (6d), which pass the store as `writers`; performance and accessibility
  gates (6e). Library mutations stay disabled.
- No change to phase 4 operation semantics, sync ordering or resync rebase,
  the blocking Windows lock (6b-1), read locking (6b-2), or commit order and
  staged-state adoption (6b-3).
