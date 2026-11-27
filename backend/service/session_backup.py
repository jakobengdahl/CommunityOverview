"""Offline operator backup of the standalone session store.

The deployment must provide a layer snapshot that serializes with all staged
writes and does not perform recovery or other writes.
"""

import base64
import binascii
import copy
import hashlib
import json
from urllib.parse import unquote_to_bytes

from backend.core.session_annotations import annotation_type_of
from backend.core.session_store import Session, _empty_state, is_valid_session_id
from backend.core.template_store import validate_template
from backend.runtime import session_context
from backend.runtime.authorization import GRAPH_ACTION_ADMIN, GRAPH_ACTION_READ
from backend.service import access, session_capture, views


class BackupRefused(Exception):
    """The complete backup cannot be safely produced."""


def canonical_bytes(value):
    """Canonical UTF-8 encoding used by both integrity checks and consumers."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _assets(state, found):
    # Keep the original data URL in the state; the asset table makes each
    # embedded payload independently verifiable without rewriting the canvas.
    for annotation in state.get("annotations", []):
        if (
            not isinstance(annotation, dict)
            or annotation_type_of(annotation) != "image"
        ):
            continue
        image = annotation.get("image")
        if not isinstance(image, dict):
            continue
        value = image.get("url")
        if not isinstance(value, str) or value[:5].lower() != "data:":
            continue
        try:
            header, encoded = value.split(",", 1)
            data = unquote_to_bytes(encoded)
            if header.lower().endswith(";base64"):
                data = base64.b64decode(data, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise BackupRefused("invalid embedded asset") from exc
        digest = hashlib.sha256(data).hexdigest()
        found[digest] = {"sha256": digest, "data_url": value}


def _authorize(hook, *, target, session_id=None, action=GRAPH_ACTION_READ):
    try:
        denied = access.authorize_graph_access(
            hook, action=action, target=target, session_id=session_id
        )
    except Exception:
        raise BackupRefused("access denied") from None
    if denied:
        raise BackupRefused("access denied")


def _load_session(session_backend, session_id):
    try:
        return session_backend.load(session_id)
    except Exception:
        raise BackupRefused("session_load_failed") from None


def create_backup(service, session_backend, write_modes, *, template_backend=None):
    """Read every persisted session under the deployment layer snapshot lock.

    The caller supplies its configured access hook and graph. Authorization is
    fail-closed for each session and every stored graph reference. No result is
    returned until the entire snapshot has passed all checks.
    """
    sessions = []
    assets = {}
    captured = []
    with session_context.bind_session(None):
        _authorize(
            service.authorization_hook,
            target="backup_sessions",
            action=GRAPH_ACTION_ADMIN,
        )
        # A session transaction alone does not order staged writes. Only a
        # deployment-provided snapshot that coordinates with every layer writer
        # can supply this guarantee. The snapshot yields a read-only WriteModes.
        snapshot = getattr(write_modes, "backup_snapshot", None)
        if not callable(snapshot):
            raise BackupRefused("layer snapshot unavailable")
        session_ids = sorted(session_backend.list_ids())
        for session_id in session_ids:
            _authorize(
                service.authorization_hook,
                target="get_visualization_session",
                session_id=session_id,
            )
        revisions = {}
        for session_id in session_ids:
            raw = _load_session(session_backend, session_id)
            revisions[session_id] = copy.deepcopy(raw)
        # Bind authorization to the records just read. A change after this
        # pass is caught by the comparison inside the transaction; hooks may
        # themselves read the store, so they must run outside its lock.
        for session_id in session_ids:
            _authorize(
                service.authorization_hook,
                target="get_visualization_session",
                session_id=session_id,
            )
        if template_backend is None:
            raise BackupRefused("template backend unavailable")
        templates = []
        tombstones = []
        if template_backend is not None:
            ids = sorted(template_backend.list_ids())
            for template_id in ids:
                _authorize(
                    service.authorization_hook,
                    target="get_visualization_session",
                    session_id=template_id,
                )
            try:
                originals = {
                    key: copy.deepcopy(template_backend.load(key)) for key in ids
                }
            except Exception:
                raise BackupRefused("template load failed") from None
            for template_id in ids:
                _authorize(
                    service.authorization_hook,
                    target="get_visualization_session",
                    session_id=template_id,
                )
        with session_backend.transaction():
            if sorted(session_backend.list_ids()) != session_ids:
                raise BackupRefused("session list changed during backup")
            with snapshot() as snapshot_modes:
                for session_id in session_ids:
                    raw = _load_session(session_backend, session_id)
                    if raw != revisions[session_id]:
                        raise BackupRefused("revision_conflict")
                    if raw is None or raw.get("id") != session_id:
                        raise BackupRefused("invalid session record")
                    session = Session.from_dict(raw)
                    status = snapshot_modes.staged_status(session_id)
                    if (
                        status == "retired"
                        or snapshot_modes.store_holds_unreadable_records()
                    ):
                        raise BackupRefused("unreadable session mode")
                    mode = snapshot_modes.mode_of(session_id)
                    if session.write_mode == "staged" and mode != "staged":
                        raise BackupRefused("unreadable session mode")
                    if mode == "staged" and snapshot_modes.layer_store.load_layer(
                        session_id
                    ):
                        raise BackupRefused("unmerged staged changes")
                    state = raw.get("state")
                    if (
                        not isinstance(state, dict)
                        or state.keys() - _empty_state().keys()
                    ):
                        raise BackupRefused("invalid session state")
                    captured.append((session_id, copy.deepcopy(raw), mode))
                if template_backend is not None:
                    try:
                        with template_backend.transaction():
                            if sorted(template_backend.list_ids()) != ids:
                                raise BackupRefused("template list changed")
                            for key in ids:
                                record = template_backend.load(key)
                                if (
                                    record != originals[key]
                                    or not isinstance(record, dict)
                                    or record.get("id") != key
                                ):
                                    raise BackupRefused("invalid template record")
                                if record.get("deleted"):
                                    if (
                                        set(record) != {"id", "deleted", "revision"}
                                        or record["deleted"] is not True
                                        or not is_valid_session_id(key)
                                        or type(record["revision"]) is not int
                                        or record["revision"] < 1
                                    ):
                                        raise BackupRefused("invalid template record")
                                    tombstones.append(copy.deepcopy(record))
                                    continue
                                doc = validate_template(record["document"])
                                if doc["template_id"] != key:
                                    raise BackupRefused("invalid template record")
                                templates.append(doc)
                    except BackupRefused:
                        raise
                    except Exception:
                        raise BackupRefused("invalid template record") from None
        # Reference authorization can consult SessionStore and must not run
        # under either persistence lock. The collected records are detached.
        for doc in templates:
            try:
                source_id = doc["source_session_id"]
                _authorize(
                    service.authorization_hook,
                    target="get_visualization_session",
                    session_id=source_id,
                )
                with session_context.bind_session(source_id):
                    nodes = list(
                        dict.fromkeys(n["source_node_id"] for n in doc["nodes"])
                    )
                    edges = list(
                        dict.fromkeys(e["source_edge_id"] for e in doc["edges"])
                    )
                    if hasattr(service, "_storage"):
                        result = views.authorize_capture_refs(
                            service._storage,
                            service.authorization_hook,
                            nodes,
                            edges,
                            target="backup_sessions",
                        )
                    else:
                        result = service.authorize_capture_refs(
                            nodes, edges, target="backup_sessions"
                        )
                if not result.get("success"):
                    raise ValueError()
            except Exception:
                raise BackupRefused("access denied") from None
        for session_id, raw, mode in captured:
            session = Session.from_dict(raw)
            state = raw["state"]
            try:
                node_ids = session_capture.backup_node_ids(state)
                for target_id in session_capture.captured_session_ids(state):
                    _authorize(
                        service.authorization_hook,
                        target="get_visualization_session",
                        session_id=target_id,
                    )
                with session_context.bind_session(session_id):
                    result = service.authorize_capture_refs(
                        list(dict.fromkeys(node_ids)),
                        session_capture.captured_edge_ids(state),
                        target="backup_sessions",
                    )
            except Exception:
                raise BackupRefused("access denied") from None
            if not result.get("success"):
                raise BackupRefused("access denied")
            _assets(state, assets)
            sessions.append(
                {
                    "id": session.id,
                    "name": session.name,
                    "description": session.description,
                    "revision": session.seq,
                    "created_at": session.created_at,
                    "updated_at": session.updated_at,
                    "write_mode": mode,
                    "write_mode_source": (
                        "explicit"
                        if mode != session.write_mode
                        else session.write_mode_source
                    ),
                    "write_mode_mirror": {
                        key: raw[key]
                        for key in ("write_mode", "write_mode_source")
                        if key in raw
                    },
                    "state": state,
                }
            )
    document = {
        "format_version": 1,
        "sessions": sessions,
        "assets": [assets[key] for key in sorted(assets)],
        "import_mappings": [],
        "import_reports": [],
    }
    if template_backend is not None:
        document["format_version"] = 2
        document["templates"] = templates
        if tombstones:
            document["template_tombstones"] = tombstones
    document["integrity"] = {
        "algorithm": "sha256",
        "sha256": hashlib.sha256(canonical_bytes(document)).hexdigest(),
    }
    return document
