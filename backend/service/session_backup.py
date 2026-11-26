"""Offline operator backup of the standalone session store.

The caller must stop all writers to the graph and sessions before opening either
store. File graph layers are process-local mirrors, so locking session files
alone cannot provide a consistent view of a running deployment.
"""

import base64
import binascii
import hashlib
import json

from backend.core.session_store import Session, _empty_state
from backend.runtime import session_context
from backend.runtime.authorization import GRAPH_ACTION_READ
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


def create_backup(service, session_backend, write_modes):
    """Read every persisted session while the deployment is stopped.

    The caller supplies its configured access hook and graph. Authorization is
    fail-closed for each session and every stored graph reference. No result is
    returned until the entire snapshot has passed all checks.
    """
    sessions = []
    assets = {}
    with session_context.bind_session(None), session_backend.transaction():
        if access.authorize_graph_access(
            service.authorization_hook,
            action=GRAPH_ACTION_READ,
            target="backup_sessions",
        ):
            raise BackupRefused("access denied")
        for session_id in sorted(session_backend.list_ids()):
            if access.authorize_graph_access(
                service.authorization_hook,
                action=GRAPH_ACTION_READ,
                target="get_visualization_session",
                session_id=session_id,
            ):
                raise BackupRefused("access denied")
            raw = session_backend.load(session_id)
            if raw is None or raw.get("id") != session_id:
                raise BackupRefused("invalid session record")
            session = Session.from_dict(raw)
            status = write_modes.staged_status(session_id)
            if status == "retired" or write_modes.store_holds_unreadable_records():
                raise BackupRefused("unreadable session mode")
            mode = write_modes.mode_of(session_id)
            if session.write_mode == "staged" and mode != "staged":
                raise BackupRefused("unreadable session mode")
            if mode == "staged" and write_modes.layer_store.load_layer(session_id):
                raise BackupRefused("unmerged staged changes")
            state = raw.get("state")
            if not isinstance(state, dict) or state.keys() - _empty_state().keys():
                raise BackupRefused("invalid session state")
            node_ids = session_capture.captured_node_ids(state)
            # Revisions can outlive membership, but still disclose node IDs.
            node_ids.extend(
                key[5:]
                for key in state.get("shape_revisions", {})
                if isinstance(key, str) and key.startswith("node:")
            )
            result = service.authorize_capture_refs(
                list(dict.fromkeys(node_ids)), session_capture.captured_edge_ids(state)
            )
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
