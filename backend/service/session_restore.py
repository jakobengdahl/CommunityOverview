"""Validate and restore version 1 session backups into an offline workspace."""

import copy
import hashlib
import json
import math

from backend.config.node_shapes import ShapeSelection
from backend.core.image_ingest import DEFAULT_MAX_SESSION_DOCUMENT_BYTES
from backend.core.session_manager import OpBatchTooLarge, validate_captured_annotations
from backend.core.session_store import (
    MAX_SESSION_DESCRIPTION_CHARS,
    _empty_state,
    _require_ingested_image,
    _require_safe_reference_target,
    _validate_intensity,
    _validate_position,
    is_valid_session_id,
)
from backend.core.template_store import validate_template
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


MAX_BACKUP_BYTES = 512 * 1024 * 1024


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


def _validate_state_values(state):
    for position in state["positions"].values():
        validated = _validate_position(position)
        if not all(math.isfinite(v) for v in validated.values()):
            raise ValueError()
    intensity = state["edge_intensity"]
    if not math.isfinite(_validate_intensity(intensity)) or not 0 <= intensity <= 1:
        raise ValueError()
    if (
        type(state["annotation_schema_version"]) is not int
        or state["annotation_schema_version"] != 1
    ):
        raise ValueError()
    for field in ("shape_overrides", "type_shapes"):
        mapping = state[field]
        if not isinstance(mapping, dict):
            raise ValueError()
        for key, selection in mapping.items():
            if not isinstance(key, str) or not key or len(key) > 256:
                raise ValueError()
            ShapeSelection.model_validate(selection)
    if any(type(v) is not int or v < 0 for v in state["shape_revisions"].values()):
        raise ValueError()
    receipts = state["shape_operation_ids"]
    if not isinstance(receipts, dict):
        raise ValueError()
    for key, receipt in receipts.items():
        if (
            not isinstance(key, str)
            or not isinstance(receipt, dict)
            or set(receipt) != {"seq", "client_id"}
            or type(receipt["seq"]) is not int
            or receipt["seq"] < 0
            or not isinstance(receipt["client_id"], (str, type(None)))
        ):
            raise ValueError()


def restore_backup(
    service,
    backend,
    modes,
    document,
    *,
    template_backend=None,
    profile_template_ids=(),
    max_sessions=10000,
    max_annotations=2000,
):
    """Check limits, authorize references, then validate and restore to an isolated target."""
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
            if len(sessions) > max_sessions:
                raise ValueError()
            for item in sessions:
                state = item["state"]
                annotations = state.get("annotations", [])
                if (
                    not isinstance(annotations, list)
                    or len(annotations) > max_annotations
                ):
                    raise ValueError()
                if (
                    len(json.dumps(item).encode("utf-8"))
                    > DEFAULT_MAX_SESSION_DOCUMENT_BYTES
                ):
                    raise ValueError()
            # Preserve all existing session checks before authorizing templates.
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
            if document.get("format_version") == 2:
                for template in document["templates"]:
                    _authorize(
                        service.authorization_hook,
                        target="get_visualization_session",
                        session_id=template["template_id"],
                    )
        except Exception:
            raise RestoreRefused("access denied") from None

        try:
            if (
                document.get("format_version") not in (1, 2)
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
            expected_fields = {
                "format_version",
                "sessions",
                "assets",
                "import_mappings",
                "import_reports",
                "integrity",
            }
            if document["format_version"] == 2:
                expected_fields.add("templates")
            if set(document) != expected_fields:
                raise RestoreRefused("invalid format")
            if document["import_mappings"] != [] or document["import_reports"] != []:
                raise RestoreRefused("unsupported mappings")
            templates = []
            if document["format_version"] == 2:
                if template_backend is None or not isinstance(
                    document["templates"], list
                ):
                    raise RestoreRefused("invalid templates")
                template_ids = set(
                    profile_template_ids()
                    if callable(profile_template_ids)
                    else profile_template_ids
                )
                for item in document["templates"]:
                    doc = validate_template(item)
                    if doc["template_id"] in template_ids:
                        raise RestoreRefused("template collision")
                    template_ids.add(doc["template_id"])
                    templates.append(doc)
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
                if (
                    mirror.get("write_mode") == "staged"
                    and item["write_mode"] != "staged"
                ):
                    raise RestoreRefused("invalid mode")
                full_state = _empty_state()
                full_state.update(state)
                _validate_state_values(full_state)
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
        from contextlib import nullcontext

        with (
            backend.transaction(),
            template_backend.transaction() if template_backend else nullcontext(),
        ):
            if (
                (template_backend is not None and template_backend.list_ids())
                or backend.list_ids()
                or any(
                    modes.staged_status(r["id"])
                    not in (("none", "active") if mode == "staged" else ("none",))
                    for r, mode in records
                )
            ):
                raise RestoreRefused("target collision")
            for doc in templates:
                template_backend.save({"id": doc["template_id"], "document": doc})
            for record, mode in records:
                if mode == "staged":
                    modes.record_staged(record["id"])
                backend.save(record)
        return reports
