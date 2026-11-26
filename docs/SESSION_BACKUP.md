# Session backup for operators

Session data is separate from graph data. For a standalone deployment using the
file graph and file session stores, stop the application and every other writer
to both stores before taking either backup. Keep them stopped until both backups
are complete. The graph file, its journal and layer sidecars belong to the graph
store; use the deployment's graph backup procedure for those files. Keep the
session backup and graph backup together, recording which artifacts belong to
the same stopped deployment and backup time.

From the repository root, run as an operator with read access to the graph and
session files and write access to the destination directory:

```sh
python3 scripts/backup_sessions.py --graph /path/to/graph.json \
  --sessions /path/to/sessions --output /path/to/session-backup.json \
  --writers-stopped
```

The flag confirms that all writers are stopped; the command cannot detect live
writers. It checks the standalone authorization mode for each session and its
graph references. A deployment using a custom authorization hook or another
storage backend needs an operator integration that supplies its configured
`GraphService`, session backend and `WriteModes` to
`backend.service.session_backup.create_backup` while all writers are stopped;
the standalone command cannot load that deployment's access policy. The caller
must run in the appropriate operator authorization context. Backups contain
sensitive canvas data and IDs: restrict access to the artifact. The command
creates a new file with owner-only permissions and refuses to overwrite a file.

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
