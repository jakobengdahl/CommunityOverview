"""Invariants of the bounded schema check (USER_FUNCTION_CONTRACT §4.3)."""

import pytest

from backend.functions.schema_check import SchemaCheckFailed, is_utf8, schema_problem


def test_is_utf8_refuses_lone_surrogates_and_values_json_cannot_hold():
    assert is_utf8({"a": ["é", 1, None]})
    assert not is_utf8({"a": "\ud800"})
    circular = []
    circular.append(circular)
    assert not is_utf8(circular)
    assert not is_utf8({"a": object()})
    deep = []
    for _ in range(100_000):
        deep = [deep]
    assert not is_utf8(deep)


def test_a_problem_message_keeps_text_beyond_ascii():
    assert "é" in schema_problem({"additionalProperties": False}, {"é": 1})


@pytest.mark.parametrize(
    "kind, value",
    [("null", None), ("boolean", True), ("array", []), ("string", ""), ("integer", 0)],
)
def test_a_schema_endless_for_one_kind_of_value_is_the_schemas_fault(kind, value):
    with pytest.raises(SchemaCheckFailed):
        schema_problem({"if": {"type": kind}, "then": {"$ref": "#"}}, value)


@pytest.mark.parametrize("kind", ["null", "array", "string", "integer"])
def test_endlessness_is_found_by_probes_even_when_the_value_is_deep(kind):
    schema = {
        "allOf": [
            {"if": {"type": kind}, "then": {"$ref": "#"}},
            {"additionalProperties": {"$ref": "#"}},
        ]
    }
    # Deep enough that the check recurses, shallow enough to parse.
    value = {}
    for _ in range(400):
        value = {"x": value}
    with pytest.raises(SchemaCheckFailed):
        schema_problem(schema, value)


def test_an_object_only_endless_schema_is_found_under_a_deep_list():
    schema = {
        "allOf": [
            {"if": {"type": "object"}, "then": {"$ref": "#"}},
            {"items": {"$ref": "#"}},
        ]
    }
    value = []
    for _ in range(400):
        value = [value]
    with pytest.raises(SchemaCheckFailed):
        schema_problem(schema, value)


def _long_chain_schema(links):
    # Each level of the value passes through ``links`` references, so the
    # check recurses for values only a little over 16 levels deep.
    defs = {f"r{i}": {"$ref": f"#/$defs/r{i + 1}"} for i in range(links)}
    defs[f"r{links}"] = {"properties": {"c": {"$ref": "#/$defs/r0"}}}
    return {"$defs": defs, "$ref": "#/$defs/r0"}


def _nested(levels):
    value = {}
    for _ in range(levels - 1):
        value = {"c": value}
    return value


def test_recursion_on_a_value_up_to_16_levels_is_the_schemas_fault_and_beyond_the_values():
    from backend.functions.schema_check import SHALLOW_LEVELS

    schema = _long_chain_schema(40)
    with pytest.raises(SchemaCheckFailed):
        schema_problem(schema, _nested(SHALLOW_LEVELS))
    assert "nested too deeply" in schema_problem(schema, _nested(SHALLOW_LEVELS + 1))
