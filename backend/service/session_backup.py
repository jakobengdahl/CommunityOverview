"""Offline operator backup of the standalone session store.

The deployment must provide a layer snapshot that serializes with all staged
writes and does not perform recovery or other writes.
"""

import base64
import binascii
import hashlib
import json

from backend.core.session_store import Session, _empty_state
from backend.runtime import session_context
from backend.runtime.authorization import GRAPH_ACTION_ADMIN, GRAPH_ACTION_READ
from backend.service import access, session_capture


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


def _assets(value, found):
    # Keep the original data URL in the state; the asset table makes each
    # embedded payload independently verifiable without rewriting the canvas.
    if isinstance(value, str) and value.startswith("data:image/"):
        try:
            header, encoded = value.split(",", 1)
            if not header.endswith(";base64"):
                raise ValueError()
            data = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise BackupRefused("invalid embedded asset") from exc
        digest = hashlib.sha256(data).hexdigest()
        found[digest] = {"sha256": digest, "data_url": value}
    elif isinstance(value, dict):
        for child in value.values():
            _assets(child, found)
    elif isinstance(value, list):
        for child in value:
            _assets(child, found)


def _authorize(hook, *, target, session_id=None, action=GRAPH_ACTION_READ):
    try:
        denied = access.authorize_graph_access(
            hook, action=action, target=target, session_id=session_id
        )
    except Exception:
        raise BackupRefused("access denied") from None
    if denied:
        raise BackupRefused("access denied")


def create_backup(service, session_backend, write_modes):
    """Read every persisted session under the deployment layer snapshot lock.

    The caller supplies its configured access hook and graph. Authorization is
    fail-closed for each session and every stored graph reference. No result is
    returned until the entire snapshot has passed all checks.
    """
    sessions = []
    assets = {}
    with session_context.bind_session(None):
        _authorize(
            service.authorization_hook, target="backup_sessions", action=GRAPH_ACTION_ADMIN
        )
        # A session transaction alone does not order staged writes. Only a
        # deployment-provided snapshot that coordinates with every layer writer
        # can supply this guarantee. The snapshot yields a read-only WriteModes.
        snapshot = getattr(write_modes, "backup_snapshot", None)
        if not callable(snapshot):
            raise BackupRefused("layer snapshot unavailable")
        with session_backend.transaction(), snapshot() as snapshot_modes:
            for session_id in sorted(session_backend.list_ids()):
                _authorize(
                    service.authorization_hook,
                    target="get_visualization_session",
                    session_id=session_id,
                )
                raw = session_backend.load(session_id)
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
                if not isinstance(state, dict) or state.keys() - _empty_state().keys():
                    raise BackupRefused("invalid session state")
                node_ids = session_capture.captured_node_ids(state)
                # Explicit membership always names graph nodes, even when an ID
                # happens to share the legacy local-group prefix.
                for field in ("node_refs", "hidden_node_ids", "dimmed_node_ids"):
                    node_ids.extend(i for i in state.get(field, []) if isinstance(i, str))
                local_groups = {
                    key for field in ("positions", "shape_overrides")
                    for key in state.get(field, {}) if key.startswith("group-")
                } - set(node_ids)
                # Revisions can outlive membership, but still disclose node IDs.
                node_ids.extend(
                    key[5:]
                    for key in state.get("shape_revisions", {})
                    if isinstance(key, str) and key.startswith("node:")
                    and key[5:] not in local_groups
                )
                try:
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
    document["integrity"] = {
        "algorithm": "sha256",
        "sha256": hashlib.sha256(canonical_bytes(document)).hexdigest(),
    }
    return document
