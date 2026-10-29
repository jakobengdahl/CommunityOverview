"""Shared validation and structured actor-attribution replacement."""

from __future__ import annotations

from typing import Any


def validate_erasure(actor_id: str, replacement_token: str) -> None:
    """Reject ambiguous replacements without echoing either input."""
    if (
        not isinstance(actor_id, str)
        or not actor_id.strip()
        or not isinstance(replacement_token, str)
        or not replacement_token.strip()
        or actor_id in replacement_token
    ):
        raise ValueError("Erasure requires a nonempty actor and a distinct replacement")


def replace_attribution(
    value: Any, actor_id: str, replacement_token: str, *, envelope: str = "record"
) -> Any:
    """Replace only schema-defined attribution paths, never arbitrary content."""
    if isinstance(value, list):
        return [
            replace_attribution(v, actor_id, replacement_token, envelope=envelope)
            for v in value
        ]
    if not isinstance(value, dict):
        return value
    children = {
        "run": {"payload": "event_data", "result": "event_data", "origin": "origin"},
        "event_data": {"event": "record", "attribution": "attribution"},
        "record": {
            "origin": "origin",
            "attribution": "attribution",
            "actor": "identity",
            "on_behalf_of": "identity",
            "inverse_op": "operation",
        },
        "origin": {"attribution": "attribution", "actor": "identity"},
        "attribution": {"actor": "identity", "on_behalf_of": "identity"},
    }
    slots = {
        "record": {"actor", "actor_id", "event_origin", "staged_by"},
        "origin": {"event_origin", "staged_by"},
        "identity": {"id", "actor_id"},
        "operation": {"client_id"},
    }
    result = dict(value)
    # Activity snapshots are annotations only when the affected entity says so.
    # Graph-history before/after values and arbitrary content stay untouched.
    if envelope == "activity" and isinstance(value.get("affected"), dict):
        if value["affected"].get("kind") == "annotation":
            for key in ("before", "after"):
                if key in value:
                    result[key] = replace_attribution(
                        value[key], actor_id, replacement_token, envelope="annotation"
                    )
    if (
        envelope == "activity_operation"
        and value.get("op") in {"annotation_created", "annotation_updated"}
        and "annotation" in value
    ):
        result["annotation"] = replace_attribution(
            value["annotation"], actor_id, replacement_token, envelope="annotation"
        )
    if envelope == "annotation" and value.get("created_by") == actor_id:
        result["created_by"] = replacement_token
    if envelope == "activity":
        if "inverse_op" in value:
            result["inverse_op"] = replace_attribution(
                value["inverse_op"],
                actor_id,
                replacement_token,
                envelope="activity_operation",
            )
        # Process ordinary record attribution without revisiting inverse_op.
        value = {k: v for k, v in value.items() if k != "inverse_op"}
        envelope = "record"
    elif envelope == "activity_operation":
        envelope = "operation"
    for key, item in value.items():
        if key in slots.get(envelope, set()) and isinstance(item, str):
            if item == actor_id:
                result[key] = replacement_token
            elif key == "event_origin" and item == f"agent:{actor_id}":
                result[key] = f"agent:{replacement_token}"
        child = children.get(envelope, {}).get(key)
        if child is not None:
            if key == "origin" and isinstance(item, str):
                if item == actor_id:
                    result[key] = replacement_token
                elif item == f"agent:{actor_id}":
                    result[key] = f"agent:{replacement_token}"
            elif isinstance(item, (dict, list)):
                result[key] = replace_attribution(
                    item, actor_id, replacement_token, envelope=child
                )
    return result
