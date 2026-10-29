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
