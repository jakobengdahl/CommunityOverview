"""Capture a session as a validated, self-contained template document."""

import copy
from datetime import datetime, timezone

from backend.config.config_loader import SYSTEM_NODE_TYPES, ConfigLoader
from backend.config.node_shapes import resolve_shape
from backend.core.session_annotations import ATTACHMENT_ID_KEYS, annotation_link_fields
from backend.core.session_manager import (
    CaptureRefused,
    LayoutBusy,
    RevisionConflict,
    SessionNotFound,
)
from backend.core.session_store import is_valid_session_id
from backend.core.template_store import (
    ALLOWED_METADATA,
    TemplateError,
    validate_template,
)
from backend.runtime import session_context
from backend.runtime.authorization import GRAPH_ACTION_READ

from . import access
from .session_capture import _annotation_links, _LEGACY_GROUP_PREFIX, _link_id


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
    ids = {a.get("id") for a in annotations if isinstance(a, dict)}
    # Ask the same resolver as session save which links address graph nodes.
    linked = []
    for annotation in annotations:
        for payload in (annotation, annotation.get("content")):
            if not isinstance(payload, dict):
                continue
            # Content carries payload fields while the annotation carries its type.
            content = annotation.get("content")
            target_kind = annotation.get("target_kind")
            if isinstance(content, dict):
                target_kind = content.get("target_kind", target_kind)
            resolved_payload = {
                **payload,
                "type": annotation.get("type"),
                "kind": annotation.get("kind"),
                "target_kind": target_kind,
            }
            linked.extend(
                v
                for _, _, v, kind in _annotation_links(resolved_payload, all_ids)
                if kind == "node" and not v.startswith(_LEGACY_GROUP_PREFIX)
            )
            resolved = {
                (id(h), k): (v, kind)
                for h, k, v, kind in _annotation_links(resolved_payload, all_ids)
            }
            removals = []
            for holder, key, _ in annotation_link_fields(resolved_payload):
                value = _link_id(
                    holder[key] if isinstance(holder, list) else holder.get(key)
                )
                if value is None:
                    continue
                link = resolved.get((id(holder), key))
                if holder is resolved_payload:
                    holder = payload
                target = (
                    node_keys.get(value)
                    if link and link[1] == "node"
                    else value
                    if link and value in ids
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
    for annotation in annotations:
        for payload in (annotation, annotation.get("content")):
            if isinstance(payload, dict):
                for field in ("attachment", "anchor"):
                    value = payload.get(field)
                    if isinstance(value, dict) and not any(
                        value.get(key) for key in ATTACHMENT_ID_KEYS
                    ):
                        payload.pop(field)
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
            if access.authorize_graph_access(
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
            requested = (
                membership
                if whole
                else (node_ids or [])
            )
            requested = list(
                dict.fromkeys(
                    i
                    for i in requested
                    if isinstance(i, str) and not i.startswith(_LEGACY_GROUP_PREFIX)
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
            decision = access.evaluate_graph_access(
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
            annotations, linked = _annotations(all_annotations, annotations, keys)
            for i in set(requested + linked):
                node = storage.get_node(i)
                if node is not None and not access.is_node_visible(
                    node, decision.graph_access
                ):
                    return {"success": False, "error": "access_denied"}
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
                a.get("position", a.get("geometry"))
                for a in annotations
                if isinstance(a, dict) and (a.get("position") or a.get("geometry"))
            ]
            xs = [p["x"] for p in points]
            ys = [p["y"] for p in points]
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
