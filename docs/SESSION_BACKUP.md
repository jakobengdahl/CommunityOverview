# Session backup for operators

Session data is separate from graph data. Back up the graph separately and
retain both artifacts and their association, including deployment and backup
time. Coordinate the graph backup with the session snapshot so they correspond.

For the file graph backend, use the supplied offline snapshot integration.
Stop all application instances and all other writers, then take one coordinated,
immutable filesystem snapshot containing the session directory and graph file
with its journal, layers and merge sidecars. Keep this snapshot private and
unchanged throughout the command. The command reads only this snapshot, never a
live store. Retain the corresponding graph snapshot as the graph backup. A
snapshot taken while any writer is active is not supported.

Provide a trusted Python module with `authorization_hook()` returning the
same operator policy used by the deployment. Run in the operator's authorization
context, with that module on `PYTHONPATH`:

```sh
export SESSION_BACKUP_AUTH_MODULE=my_deployment.backup_auth
export SESSION_BACKUP_GRAPH=/snapshot/graph.json
export SESSION_BACKUP_SESSIONS=/snapshot/sessions
python3 scripts/backup_sessions.py --integration scripts.session_backup_file_snapshot \
  --output /secure/session-backup.json
```

If the graph backend uses a custom `journal_path`, include that journal in the
same coordinated snapshot and set `SESSION_BACKUP_GRAPH_JOURNAL` to its snapshot
path. The integration requires that file when the variable is set; otherwise it
uses the default journal beside the graph. The operator must match this setting
to the backend configuration, because the graph file does not record the path.
An incomplete or malformed journal is refused; repair it through the normal
recovery workflow and take a fresh snapshot before retrying.

The command checks `backup_sessions` admin authorization before opening stores,
and checks every session and reference before producing a document. The default
hook denies admin access. The integration makes private working copies, and
refuses a pending merge; resolve such a merge through the normal application
workflow before taking a fresh offline snapshot. Restrict artifact access: the
command creates a new owner-only file and refuses to overwrite one.

For PostgreSQL or another deployment, a trusted integration may supply
`authorization_hook()` and `open_backup(hook)`, yielding `(service, sessions,
modes)`. It must provide a read-only `modes.backup_snapshot()` coordinated with
all staged writes, mode changes and merges across instances, and use the same
hook in the service. No PostgreSQL integration is supplied. The session backend
transaction is held during collection; authorization runs after it is released.

The command backs up all sessions in the directory or fails without producing an
artifact. It refuses inaccessible references, unreadable staged-session records,
invalid embedded assets, and any unmerged staged changes. Merge or discard those
changes through the normal workflow before taking a fresh snapshot and retrying. It does
not include transient presence, leases or activity. The backup is held in memory,
so available memory and disk space must accommodate the full session data and
embedded assets. It does not back up templates or graph data.

Format version 1 stores embedded image data URLs in the presentation state and
in a deduplicated asset table. Each asset's SHA-256 is over its decoded bytes.
Each session's `write_mode` and `write_mode_source` describe its effective mode;
`write_mode_mirror` preserves the original fields present in the session record,
which can differ from the authoritative staged-session record.
The document `integrity.sha256` is over UTF-8 JSON of the entire document with
`integrity` omitted, sorted keys, no whitespace, unescaped Unicode, and no
non-finite numbers. These hashes detect accidental corruption; they do not
provide authentication. Restore is not available yet and is planned separately.
Retain both artifacts securely until a restore can be tested against a separate
store; a graph-only backup cannot recover sessions.
