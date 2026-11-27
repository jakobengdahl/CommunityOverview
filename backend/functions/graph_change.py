"""Validation and matching for declarative node transition triggers."""


def valid_trigger(trigger, sensitive_fields=()):
    if not isinstance(trigger, dict) or set(trigger) - {
        "kind",
        "node_types",
        "fields",
        "before",
        "after",
        "debounce_seconds",
        "batch_size",
    }:
        return False
    if ("debounce_seconds" in trigger) != ("batch_size" in trigger):
        return False
    if "debounce_seconds" in trigger and (
        type(trigger["debounce_seconds"]) is not int
        or not 1 <= trigger["debounce_seconds"] <= 300
        or type(trigger["batch_size"]) is not int
        or not 1 <= trigger["batch_size"] <= 100
    ):
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
    if not all(
        isinstance(trigger.get(side, {}), dict)
        and all(
            isinstance(k, str) and k and all(k.split("."))
            for k in trigger.get(side, {})
        )
        for side in ("before", "after")
    ):
        return False
    paths = fields + [
        key for side in ("before", "after") for key in trigger.get(side, {})
    ]
    for path in paths:
        parts = tuple(path.split("."))
        for secret in sensitive_fields:
            secret_parts = tuple(secret.split("."))
            if (
                parts[: len(secret_parts)] == secret_parts
                or secret_parts[: len(parts)] == parts
            ):
                return False
    return True


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


def filtered_entity(entity, fields, sensitive_fields):
    """Project declared paths before persisting an event; omit sensitive subtrees."""
    from copy import deepcopy

    sensitive = [tuple(path.split(".")) for path in sensitive_fields]

    def project(value, path):
        if any(path[: len(secret)] == secret for secret in sensitive):
            return _MISSING
        if isinstance(value, dict):
            result = {}
            for key, item in value.items():
                child = project(item, path + (key,))
                if child is not _MISSING:
                    result[key] = child
            return result
        # A container with a sensitive descendant cannot safely be projected.
        if isinstance(value, list) and any(
            secret[: len(path)] == path for secret in sensitive
        ):
            return _MISSING
        return deepcopy(value)

    def select(snapshot):
        if snapshot is None:
            return None
        result = {}
        for field in sorted(fields, key=lambda path: len(path.split("."))):
            path = tuple(field.split("."))
            value = _get(snapshot, field)
            if value is _MISSING:
                continue
            value = project(value, path)
            if value is _MISSING:
                continue
            target = result
            for part in path[:-1]:
                existing = target.setdefault(part, {})
                if not isinstance(existing, dict):
                    break
                target = existing
            else:
                target[path[-1]] = value
        return result

    def changes(before, after, prefix=()):
        if any(prefix[: len(secret)] == secret for secret in sensitive):
            return [] if _json_equal(before, after) else [".".join(prefix)]
        if isinstance(before, dict) or isinstance(after, dict):
            left = before if isinstance(before, dict) else {}
            right = after if isinstance(after, dict) else {}
            if not left and not right:
                return [] if _json_equal(before, after) else [".".join(prefix)]
            return [
                name
                for key in sorted(left.keys() | right.keys())
                for name in changes(
                    left.get(key, _MISSING), right.get(key, _MISSING), prefix + (key,)
                )
            ]
        return [] if _json_equal(before, after) else [".".join(prefix)]

    data = entity["data"]
    return {
        "kind": entity["kind"],
        "id": entity["id"],
        "type": entity["type"],
        "changed_fields": changes(data["before"], data["after"]),
        "data": {side: select(data[side]) for side in ("before", "after", "patch")},
    }
