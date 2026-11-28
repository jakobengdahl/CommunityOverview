# Session backup and restore threat model

This model describes offline session backup and restore on main. It covers the
operator commands, the trusted integrations they load, the backup artifact, and
the restored workspace. The backup artifact is treated as untrusted input on
restore: it may have been altered at rest or in transit, or created elsewhere.
The operator procedure is in [SESSION_BACKUP.md](../../SESSION_BACKUP.md).

## Assets

- Visualization session records: names, descriptions, revisions, write modes and
  presentation state, including annotations and embedded image data URLs.
- Mutable session templates and template tombstones.
- Graph references held by sessions and templates, and the visibility decisions
  that govern which nodes, edges and sessions an operator may read.
- The backup artifact, which contains every session and mutable template.
- The integrity of the restored workspace and of any instance later switched
  to it, including the staged-versus-direct write mode of each session.
- The operator's offline snapshot and graph backup, which must not be modified.

## Actors

An operator runs [backup_sessions.py](../../../scripts/backup_sessions.py) and
[restore_sessions.py](../../../scripts/restore_sessions.py) with a trusted
authorization module and a trusted integration module. The supplied file
integrations are
[session_backup_file_snapshot.py](../../../scripts/session_backup_file_snapshot.py)
and [session_restore_file_snapshot.py](../../../scripts/session_restore_file_snapshot.py).
Session authors created the content that ends up in the artifact. An adversary
may be anyone who can read or replace the artifact where it is stored or
moved, an operator whose policy should not cover every session, or a session
author whose annotations or templates carry unsafe content.

## Trust boundaries

1. **Operator code into the commands.** `--integration` and
   `SESSION_BACKUP_AUTH_MODULE` are imported and executed with the operator's
   privileges. Both are trusted by design; nothing here sandboxes them.
2. **Offline snapshot into backup.** Session, template and graph files cross
   from a filesystem snapshot into the backup service. The snapshot must be
   immutable and taken with no writers active.
3. **Backup into the artifact.** All session data leaves the store as a single
   JSON document that is then stored and moved outside the application.
4. **Artifact into restore.** The document crosses back into the session and
   template stores. Its content, hashes, IDs, modes and references are
   attacker-controllable once the artifact leaves the operator's custody.
5. **Restored workspace into service.** The published directory becomes a
   candidate store; restored annotations are later rendered by browsers and
   restored modes decide whether graph writes are staged.

## Entry points

- `scripts/backup_sessions.py --integration <module> --output <path>`: imports
  the integration, calls `authorization_hook()` and `open_backup(hook)`, then
  [create_backup](../../../backend/service/session_backup.py) and writes the
  canonical document.
- `scripts/restore_sessions.py --integration <module> --input <path> --output <dir>`:
  reads and parses the artifact, calls `open_restore(hook, output)`, then
  [restore_backup](../../../backend/service/session_restore.py) and the
  integration's `publish()` callback, and prints a JSON reference report.
- Environment inputs to the file integrations: `SESSION_BACKUP_AUTH_MODULE`,
  `SESSION_BACKUP_GRAPH`, `SESSION_BACKUP_GRAPH_JOURNAL`,
  `SESSION_BACKUP_SESSIONS`, `SESSION_BACKUP_TEMPLATES` and `SCHEMA_FILE`.
- Artifact fields read on restore: `format_version`, `sessions` (each with
  `id`, `name`, `description`, `revision`, timestamps, `write_mode`,
  `write_mode_source`, `write_mode_mirror` and `state`), `assets`,
  `import_mappings`, `import_reports`, `templates`, `template_tombstones` and
  `integrity`.

## Threats (STRIDE-style)

- **Spoofing:** a forged or substituted artifact is presented as a genuine
  backup; an integration or authorization module on `PYTHONPATH` that is not
  the deployment's policy is loaded in its place.
- **Tampering:** an altered artifact injects session state, unsafe annotations
  or image data, templates that shadow profile templates, colliding IDs, or
  changed write modes; a restore overwrites live sessions or the graph; the
  snapshot changes while a backup is collected, giving a torn artifact.
- **Repudiation:** neither command keeps an audit record of who produced or
  restored an artifact, and restored sessions start with an empty activity log.
- **Information disclosure:** the artifact or reference report exposes sessions
  or graph IDs to someone not entitled to them; an operator backs up or
  restores sessions outside their policy; error messages or tracebacks reveal
  session IDs or record content before access is checked.
- **Denial of service:** an oversized or deeply nested artifact, or one with
  very many sessions, annotations or templates, exhausts memory or time; a
  restore fills the disk.
- **Elevation of privilege:** a restored session refers to nodes, edges or
  sessions the operator cannot read, or is switched from staged to direct mode
  so later writes bypass staging; a restored annotation carries a payload that
  executes or navigates when rendered.

## Mitigations with code references

- **Admin authorization first.** Both scripts call `_authorize` with
  `GRAPH_ACTION_ADMIN` for `backup_sessions` or `restore_sessions` before
  opening stores or reading the input, and
  [create_backup](../../../backend/service/session_backup.py) and
  [restore_backup](../../../backend/service/session_restore.py) repeat that
  check. `_authorize` fails closed when the hook raises. The
  [default hook](../../../backend/runtime/authorization.py) denies admin
  actions. Both scripts refuse an integration whose service does not use the
  same hook object.
- **Per-record authorization on backup.** `create_backup` authorizes every
  session and mutable template, re-authorizes after reading, and authorizes
  every referenced session, node and edge through
  [authorize_capture_refs](../../../backend/service/views.py); any denial
  refuses the whole artifact. The file integration's `open_backup` authorizes
  the full session list before loading staged layers.
- **Per-record authorization on restore.** `restore_backup` authorizes every
  session, captured session reference, node, edge, template and tombstone ID in
  the document before validation and before any write.
- **Consistent snapshot.** `create_backup` requires a `backup_snapshot()` on the
  write modes, compares session and template records read before and inside the
  transaction, and refuses changed lists, revision conflicts, retired or
  unreadable staged records and unmerged staged layers. `open_backup` copies
  the snapshot to a private directory, refuses an incomplete journal or merge
  log, and `SnapshotGraphBackend._cut_journal_to` refuses journal recovery.
- **Artifact handling.** `backup_sessions.py` creates the output with
  `O_CREAT | O_EXCL` and mode `0o600`, fsyncs it, and removes it on failure.
  `canonical_bytes` gives the deterministic encoding the integrity hash covers.
- **Input limits.** `restore_sessions.py` refuses inputs over
  `MAX_BACKUP_BYTES` (512 MiB) before parsing. `restore_backup` caps sessions,
  annotations per session, templates and tombstones (10,000, 2,000, 10,000 and
  10,000 by default), and per-record size at
  `DEFAULT_MAX_SESSION_DOCUMENT_BYTES`; `OpBatchTooLarge` is refused.
- **Strict document validation.** `restore_backup` accepts only integer format
  versions 1 and 2, recomputes `integrity.sha256`, requires the exact top-level
  and per-session field sets, refuses nonempty import tables, validates IDs with
  `is_valid_session_id`, revisions, names, descriptions, timestamps and modes,
  and refuses a mirror that records staged mode for a session restored as
  direct. `_validate_reference_fields` and `_validate_state_values` check state
  values; templates pass [validate_template](../../../backend/core/template_store.py).
- **Annotation and asset safety.** Restore applies
  [validate_captured_annotations](../../../backend/core/session_manager.py) and
  `_require_ingested_image` and `_require_safe_reference_target` from
  [session_store.py](../../../backend/core/session_store.py) to every
  annotation, then rebuilds the asset table with `_assets` and requires it to
  equal the document's `assets`.
- **No overwrite.** Inside the store transactions `restore_backup` refuses any
  existing session or template, template IDs that collide with profile
  templates, restored sessions or staged records, and staged records other than
  active ones for restored staged sessions. `open_restore` writes only to a
  fresh temporary directory beside the output, and `_publish` uses Linux
  `renameat2` with no-replace so an existing output is never replaced.
- **Error hygiene.** Refusals use fixed `BackupRefused` and `RestoreRefused`
  reasons and suppress the original exception (`from None`), so record content
  and session IDs are not chained into tracebacks; until restore begins,
  `restore_sessions.py` reports any failure only as `access denied`.
- Behaviour is exercised by
  [test_session_backup.py](../../../backend/service/tests/test_session_backup.py)
  and [test_session_restore.py](../../../backend/service/tests/test_session_restore.py).

## Residual risk

- The SHA-256 hashes detect accidental corruption only. Anyone who can modify
  the artifact can recompute them, so restore cannot tell a genuine backup from
  a forged one. Confidentiality and authenticity of the artifact at rest and in
  transit depend on the operator's storage; the artifact is not encrypted.
- Within the validated value ranges the document is trusted: a modified
  artifact can choose a session's `write_mode` (for example direct instead of
  staged), names, revisions, timestamps and permitted state. The required
  verification in a separate offline instance is the control for this.
- Authorization covers what the operator's policy allows, not what the original
  author intended. A policy that grants admin and permissive reads covers every
  session in the snapshot. Restore
  authorizes against the paired graph; references missing from it are reported,
  not refused, and the report is session data the operator must protect.
- The integration and authorization modules run unsandboxed with the operator's
  privileges. A malicious or misconfigured module defeats every control here.
- Consistency depends on the operator: snapshots taken with active writers are
  unsupported, and the commands cannot detect every such snapshot. A custom
  journal path must be set by hand to match the backend configuration. No
  PostgreSQL integration is supplied; another backend's integration must provide
  its own coordinated snapshot and atomic publish.
- The 512 MiB input limit still allows a much larger in-memory parse, and the
  whole backup, copied graph and restored data must fit in memory and on disk.
  Legacy image URLs that fail current safety rules are backed up but refuse
  restore until corrected. Interrupted restores can leave a temporary directory
  for the operator to remove.
- Neither command records an audit event, and restored sessions carry no
  activity history; attribution of a backup or restore rests on the operator's
  own process records.
