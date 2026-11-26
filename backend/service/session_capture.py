"""``save_session_view``: capture a session's presentation as a new session.

The MCP tool and the REST route both answer through ``save_session_view`` here,
so the same input gives the same result on either surface
(docs/SESSION_SAVEDVIEW_COMPAT_CONTRACT.md, "Identity and capture").
"""

import logging
from typing import Any, Callable, Collection, Dict, Iterator, List, Optional, Tuple

from backend.config.config_loader import build_session_url
from backend.core.session_annotations import annotation_type_of
from backend.core.session_manager import (
    CaptureRefused,
    LayoutBusy,
    OpBatchTooLarge,
    RevisionConflict,
    SessionLimitReached,
    SessionNotFound,
)
from backend.core.session_store import is_valid_session_id
from backend.runtime import session_context
from backend.runtime.authorization import GRAPH_ACTION_MUTATE, GRAPH_ACTION_READ

from . import access
from .staging import HookFailed

logger = logging.getLogger(__name__)

INVALID_SESSION_ID_MESSAGE = (
    "Invalid session ID format — expected DDDD-DDDD-DDDD-DDDD "
    "(the older DDDD-DDDD form is also accepted)"
)
TARGET = "save_session_view"
MAX_NAME_CHARS = 200

# Every error the operation answers with, and its REST status.
ERROR_STATUS = {
    "invalid_request": 400,
    "access_denied": 403,
    "session_not_found": 404,
    "revision_conflict": 409,
    "session_busy": 409,
    "staged_changes": 409,
    "invalid_presentation": 422,
    "session_too_large": 413,
    "too_many_sessions": 503,
    "storage_error": 503,
}

_REFUSALS = {
    SessionNotFound: "session_not_found",
    LayoutBusy: "session_busy",
    OpBatchTooLarge: "session_too_large",
    SessionLimitReached: "too_many_sessions",
}


class _Denied(Exception):
    pass


def _refused(code: str, **extra: Any) -> Dict[str, Any]:
    return {"success": False, "error": code, **extra}


# Before groups became annotations, the canvas stored them as nodes keyed
# "group-<n>"; such a canvas key names no graph node.
_LEGACY_GROUP_PREFIX = "group-"
# Top-level fields the canvas resolves by id alone, as a node or an annotation.
_UNTYPED_LINKS = ("startAnchor", "endAnchor", "parentId", "parent_id", "parentNode")


def _annotation_ids(state: Dict[str, Any]) -> set:
    return {
        a["id"]
        for a in state.get("annotations") or []
        if isinstance(a, dict) and isinstance(a.get("id"), str)
    }


def _annotation_links(
    annotation: Dict[str, Any], annotation_ids: Collection[str]
) -> Iterator[Tuple[Any, Any, str, str]]:
    """``(holder, key, id, kind)`` for every id ``holder[key]`` an annotation
    links to, ``kind`` being ``"node"`` or ``"annotation"``.

    Only the explicit link fields of the annotation write schema are read: a
    resource reference's ``target``, an ``attachment`` (top level or a line's
    ``start``/``end``), the untyped canvas links and group members. An id
    counts as an annotation only when the source stores an annotation under
    it and the field does not declare it a node; free-form payload is never
    read, and an annotation-typed link to no stored annotation is skipped.
    """
    fields: List[Tuple[Any, Any, Optional[str]]] = []
    if (
        annotation_type_of(annotation) == "reference"
        and annotation.get("target_kind") == "resource"
    ):
        fields.append((annotation, "target", "node"))
    for endpoint in (annotation, annotation.get("start"), annotation.get("end")):
        attachment = endpoint.get("attachment") if isinstance(endpoint, dict) else None
        if isinstance(attachment, dict):
            fields.append((attachment, "target_id", attachment.get("target_type")))
    fields.extend((annotation, key, None) for key in _UNTYPED_LINKS)
    members = annotation.get("member_node_ids")
    if isinstance(members, list):
        fields.extend((members, i, None) for i in range(len(members)))
    for holder, key, declared in fields:
        value = holder[key] if isinstance(holder, list) else holder.get(key)
        if not isinstance(value, str):
            continue
        if declared in (None, "annotation") and value in annotation_ids:
            yield holder, key, value, "annotation"
        elif declared != "annotation":
            yield holder, key, value, "node"


def captured_node_ids(state: Dict[str, Any]) -> List[str]:
    """Every graph node id the captured canvas names, first use first.

    ``shape_revisions`` keys are not read: they outlive a node's removal from
    the canvas, so a removed node would be checked and reported missing.
    Position keys always name nodes; a shape override keyed by an annotation
    id belongs to that annotation.
    """
    annotation_ids = _annotation_ids(state)
    ids: Dict[str, None] = {}
    for key in ("node_refs", "hidden_node_ids", "dimmed_node_ids", "positions"):
        ids.update(dict.fromkeys(state.get(key) or []))
    ids.update(
        dict.fromkeys(
            i for i in (state.get("shape_overrides") or {}) if i not in annotation_ids
        )
    )
    canvas = [
        i for i in ids if isinstance(i, str) and not i.startswith(_LEGACY_GROUP_PREFIX)
    ]
    linked = [
        value
        for annotation in state.get("annotations") or []
        if isinstance(annotation, dict)
        for _, _, value, kind in _annotation_links(annotation, annotation_ids)
        if kind == "node"
    ]
    return list(dict.fromkeys(canvas + linked))


def relink_capture(state: Dict[str, Any], fresh_ids: Dict[str, str]) -> None:
    """Point every annotation link and shape override that names a copied
    annotation at its fresh id (``fresh_ids``: source id to copy id), and
    drop group members that are graph nodes off the captured canvas."""
    on_canvas = {i for i in state.get("node_refs") or [] if isinstance(i, str)}
    for annotation in state["annotations"]:
        links = list(_annotation_links(annotation, fresh_ids))
        for holder, key, value, kind in links:
            if kind == "annotation":
                holder[key] = fresh_ids[value]
        members = annotation.get("member_node_ids")
        if isinstance(members, list):
            graph = {i for h, i, _, kind in links if h is members and kind == "node"}
            annotation["member_node_ids"] = [
                m for i, m in enumerate(members) if i not in graph or m in on_canvas
            ]
    state["shape_overrides"] = {
        fresh_ids.get(k, k): v for k, v in (state.get("shape_overrides") or {}).items()
    }


def captured_edge_ids(state: Dict[str, Any]) -> List[str]:
    """Every graph edge id the captured presentation names, first use first."""
    ids: Dict[str, None] = {}
    for key in ("hidden_edge_ids", "dimmed_edge_ids"):
        ids.update(dict.fromkeys(state.get(key) or []))
    return [i for i in ids if isinstance(i, str)]


def _hook_call(callback: Callable[..., Any]) -> Callable[..., Any]:
    """``callback`` with anything it raises re-raised as HookFailed: a hook
    that could not answer has not authorized the capture."""

    def call(*args: Any, **kwargs: Any) -> Any:
        try:
            return callback(*args, **kwargs)
        except Exception as exc:
            raise HookFailed() from exc

    return call


def _valid(name: Any, expected_revision: Any) -> bool:
    return (
        isinstance(name, str)
        and bool(name.strip())
        and len(name) <= MAX_NAME_CHARS
        and type(expected_revision) is int
        and expected_revision >= 0
    )


def save_session_view(
    service: Any,
    session_manager: Any,
    session_id: Any,
    name: Any,
    expected_revision: Any,
) -> Dict[str, Any]:
    """Save ``session_id`` at ``expected_revision`` as a new session.

    Arguments arrive raw from either surface and are validated here. Errors
    carry only a code from ``ERROR_STATUS`` (plus ``current_revision`` on a
    conflict, after the source read was authorized, and the fixed id-format
    hint for a malformed session id): never hook reasons,
    exception text, referenced ids or the destination id.
    """
    if session_manager is None:
        return _refused("storage_error")
    if not isinstance(session_id, str) or not is_valid_session_id(session_id):
        return _refused("invalid_request", message=INVALID_SESSION_ID_MESSAGE)
    if not _valid(name, expected_revision):
        return _refused("invalid_request")
    hook = service.authorization_hook
    authorize = _hook_call(access.authorize_graph_access)

    def authorize_capture(state: Dict[str, Any]) -> List[str]:
        result = service.authorize_capture_refs(
            captured_node_ids(state), captured_edge_ids(state)
        )
        if not result.get("success"):
            raise _Denied()
        return result["missing_node_ids"]

    def assign_access(copy_id: str) -> None:
        if access.assign_copied_session_access(
            hook,
            source_session_id=session_id,
            copy_session_id=copy_id,
            target=TARGET,
        ):
            raise _Denied()

    def revoke_access(copy_id: str) -> None:
        # The assignment failure is what the caller is answered with; a revoke
        # that also fails must not replace it.
        try:
            access.revoke_copied_session_access(
                hook,
                source_session_id=session_id,
                copy_session_id=copy_id,
                target=TARGET,
            )
        except Exception:
            logger.exception("Revoking access to a failed saved-view copy failed")

    # Graph reads for the capture run outside any session the caller is in.
    with session_context.bind_session(None):
        try:
            # A source the caller may not read is concealed as missing.
            if authorize(
                hook,
                action=GRAPH_ACTION_READ,
                target="get_visualization_session",
                session_id=session_id,
            ):
                return _refused("session_not_found")
            if authorize(
                hook, action=GRAPH_ACTION_MUTATE, target="create_visualization_session"
            ):
                return _refused("access_denied")
            session, missing = session_manager.save_session_view(
                session_id,
                name.strip(),
                expected_revision,
                authorize_capture=_hook_call(authorize_capture),
                relink_capture=relink_capture,
                assign_access=_hook_call(assign_access),
                revoke_access=revoke_access,
            )
        except (_Denied, HookFailed):
            return _refused("access_denied")
        except RevisionConflict as exc:
            return _refused("revision_conflict", current_revision=exc.actual)
        except CaptureRefused as exc:
            return _refused(exc.code)
        except tuple(_REFUSALS) as exc:
            return _refused(
                next(code for kind, code in _REFUSALS.items() if isinstance(exc, kind))
            )
        except Exception:
            # Document and staged-layer stores raise their own types (OSError,
            # LayerStoreError, database driver errors); none may escape as a 500.
            logger.exception("Saving a session view failed in storage")
            return _refused("storage_error")
    return {
        "success": True,
        "kind": "saved_session",
        "source_session_id": session_id,
        "source_revision": expected_revision,
        "session_id": session.id,
        "revision": session.seq,
        "session_url": build_session_url(session.id),
        "missing_node_ids": missing,
    }
