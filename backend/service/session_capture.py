"""``save_session_view``: capture a session's presentation as a new session.

The MCP tool and the REST route both answer through ``save_session_view`` here,
so the same input gives the same result on either surface
(docs/SESSION_SAVEDVIEW_COMPAT_CONTRACT.md, "Identity and capture").
"""

import logging
from typing import Any, Callable, Dict, List

from backend.config.config_loader import build_session_url
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


def captured_node_ids(state: Dict[str, Any]) -> List[str]:
    """Every graph node id the captured canvas names, first use first.

    ``shape_revisions`` keys are not read: they outlive a node's removal from
    the canvas, so a removed node would be checked and reported missing.
    """
    ids: Dict[str, None] = {}
    for key in ("node_refs", "hidden_node_ids", "dimmed_node_ids"):
        ids.update(dict.fromkeys(state.get(key) or []))
    annotations = state.get("annotations") or []
    annotation_ids = {a.get("id") for a in annotations if isinstance(a, dict)}
    for key in ("positions", "shape_overrides"):
        ids.update(
            dict.fromkeys(
                i
                for i in (state.get(key) or {})
                if i not in annotation_ids and not i.startswith("group-")
            )
        )

    annotation_targets: set[str] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if (
                value.get("type") == "reference"
                and value.get("target_kind") == "resource"
            ):
                target = value.get("target")
                if isinstance(target, str):
                    ids[target] = None
                    annotation_targets.add(target)
            target = value.get("target_id")
            if isinstance(target, str) and (
                value.get("target_type") == "node" or target not in annotation_ids
            ):
                ids[target] = None
                annotation_targets.add(target)
            for key, child in value.items():
                if key == "member_node_ids" and isinstance(child, list):
                    graph_members = [i for i in child if i not in annotation_ids]
                    ids.update(dict.fromkeys(graph_members))
                    annotation_targets.update(graph_members)
                elif (
                    key
                    in (
                        "startAnchor",
                        "endAnchor",
                        "parentId",
                        "parent_id",
                        "parentNode",
                    )
                    and isinstance(child, str)
                    and child not in annotation_ids
                ):
                    ids[child] = None
                    annotation_targets.add(child)
                else:
                    visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(annotations)
    return [
        i
        for i in ids
        if isinstance(i, str)
        and (
            not i.startswith("group-")
            or i in (state.get("node_refs") or [])
            or i in annotation_targets
        )
    ]


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
