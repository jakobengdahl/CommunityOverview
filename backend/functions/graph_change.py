"""Validation and matching for declarative node transition triggers."""


def valid_trigger(trigger):
    if not isinstance(trigger, dict) or set(trigger) - {
        "kind",
        "node_types",
        "fields",
        "before",
        "after",
    }:
        return False
    types = trigger.get("node_types")
    fields = trigger.get("fields")
    if (
        trigger.get("kind") != "graph_change"
        or not isinstance(types, list)
        or not types
        or any(not isinstance(x, str) or not x for x in types)
    ):
        return False
    if (
        not isinstance(fields, list)
        or not fields
        or any(
            not isinstance(x, str) or not x or any(not p for p in x.split("."))
            for x in fields
        )
    ):
        return False
    return all(
        isinstance(trigger.get(side, {}), dict)
        and all(
            isinstance(k, str) and k and all(k.split("."))
            for k in trigger.get(side, {})
        )
        for side in ("before", "after")
    )


_MISSING = object()


def _get(snapshot, path):
    value = snapshot
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return _MISSING
        value = value[part]
    return value


def matches(trigger, event):
    entity = event.to_webhook_payload()["entity"]
    if entity["kind"] != "node" or entity["type"] not in trigger["node_types"]:
        return False
    data = entity["data"]
    before, after = data["before"], data["after"]
    if not any(
        _get(before, field) != _get(after, field) for field in trigger["fields"]
    ):
        return False
    return all(
        _get(data[side], path) == value
        for side in ("before", "after")
        for path, value in trigger.get(side, {}).items()
    )
