"""Capture a session as a validated, self-contained template document."""

import copy
import logging
from datetime import datetime, timezone

from backend.config.config_loader import SYSTEM_NODE_TYPES, ConfigLoader
from backend.config.node_shapes import resolve_shape
from backend.core.session_annotations import (
    ATTACHMENT_ID_KEYS,
    annotation_link_fields,
    annotation_type_of,
)
from backend.core.session_manager import (
    CaptureRefused,
    LayoutBusy,
    RevisionConflict,
    SessionNotFound,
)
from backend.core.session_store import _DEFAULT_MAX_ANNOTATIONS, is_valid_session_id
from backend.core.template_store import (
    ALLOWED_METADATA,
    TemplateError,
    validate_template,
)
from backend.runtime import session_context
from backend.runtime.authorization import GRAPH_ACTION_READ

from . import access
from .session_capture import (
    _annotation_links,
    _hook_call,
    _LEGACY_GROUP_PREFIX,
    _link_id,
    INVALID_SESSION_ID_MESSAGE,
)
from .staging import HookFailed

logger = logging.getLogger(__name__)


def _line_points(annotation):
    """Explicit line coordinates, including legacy and nested endpoints."""
    if annotation_type_of(annotation) != "line":
        return []
    points = []
    for payload in (annotation, annotation.get("content")):
        if not isinstance(payload, dict):
            continue
        points.extend(payload.get(k) for k in ("from", "to"))
        for field in ("start", "end", "startEndpoint", "endEndpoint"):
            endpoint = payload.get(field)
            if isinstance(endpoint, dict):
                points.append(
                    endpoint.get("point") or endpoint.get("position") or endpoint
                )
    return [p for p in points if isinstance(p, dict)]


def _freehand_points(annotation):
    if annotation_type_of(annotation) != "freehand":
        return []
    points = []
    for payload in (annotation, annotation.get("content")):
        if isinstance(payload, dict) and isinstance(payload.get("points"), list):
            points.extend(p for p in payload["points"] if isinstance(p, dict))
    return points


def _shape(node, state, config, saved):
    overrides = state.get("shape_overrides") or {}
    defaults = state.get("type_shapes") or {}
    selection = overrides.get(node.id, defaults.get(node.type_str))
    result = resolve_shape(
        node.id,
        node.type_str,
        shape_overrides=overrides,
        type_shapes=defaults,
        node_types=config["types"],
        config_shapes=config["shapes"],
        saved_shapes=saved,
    )
    definition = result["definition"]
    if definition is None:
        return {"ref": None, "inline": None, "builtin": True}
    if selection is None:
        name = config["types"].get(node.type_str, {}).get("shape")
        ref = f"config:{name}" if name else None
    else:
        ref = selection.get("ref")
    return {"ref": ref, "inline": definition, "builtin": False}


def _annotations(source, selected, node_keys):
    annotations = copy.deepcopy(selected)
    all_ids = {a.get("id") for a in source if isinstance(a, dict)}
    # Resolve and authorize links even on references whose targets cannot be
    # represented in the template. A reference without its resource target
    # is invalid, so omit that annotation and links to it from the document.
    linked = []
    kept = []
    for annotation in annotations:
        content = annotation.get("content")
        merged = {**annotation, **(content if isinstance(content, dict) else {})}
        for payload in (annotation, content):
            if isinstance(payload, dict):
                resolved_payload = {
                    **payload,
                    "type": annotation.get("type"),
                    "kind": annotation.get("kind"),
                    "target_kind": merged.get("target_kind"),
                }
                linked.extend(
                    v
                    for _, _, v, kind in _annotation_links(resolved_payload, all_ids)
                    if kind == "node"
                )
        target = merged.get("target")
        if (
            annotation_type_of(annotation) == "reference"
            and merged.get("target_kind") == "resource"
            and target
            and _link_id(target) not in node_keys
        ):
            continue
        kept.append(annotation)
    annotations = kept
    ids = {a.get("id") for a in annotations if isinstance(a, dict)}
    for annotation in annotations:
        content = annotation.get("content")
        target_kind = annotation.get("target_kind")
        if isinstance(content, dict):
            target_kind = content.get("target_kind", target_kind)
        for payload in (annotation, content):
            if not isinstance(payload, dict):
                continue
            resolved_payload = {
                **payload,
                "type": annotation.get("type"),
                "kind": annotation.get("kind"),
                "target_kind": target_kind,
            }
            resolved = {
                (id(h), k): (v, kind)
                for h, k, v, kind in _annotation_links(resolved_payload, all_ids)
            }
            # The attachment dicts a link can sit in, by identity, with the
            # dict and key holding each; endpoint dicts are not containers,
            # their point stays valid once a link is dropped.
            containers = {}
            for field in ("attachment", "anchor"):
                if isinstance(payload.get(field), dict):
                    containers[id(payload[field])] = (payload, field)
            for field in ("start", "startEndpoint", "end", "endEndpoint"):
                endpoint = payload.get(field)
                if isinstance(endpoint, dict) and isinstance(
                    endpoint.get("attachment"), dict
                ):
                    containers[id(endpoint["attachment"])] = (endpoint, "attachment")
            removals = []
            for holder, key, declared in annotation_link_fields(resolved_payload):
                value = _link_id(
                    holder[key] if isinstance(holder, list) else holder.get(key)
                )
                if value is None:
                    continue
                link = resolved.get((id(holder), key))
                if holder is resolved_payload:
                    holder = payload
                # The canvas resolves an id naming an annotation as one even
                # in a node-only field; the template format cannot hold that.
                node_only = isinstance(holder, list) or declared == "node"
                target = (
                    node_keys.get(value)
                    if link and link[1] == "node"
                    else value
                    if link and value in ids and not node_only
                    else None
                )
                if target is None:
                    removals.append((holder, key))
                else:
                    holder[key] = target
            for holder, key in reversed(removals):
                if isinstance(holder, list):
                    del holder[key]
                else:
                    holder.pop(key, None)
            # An attachment left without an id is invalid; drop it whole.
            for holder, _ in removals:
                parent = containers.get(id(holder))
                if parent and not any(holder.get(k) for k in ATTACHMENT_ID_KEYS):
                    parent[0].pop(parent[1], None)
    return annotations, linked


def capture_template(
    service,
    session_manager,
    session_id,
    *,
    template_id,
    name,
    created_by,
    expected_revision,
    description="",
    tags=None,
    node_ids=None,
    annotation_ids=None,
):
    """Return a v1 document or a stable error; this function never persists it."""
    if (
        not isinstance(session_id, str)
        or not is_valid_session_id(session_id)
        or not isinstance(template_id, str)
        or not is_valid_session_id(template_id)
        or not isinstance(name, str)
        or not name.strip()
        or not isinstance(created_by, str)
        or not created_by
        or type(expected_revision) is not int
        or expected_revision < 0
        or any(
            v is not None
            and (not isinstance(v, list) or any(not isinstance(i, str) for i in v))
            for v in (node_ids, annotation_ids)
        )
    ):
        return {"success": False, "error": "invalid_request"}
    try:
        with (
            session_context.bind_session(None),
            session_manager.store._lock,
            session_manager.store._backend.transaction(),
        ):
            if _hook_call(access.authorize_graph_access)(
                service.authorization_hook,
                action=GRAPH_ACTION_READ,
                target="get_visualization_session",
                session_id=session_id,
            ):
                return {"success": False, "error": "session_not_found"}
            source, _ = session_manager.capture_source(session_id, expected_revision)
            state = copy.deepcopy(source.state)
            all_annotations = state.get("annotations") or []
            whole = node_ids is None and annotation_ids is None
            membership = state.get("node_refs") or []
            requested = membership if whole else (node_ids or [])
            # Like annotation_ids, explicit node_ids select from the session only.
            requested = list(
                dict.fromkeys(
                    i
                    for i in requested
                    if isinstance(i, str)
                    and i in membership
                    and not i.startswith(_LEGACY_GROUP_PREFIX)
                )
            )
            annotations = (
                all_annotations
                if whole
                else [
                    a
                    for a in all_annotations
                    if isinstance(a, dict) and a.get("id") in (annotation_ids or [])
                ]
            )
            storage = service._storage
            decision = _hook_call(access.evaluate_graph_access)(
                service.authorization_hook,
                action=GRAPH_ACTION_READ,
                target="save_session_view",
            )
            if not decision.allowed:
                return {"success": False, "error": "access_denied"}
            existing = {i: storage.get_node(i) for i in requested}
            nodes = {
                i: n
                for i, n in existing.items()
                if n is not None and n.type_str not in SYSTEM_NODE_TYPES
            }
            used = {a.get("id") for a in annotations if isinstance(a, dict)}
            keys = {}
            for index, node_id in enumerate(nodes):
                key = f"node-{index}"
                while key in used:
                    key += "-"
                used.add(key)
                keys[node_id] = key
            selected_count = len(annotations)
            annotations, linked = _annotations(all_annotations, annotations, keys)
            # As on the save path: under narrowing a missing ref is denied too.
            refs = _hook_call(service.authorize_capture_refs)(
                list(dict.fromkeys(requested + linked)), []
            )
            if not refs.get("success"):
                return {"success": False, "error": "access_denied"}
            # After authorization so a denial wins; counted before omitted
            # references were dropped so they cannot bypass the limit.
            if selected_count > _DEFAULT_MAX_ANNOTATIONS:
                return {"success": False, "error": "annotations_limit"}
            config = ConfigLoader().config
            shapes = {k: v.model_dump() for k, v in config.node_shapes.items()}
            types = {k: v.model_dump() for k, v in config.schema_.node_types.items()}
            library = session_manager.shape_library
            saved = (
                {k: v["definition"] for k, v in library.snapshot()["entries"].items()}
                if library
                else {}
            )
            positions = state.get("positions") or {}
            captured = []
            for i, node in nodes.items():
                snapshot = node.to_dict()
                snapshot = {
                    k: snapshot[k]
                    for k in (
                        "type",
                        "name",
                        "description",
                        "summary",
                        "tags",
                        "subtypes",
                        "metadata",
                    )
                }
                snapshot["metadata"] = {
                    k: v
                    for k, v in snapshot["metadata"].items()
                    if k in ALLOWED_METADATA
                }
                captured.append(
                    {
                        "key": keys[i],
                        "source_node_id": i,
                        "snapshot": snapshot,
                        "position": positions.get(i, {"x": 0, "y": 0}),
                        "hidden": i in (state.get("hidden_node_ids") or []),
                        "shape": _shape(
                            node, state, {"types": types, "shapes": shapes}, saved
                        ),
                    }
                )
            edges = []
            for edge in storage.get_edges_between_nodes(list(nodes)):
                if not access.is_edge_visible(edge, storage, decision.graph_access):
                    continue
                edges.append(
                    {
                        "source_edge_id": edge.id,
                        "type": edge.type_str,
                        "source": keys[edge.source],
                        "target": keys[edge.target],
                        "label": edge.label,
                    }
                )
            points = [n["position"] for n in captured]
            points += [
                a.get("position") or a.get("geometry")
                for a in annotations
                if isinstance(a, dict) and (a.get("position") or a.get("geometry"))
            ]
            # Geometry is not validated on write; a resize-only update can
            # store a size without a position, which the canvas reads as 0.
            xs = [p.get("x", 0) for p in points]
            ys = [p.get("y", 0) for p in points]
            bounds = {
                "x": min(xs, default=0),
                "y": min(ys, default=0),
                "width": max(xs, default=0) - min(xs, default=0),
                "height": max(ys, default=0) - min(ys, default=0),
            }
            doc = {
                "schema_version": 1,
                "template_id": template_id,
                "name": name.strip(),
                "description": description,
                "tags": tags if tags is not None else [],
                "revision": 0,
                "created_by": created_by,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source_session_id": session_id,
                "source_session_revision": source.seq,
                "nodes": captured,
                "edges": edges,
                "annotation_document": {
                    "schema_version": 1,
                    "annotations": annotations,
                },
                "bounds": bounds,
                "thumbnail": None,
            }
            return {"success": True, "document": validate_template(doc)}
    except TemplateError as exc:
        return {"success": False, "error": exc.code}
    except CaptureRefused as exc:
        return {"success": False, "error": exc.code}
    except HookFailed:
        # As on the save path: a hook that could not answer has not authorized.
        return {"success": False, "error": "access_denied"}
    except RevisionConflict:
        return {"success": False, "error": "revision_conflict"}
    except (LayoutBusy, SessionNotFound) as exc:
        return {
            "success": False,
            "error": "session_busy"
            if isinstance(exc, LayoutBusy)
            else "session_not_found",
        }
    except (TypeError, ValueError, KeyError, AttributeError):
        return {"success": False, "error": "invalid_presentation"}

    except Exception:
        return {"success": False, "error": "storage_error"}


# The session authorization seam also governs templates in open core.
ERROR_STATUS = {
    "unsupported_mode": 400,
    "invalid_request": 400,
    "access_denied": 403,
    "template_not_found": 404,
    "session_not_found": 404,
    "revision_conflict": 409,
    "session_busy": 409,
    "staged_changes": 409,
    "invalid_presentation": 422,
    "nodes_limit": 413,
    "edges_limit": 413,
    "annotations_limit": 413,
    "session_too_large": 413,
    "storage_error": 503,
}


def _authorize(service, action, target, session_id=None):
    from backend.runtime.session_context import bind_session

    try:
        # Collection operations have no session target, regardless of any MCP binding.
        with bind_session(None):
            denied = access.authorize_graph_access(
                service.authorization_hook,
                action=action,
                target=target,
                session_id=session_id,
            )
        return denied is None
    except Exception:
        return False


def _failure(code, **extra):
    return {"success": False, "error": code, **extra}


def _conflict(store, template_id):
    # The CAS lost to a concurrent writer; answer with what it wrote.
    try:
        doc = store.get(template_id)
    except Exception:
        return _failure("storage_error")
    if doc is None:
        return _failure("template_not_found")
    return _failure("revision_conflict", current_revision=doc["revision"])


def _metadata(name, description, tags):
    return (
        isinstance(name, str)
        and bool(name.strip())
        and len(name) <= 200
        and isinstance(description, str)
        and isinstance(tags, list)
        and all(isinstance(tag, str) for tag in tags)
    )


def save_template(
    service,
    manager,
    session_id,
    name,
    expected_revision,
    node_ids=None,
    annotation_ids=None,
    description="",
    tags=None,
):
    import secrets
    from backend.runtime.authorization import GRAPH_ACTION_MUTATE

    if manager is None:
        return _failure("storage_error")
    if not isinstance(session_id, str) or not is_valid_session_id(session_id):
        return _failure("invalid_request", message=INVALID_SESSION_ID_MESSAGE)
    tags = [] if tags is None else tags
    if not _metadata(name, description, tags):
        return _failure("invalid_request")
    if not _authorize(service, GRAPH_ACTION_MUTATE, "create_visualization_session"):
        return _failure("access_denied")
    try:
        store = manager.template_store
        with store._backend.transaction():
            for _ in range(100):
                template_id = "-".join(
                    f"{secrets.randbelow(10000):04d}" for _ in range(4)
                )
                # Templates authorize as sessions by id, so a template id
                # must never name a session, live or retired.
                if (
                    store._backend.load(template_id) is None
                    and not manager.store.exists(template_id)
                    and manager.write_modes.staged_status(template_id) == "none"
                ):
                    break
            else:
                return _failure("storage_error")
            result = capture_template(
                service,
                manager,
                session_id,
                template_id=template_id,
                name=name,
                created_by=access.build_mutation_actor_id(
                    target="create_visualization_session"
                ),
                expected_revision=expected_revision,
                description=description,
                tags=tags,
                node_ids=node_ids,
                annotation_ids=annotation_ids,
            )
            if not result["success"]:
                if result["error"] == "invalid_document":
                    result["error"] = "invalid_presentation"
                return result
            hook = service.authorization_hook
            try:
                with session_context.bind_session(None):
                    denied = access.assign_copied_session_access(
                        hook,
                        source_session_id=session_id,
                        copy_session_id=template_id,
                        target="save_template",
                    )
            except Exception:
                denied = True
            if denied:
                # A refusing or raising hook may still have granted something.
                try:
                    with session_context.bind_session(None):
                        access.revoke_copied_session_access(
                            hook,
                            source_session_id=session_id,
                            copy_session_id=template_id,
                            target="save_template",
                        )
                except Exception:
                    logger.exception("Revoking access to an unsaved template failed")
                return _failure("access_denied")
            try:
                saved = store.save(result["document"], 0)
            except Exception:
                with session_context.bind_session(None):
                    access.revoke_copied_session_access(
                        hook,
                        source_session_id=session_id,
                        copy_session_id=template_id,
                        target="save_template",
                    )
                raise
            return {"success": True, "template": saved}
    except Exception:
        return _failure("storage_error")


def list_templates(service, manager, query=None, tags=None, limit=None):
    if manager is None:
        return _failure("storage_error")
    if (
        (query is not None and not isinstance(query, str))
        or (
            tags is not None
            and (
                not isinstance(tags, list) or any(not isinstance(t, str) for t in tags)
            )
        )
        or (limit is not None and (type(limit) is not int or limit < 1))
    ):
        return _failure("invalid_request")
    if not _authorize(service, GRAPH_ACTION_READ, "list_visualization_sessions"):
        return _failure("access_denied")
    try:
        docs = [
            doc
            for doc in manager.template_store.list()
            if _authorize(
                service,
                GRAPH_ACTION_READ,
                "get_visualization_session",
                doc["template_id"],
            )
            and (
                not query
                or query.casefold()
                in (doc["name"] + " " + doc["description"]).casefold()
            )
            and (not tags or set(tags) <= set(doc["tags"]))
        ]
        return {"success": True, "templates": docs[:limit], "count": len(docs[:limit])}
    except Exception:
        return _failure("storage_error")


def get_template(service, manager, template_id):
    if manager is None:
        return _failure("storage_error")
    if not isinstance(template_id, str) or not is_valid_session_id(template_id):
        return _failure("invalid_request")
    if not _authorize(
        service, GRAPH_ACTION_READ, "get_visualization_session", template_id
    ):
        return _failure("access_denied")
    try:
        doc = manager.template_store.get(template_id)
        return (
            {"success": True, "template": doc}
            if doc
            else _failure("template_not_found")
        )
    except Exception:
        return _failure("storage_error")


def update_template(
    service,
    manager,
    template_id,
    expected_revision,
    name=None,
    description=None,
    tags=None,
    replace_from=None,
):
    from backend.runtime.authorization import GRAPH_ACTION_MUTATE

    if manager is None:
        return _failure("storage_error")
    if (
        not isinstance(template_id, str)
        or not is_valid_session_id(template_id)
        or type(expected_revision) is not int
        or expected_revision < 0
    ):
        return _failure("invalid_request")
    if not _authorize(
        service, GRAPH_ACTION_MUTATE, "visualization_session", template_id
    ):
        return _failure("access_denied")
    try:
        store = manager.template_store
        with store._backend.transaction():
            try:
                doc = store.get(template_id)
            except TemplateError:
                # An unreadable stored document, as get_template answers it.
                return _failure("storage_error")
            if doc is None:
                return _failure("template_not_found")
            if doc["revision"] != expected_revision:
                return _failure("revision_conflict", current_revision=doc["revision"])
            new_name = doc["name"] if name is None else name
            new_description = doc["description"] if description is None else description
            new_tags = doc["tags"] if tags is None else tags
            if not _metadata(new_name, new_description, new_tags):
                return _failure("invalid_request")
            if replace_from is not None:
                if not isinstance(replace_from, dict) or set(replace_from) - {
                    "session_id",
                    "expected_revision",
                    "node_ids",
                    "annotation_ids",
                }:
                    return _failure("invalid_request", field="replace_from")
                result = capture_template(
                    service,
                    manager,
                    replace_from.get("session_id"),
                    template_id=template_id,
                    name=new_name,
                    created_by=doc["created_by"],
                    expected_revision=replace_from.get("expected_revision"),
                    description=new_description,
                    tags=new_tags,
                    node_ids=replace_from.get("node_ids"),
                    annotation_ids=replace_from.get("annotation_ids"),
                )
                if not result["success"]:
                    if result["error"] == "invalid_document":
                        result["error"] = "invalid_presentation"
                    return {**result, "field": "replace_from"}
                fresh = result["document"]
                for key in (
                    "source_session_id",
                    "source_session_revision",
                    "nodes",
                    "edges",
                    "annotation_document",
                    "bounds",
                    "thumbnail",
                ):
                    doc[key] = fresh[key]
            doc.update(
                name=new_name.strip(), description=new_description, tags=new_tags
            )
            return {"success": True, "template": store.save(doc, expected_revision)}
    except TemplateError as exc:
        if exc.code == "revision_conflict":
            return _conflict(store, template_id)
        return _failure("invalid_request")
    except Exception:
        return _failure("storage_error")


def delete_template(service, manager, template_id, expected_revision):
    from backend.runtime.authorization import GRAPH_ACTION_MUTATE

    if manager is None:
        return _failure("storage_error")
    if (
        not isinstance(template_id, str)
        or not is_valid_session_id(template_id)
        or type(expected_revision) is not int
        or expected_revision < 0
    ):
        return _failure("invalid_request")
    if not _authorize(
        service, GRAPH_ACTION_MUTATE, "delete_visualization_session", template_id
    ):
        return _failure("access_denied")
    try:
        manager.template_store.delete(template_id, expected_revision)
        return {"success": True, "deleted": True}
    except TemplateError as exc:
        if exc.code == "not_found":
            return _failure("template_not_found")
        return _conflict(manager.template_store, template_id)
    except Exception:
        return _failure("storage_error")


def insert_template(
    service,
    manager,
    session_id,
    template_id,
    mode="reference",
    x=None,
    y=None,
    expected_revision=None,
    template_revision=None,
):
    """Insert a template's existing nodes and presentation in one session write."""
    import math
    import secrets
    from backend.runtime.authorization import GRAPH_ACTION_MUTATE
    from backend.core.session_store import OpError, AnnotationLimitReached
    from backend.core.session_manager import OpBatchTooLarge, _check_op_image_sizes
    from .session_capture import relink_capture

    def valid_coordinate(value):
        if value is None:
            return True
        if type(value) not in (int, float):
            return False
        try:
            return math.isfinite(value)
        except OverflowError:
            return False

    if manager is None:
        return _failure("storage_error")
    if not isinstance(session_id, str) or not is_valid_session_id(session_id):
        return _failure("invalid_request", message=INVALID_SESSION_ID_MESSAGE)
    if (
        not isinstance(template_id, str)
        or not is_valid_session_id(template_id)
        or any(
            v is not None and (type(v) is not int or v < 0)
            for v in (expected_revision, template_revision)
        )
        or not all(valid_coordinate(v) for v in (x, y))
        or (x is None) != (y is None)
    ):
        return _failure("invalid_request")
    if mode != "reference":
        return _failure("unsupported_mode" if mode == "copy" else "invalid_request")
    if not _authorize(service, GRAPH_ACTION_MUTATE, "add_nodes_to_session", session_id):
        return _failure("access_denied")
    try:
        # Hold both stores through the check and publication so neither revision
        # can change between validation and the session write.
        with (
            manager.template_store._backend.transaction(),
            manager.store._lock,
            manager.store._backend.transaction(),
        ):
            result = get_template(service, manager, template_id)
            if not result["success"]:
                return result
            doc = result["template"]
            if template_revision is not None and template_revision != doc["revision"]:
                return _failure("revision_conflict", current_revision=doc["revision"])
            if manager._lock(session_id).locked():
                return _failure("session_busy")
            session = manager.get_session(session_id)
            if session is None or manager.write_modes.is_retired(session_id):
                return _failure("session_not_found")
            if expected_revision is not None and session.seq != expected_revision:
                return _failure("revision_conflict", current_revision=session.seq)
            ids = list(dict.fromkeys(n["source_node_id"] for n in doc["nodes"]))
            with service.reading_in(session_id):
                resolved = service.resolve_session_node_semantics(
                    ids, action=GRAPH_ACTION_MUTATE, target="add_nodes_to_session"
                )
            if not resolved.get("success"):
                return _failure("access_denied")
            known = resolved.get("nodes") or {}
            present = set(session.state.get("node_refs", []))
            added, skipped, keys = [], [], {}
            positions, overrides = {}, {}
            hidden = list(session.state.get("hidden_node_ids", []))
            if x is None:
                right = [
                    p.get("x", 0)
                    for p in (session.state.get("positions") or {}).values()
                ]
                for annotation in session.state.get("annotations", []):
                    p = annotation.get("position") or annotation.get("geometry") or {}
                    size = annotation.get("size") or annotation.get("geometry") or {}
                    right.append(p.get("x", 0) + size.get("width", size.get("w", 0)))
                    right.extend(
                        p["x"]
                        for p in _line_points(annotation) + _freehand_points(annotation)
                        if isinstance(p.get("x"), (int, float))
                    )
                x = max(right, default=-200) + 200
                y = 0
            dx, dy = x - doc["bounds"]["x"], y - doc["bounds"]["y"]
            library = manager.shape_library
            entries = library.backend.read()["entries"] if library else {}
            config = ConfigLoader().config.node_shapes
            shape_report = []
            for node in doc["nodes"]:
                node_id = node["source_node_id"]
                graph_node = service._storage.get_node(node_id)
                if (
                    node_id not in known
                    or graph_node is None
                    or getattr(graph_node, "archived", False)
                ):
                    skipped.append({"node_id": node_id, "reason": "missing"})
                    continue
                keys[node["key"]] = node_id
                if node_id in present:
                    skipped.append({"node_id": node_id, "reason": "already_present"})
                    continue
                present.add(node_id)
                added.append(node_id)
                p = node["position"]
                positions[node_id] = {"x": p["x"] + dx, "y": p["y"] + dy}
                if node["hidden"]:
                    hidden.append(node_id)
                shape = node["shape"]
                if shape is None:
                    continue
                ref = shape["ref"]
                if shape["builtin"]:
                    selection = {"builtin": True}
                elif ref:
                    namespace, name = ref.split(":", 1)
                    entry = entries.get(name) if namespace == "saved" else None
                    if entry and entry.get("deleted"):
                        selection = {"builtin": True}
                        shape_report.append(
                            {"node_id": node_id, "ref": ref, "reason": "tombstoned"}
                        )
                    elif (entry and not entry.get("deleted")) or (
                        namespace == "config" and name in config
                    ):
                        selection = {"ref": ref}
                    else:
                        selection = {"inline": shape["inline"]}
                        shape_report.append(
                            {"node_id": node_id, "ref": ref, "reason": "missing_shape"}
                        )
                else:
                    selection = {"inline": shape["inline"]}
                overrides[node_id] = selection
            source = copy.deepcopy(doc["annotation_document"]["annotations"])
            occupied = (
                present
                | {a.get("id") for a in session.state.get("annotations", [])}
                | set(session._deleted_annotation_ids)
                | {a["id"] for a in source}
                | {n["key"] for n in doc["nodes"]}
            )
            fresh = {}
            for a in source:
                new_id = secrets.token_hex(16)
                while new_id in occupied:
                    new_id = secrets.token_hex(16)
                occupied.add(new_id)
                fresh[a["id"]] = new_id
            relink_capture(
                {
                    "annotations": source,
                    "node_refs": [n["key"] for n in doc["nodes"]],
                    "shape_overrides": {},
                },
                fresh,
            )
            for a in source:
                a["id"] = fresh[a["id"]]
            annotations, _ = _annotations(source, source, keys)
            kept = {a["id"] for a in annotations}
            fresh = {old: new for old, new in fresh.items() if new in kept}
            for a in annotations:
                points = [
                    a.get("position"),
                    a.get("geometry"),
                    *_line_points(a),
                    *_freehand_points(a),
                ]
                seen = set()
                for p in points:
                    if isinstance(p, dict) and id(p) not in seen:
                        seen.add(id(p))
                        if "x" in p:
                            p["x"] += dx
                        if "y" in p:
                            p["y"] += dy
            op = {
                "op": "nodes_replaced",
                "client_id": "mcp-agent",
                "node_ids": session.state.get("node_refs", []) + added,
                "positions": positions,
                "hidden_node_ids": hidden,
                "annotations": session.state.get("annotations", []) + annotations,
                "inserted_shape_overrides": overrides,
            }
            _check_op_image_sizes(op)
            manager._apply_op_sync(
                session, session_id, "mcp-agent", op, enforce_session_budgets=True
            )
            return {
                "success": True,
                "added": added,
                "annotation_id_map": fresh,
                "skipped": skipped,
                "shapes": shape_report,
                "revision": session.seq,
            }
    except AnnotationLimitReached:
        return _failure("annotations_limit")
    except OpBatchTooLarge:
        return _failure("session_too_large")
    except (OpError, TypeError, ValueError, KeyError):
        return _failure("invalid_presentation")
    except Exception:
        logger.exception("Template insertion failed")
        return _failure("storage_error")
