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


def replace_attribution(value: Any, actor_id: str, replacement_token: str) -> Any:
    """Copy JSON data, replacing actor slots while preserving entity content."""
    if isinstance(value, list):
        return [replace_attribution(v, actor_id, replacement_token) for v in value]
    if not isinstance(value, dict):
        return value
    result = {}
    for key, item in value.items():
        if key in {"actor_id", "actor", "decided_by", "staged_by", "client_id"}:
            if item == actor_id:
                item = replacement_token
        if key in {"actor", "on_behalf_of"} and isinstance(item, dict):
            item = dict(item)
            if item.get("id") == actor_id:
                item["id"] = replacement_token
        if key in {"origin", "event_origin"} and isinstance(item, str):
            if item == actor_id:
                item = replacement_token
            elif item == f"agent:{actor_id}":
                item = f"agent:{replacement_token}"
        result[key] = replace_attribution(item, actor_id, replacement_token)
    return result
