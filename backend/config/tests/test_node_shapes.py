# covers: backend/config/node_shapes.py
"""Shape contract and configuration transport guarantees."""

import pytest
from pydantic import ValidationError

from backend.config.node_shapes import (
    ShapeDefinition,
    ShapeSelection,
    resolve_shape,
    resolve_bindings,
)


@pytest.fixture
def definition():
    return {
        "version": 1,
        "base": "rectangle",
        "scale": 1,
        "placement": "top-left",
        "areas": {"first": "type", "second": "name", "third": None},
    }


def test_definition_contract_accepts_catalogue_and_rejects_invalid_fields(definition):
    """The complete JSON contract accepts four bases and rejects malformed input."""
    for base in ("rectangle", "rectangle-flat", "triangle-up", "triangle-down"):
        raw = {
            **definition,
            "base": base,
            "placement": "centre" if base.startswith("triangle") else "top-left",
        }
        assert ShapeDefinition.model_validate(raw).base == base
    for patch in (
        {"version": True},
        {"version": 2},
        {"base": "ellipse"},
        {"scale": False},
        {"scale": float("nan")},
        {"scale": 0.09},
        {"scale": 11},
        {"placement": "centre"},
        {"extra": 1},
        {"areas": {"first": ""}},
        {"base": "triangle-up"},
    ):
        with pytest.raises(ValidationError):
            ShapeDefinition.model_validate({**definition, **patch})
    for selection in (
        {"builtin": False},
        {"builtin": 1},
        {"ref": "bad"},
        {"ref": None},
        {"builtin": True, "inline": definition},
    ):
        with pytest.raises(ValidationError):
            ShapeSelection.model_validate(selection)


def test_resolution_uses_whole_selections_in_priority_order(definition):
    """An explicit built-in stops inheritance at every priority."""
    options = {
        "config_shapes": {"card": definition},
        "node_types": {"Risk": {"shape": "card"}},
    }
    assert resolve_shape("n", "Risk", **options)["definition"] == definition
    options["type_shapes"] = {"Risk": {"inline": {**definition, "scale": 2}}}
    assert resolve_shape("n", "Risk", **options)["definition"]["scale"] == 2
    options["shape_overrides"] = {"n": {"builtin": True}}
    assert resolve_shape("n", "Risk", **options)["definition"] is None
    assert resolve_shape("other", "Other")["source"] == "builtin"


def test_deleted_and_invalid_references_stop_at_builtin(definition):
    """Deleted, unsupported and malformed selections never expose lower defaults."""
    for selection, saved in (
        ({"ref": "saved:deleted"}, {}),
        ({"ref": "config:gone"}, {}),
        ({"ref": "saved:bad"}, {"bad": {**definition, "version": 2}}),
        ({"inline": {}}, {}),
    ):
        result = resolve_shape(
            "n",
            "Risk",
            shape_overrides={"n": selection},
            type_shapes={"Risk": {"inline": definition}},
            saved_shapes=saved,
        )
        assert result["definition"] is None
        assert result["diagnostic"] is not None


def test_bindings_allow_only_declared_scalars_and_report_empty_values(definition):
    """Unavailable or composite values stay empty with structured warnings."""
    shape = ShapeDefinition.model_validate(definition)
    result = resolve_bindings(shape, {"type": ["Risk"], "name": "Example"}, [])
    assert result["values"] == {"first": "", "second": "Example", "third": ""}
    assert result["diagnostics"] == [
        {"code": "unavailable_binding", "area": "first", "field": "type"}
    ]
