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


def _json_equal(left, right):
    # JSON booleans are distinct from numbers, including inside containers.
    if left is _MISSING or right is _MISSING:
        return left is right
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(
            _json_equal(left[key], right[key]) for key in left
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _json_equal(a, b) for a, b in zip(left, right)
        )
    return left == right


def matches(trigger, event):
    entity = event.to_webhook_payload()["entity"]
    if entity["kind"] != "node" or entity["type"] not in trigger["node_types"]:
        return False
    data = entity["data"]
    before, after = data["before"], data["after"]
    if not any(
        not _json_equal(_get(before, field), _get(after, field))
        for field in trigger["fields"]
    ):
        return False
    return all(
        _json_equal(_get(data[side], path), value)
        for side in ("before", "after")
        for path, value in trigger.get(side, {}).items()
    )
