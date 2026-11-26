# Session backup for operators

Session data is separate from graph data. Back up the graph separately and
retain both artifacts and their association, including deployment and backup
time. Coordinate the graph backup with the session snapshot so they correspond.

The command requires a trusted deployment-specific Python integration module:

```sh
python3 scripts/backup_sessions.py --integration my_deployment.session_backup \
  --output /path/to/session-backup.json
```

The module supplies `authorization_hook()` for the deployment's actual operator
policy and `open_backup(hook)`, a context manager yielding `(service, sessions,
modes)`. The service must use that hook. The command checks `backup_sessions`
access before opening the stores and checks every session and reference again
while creating the document. Run it in the operator's authorization context.

The integration must use read-only graph and layer access, without merge recovery,
and provide `modes.backup_snapshot()`, a context manager yielding a read-only
`WriteModes`-compatible snapshot. It must serialize with all staged writes,
mode changes and merges on every instance throughout the context. The session
backend transaction is held across this snapshot. Observe the deployment's lock
ordering to avoid deadlocks. If these guarantees cannot be provided, backup is
unavailable; a claim that writers are stopped is not a substitute. The stock
file graph backend does not supply this snapshot: its layer mirror is
process-local and loading it may settle a pending merge. There is no built-in
integration for it or for PostgreSQL yet. A deployment must implement and test
one before running the command. Restrict access to the artifact; the command
creates a new owner-only file and refuses to overwrite one.

The command backs up all sessions in the directory or fails without producing an
artifact. It refuses inaccessible references, unreadable staged-session records,
invalid embedded assets, and any unmerged staged changes. Merge or discard those
changes through the normal workflow before stopping writers and retrying. It does
not include transient presence, leases or activity. The backup is held in memory,
so available memory and disk space must accommodate the full session data and
embedded assets. It does not back up templates or graph data.

Format version 1 stores embedded image data URLs in the presentation state and
in a deduplicated asset table. Each asset's SHA-256 is over its decoded bytes.
The document `integrity.sha256` is over UTF-8 JSON of the entire document with
`integrity` omitted, sorted keys, no whitespace, unescaped Unicode, and no
non-finite numbers. These hashes detect accidental corruption; they do not
provide authentication. Restore is not available yet and is planned separately.
Retain both artifacts securely until a restore can be tested against a separate
store; a graph-only backup cannot recover sessions.
