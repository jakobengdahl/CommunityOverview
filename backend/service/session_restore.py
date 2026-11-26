"""Validate and restore version 1 session backups into an offline workspace."""

import copy
import hashlib
import json

from backend.core.image_ingest import DEFAULT_MAX_SESSION_DOCUMENT_BYTES
from backend.core.session_manager import OpBatchTooLarge, validate_captured_annotations
from backend.core.session_store import (
    MAX_SESSION_DESCRIPTION_CHARS,
    _empty_state,
    _require_ingested_image,
    _require_safe_reference_target,
    is_valid_session_id,
)
from backend.runtime import session_context
from backend.runtime.authorization import GRAPH_ACTION_ADMIN
from backend.service import session_capture, views
from backend.service.session_backup import (
    _assets,
    _authorize,
    canonical_bytes,
)


class RestoreRefused(Exception):
    """The backup could not be restored safely."""


def _validate_reference_fields(state):
    # The capture helpers deliberately ignore non-string IDs. Reject them
    # before calling those helpers so no persisted reference bypasses access.
    if not isinstance(state, dict):
        raise ValueError()
    for key in (
        "node_refs",
        "hidden_node_ids",
        "dimmed_node_ids",
        "hidden_edge_ids",
        "dimmed_edge_ids",
    ):
        value = state.get(key, [])
        if not isinstance(value, list) or any(not isinstance(i, str) for i in value):
            raise ValueError()
    for key in ("positions", "shape_overrides", "shape_revisions"):
        value = state.get(key, {})
        if not isinstance(value, dict) or any(not isinstance(i, str) for i in value):
            raise ValueError()


def restore_backup(
    service, backend, modes, document, *, max_sessions=10000, max_annotations=2000
):
    """Authorize all references before validation or publishing to an isolated target."""
    with session_context.bind_session(None):
        try:
            _authorize(
                service.authorization_hook,
                target="restore_sessions",
                action=GRAPH_ACTION_ADMIN,
            )
            sessions = document["sessions"]
            if not isinstance(sessions, list):
                raise ValueError()
            reports = []
            for item in sessions:
                sid, state = item["id"], item["state"]
                _validate_reference_fields(state)
                _authorize(
                    service.authorization_hook,
                    target="get_visualization_session",
                    session_id=sid,
                )
                for target in session_capture.captured_session_ids(state):
                    _authorize(
                        service.authorization_hook,
                        target="get_visualization_session",
                        session_id=target,
                    )
                with session_context.bind_session(sid):
                    nodes = session_capture.backup_node_ids(state)
                    edges = session_capture.captured_edge_ids(state)
                    if hasattr(service, "_storage"):
                        result = views.authorize_capture_refs(
                            service._storage,
                            service.authorization_hook,
                            nodes,
                            edges,
                            target="restore_sessions",
                        )
                    else:
                        result = service.authorize_capture_refs(
                            nodes, edges, target="restore_sessions"
                        )
                if not result.get("success"):
                    raise ValueError()
                reports.append(
                    {
                        "session_id": sid,
                        "missing_node_ids": result["missing_node_ids"],
                        "missing_edge_ids": (
                            [
                                edge
                                for edge in edges
                                if edge not in service._storage.edges
                            ]
                            if hasattr(service, "_storage")
                            else result.get("missing_edge_ids", [])
                        ),
                    }
                )
        except Exception:
            raise RestoreRefused("access denied") from None

        try:
            if (
                document.get("format_version") != 1
                or type(document.get("format_version")) is not int
            ):
                raise RestoreRefused("unsupported version")
            integrity = document["integrity"]
            unsigned = {k: v for k, v in document.items() if k != "integrity"}
            if integrity != {
                "algorithm": "sha256",
                "sha256": hashlib.sha256(canonical_bytes(unsigned)).hexdigest(),
            }:
                raise RestoreRefused("integrity failure")
            if set(document) != {
                "format_version",
                "sessions",
                "assets",
                "import_mappings",
                "import_reports",
                "integrity",
            }:
                raise RestoreRefused("invalid format")
            if document["import_mappings"] != [] or document["import_reports"] != []:
                raise RestoreRefused("unsupported mappings")
            if len(sessions) > max_sessions:
                raise RestoreRefused("session limit exceeded")
            assets, records, seen = {}, [], set()
            for item in sessions:
                if set(item) != {
                    "id",
                    "name",
                    "description",
                    "revision",
                    "created_at",
                    "updated_at",
                    "write_mode",
                    "write_mode_source",
                    "write_mode_mirror",
                    "state",
                }:
                    raise RestoreRefused("invalid record")
                sid, state = item["id"], item["state"]
                if not is_valid_session_id(sid) or sid in seen:
                    raise RestoreRefused("session collision")
                seen.add(sid)
                if not isinstance(state, dict) or state.keys() - _empty_state().keys():
                    raise RestoreRefused("invalid state")
                if (
                    type(item["revision"]) is not int
                    or item["revision"] < 0
                    or item["write_mode"] not in ("direct", "staged")
                    or item["write_mode_source"] not in ("inherited", "explicit")
                    or not isinstance(item["name"], (str, type(None)))
                    or (item["name"] is not None and len(item["name"]) > 200)
                    or not isinstance(item["description"], (str, type(None)))
                    or (
                        item["description"] is not None
                        and len(item["description"]) > MAX_SESSION_DESCRIPTION_CHARS
                    )
                    or not all(
                        isinstance(item[k], str) and item[k]
                        for k in ("created_at", "updated_at")
                    )
                ):
                    raise RestoreRefused("invalid record")
                mirror = item["write_mode_mirror"]
                if (
                    not isinstance(mirror, dict)
                    or mirror.keys() - {"write_mode", "write_mode_source"}
                    or any(
                        v
                        not in (
                            ("direct", "staged")
                            if k == "write_mode"
                            else ("inherited", "explicit")
                        )
                        for k, v in mirror.items()
                    )
                ):
                    raise RestoreRefused("invalid mode")
                full_state = _empty_state()
                full_state.update(state)
                validate_captured_annotations(full_state, max_annotations)
                for annotation in full_state["annotations"]:
                    _require_ingested_image(annotation, None)
                    _require_safe_reference_target(annotation, None)
                _assets(state, assets)
                record = {
                    "id": sid,
                    "name": item["name"],
                    "description": item["description"],
                    "seq": item["revision"],
                    "created_at": item["created_at"],
                    "updated_at": item["updated_at"],
                    "state": copy.deepcopy(state),
                    "activity_log": [],
                    **mirror,
                }
                if len(json.dumps(record)) > DEFAULT_MAX_SESSION_DOCUMENT_BYTES:
                    raise RestoreRefused("document limit exceeded")
                records.append((record, item["write_mode"]))
            if document["assets"] != [assets[k] for k in sorted(assets)]:
                raise RestoreRefused("asset integrity failure")
        except RestoreRefused:
            raise
        except OpBatchTooLarge:
            raise RestoreRefused("session limit exceeded") from None
        except Exception:
            raise RestoreRefused("invalid or unsafe backup") from None
        # The caller owns an isolated workspace and publishes it only after this
        # completes. Discarding that workspace is the rollback for every failure.
        with backend.transaction():
            if backend.list_ids() or any(
                modes.staged_status(r["id"])
                not in (("none", "active") if mode == "staged" else ("none",))
                for r, mode in records
            ):
                raise RestoreRefused("target collision")
            for record, mode in records:
                if mode == "staged":
                    modes.record_staged(record["id"])
                backend.save(record)
        return reports
