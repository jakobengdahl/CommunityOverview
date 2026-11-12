"""Validated, renderer-independent node appearance contracts."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

ShapeName = Annotated[str, Field(strict=True, pattern=r"^[a-z][a-z0-9-]{0,63}$")]
Binding = Annotated[str, Field(strict=True, min_length=1, max_length=64)] | None
RECTANGLE_PLACEMENTS = {
    f"{vertical}-{horizontal}"
    for vertical in ("top", "middle", "bottom")
    for horizontal in ("left", "centre", "right")
}


class ShapeAreas(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    first: Binding
    second: Binding
    third: Binding


class ShapeDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1]
    base: Literal["rectangle", "rectangle-flat", "triangle-up", "triangle-down"]
    scale: float = Field(ge=0.1, le=10, allow_inf_nan=False)
    placement: str
    areas: ShapeAreas

    @model_validator(mode="before")
    @classmethod
    def numeric_contract(cls, value):
        # Python bool is an int, but JSON booleans are not version numbers.
        if isinstance(value, dict) and type(value.get("version")) is not int:
            raise ValueError("version must be an integer")
        return value

    @model_validator(mode="after")
    def placement_matches_base(self):
        allowed = (
            {"centre", "bottom"}
            if self.base.startswith("triangle-")
            else RECTANGLE_PLACEMENTS
        )
        if self.placement not in allowed:
            raise ValueError("placement is unsupported for this base")
        return self


class ShapeSelection(BaseModel):
    """Exactly one complete selection; absence is represented outside this model."""

    model_config = ConfigDict(extra="forbid", strict=True)
    ref: (
        Annotated[str, Field(pattern=r"^(config|saved):[a-z][a-z0-9-]{0,63}$")] | None
    ) = None
    inline: ShapeDefinition | None = None
    builtin: Literal[True] | None = None

    @model_validator(mode="before")
    @classmethod
    def exactly_one(cls, value):
        if not isinstance(value, dict) or len(value) != 1:
            raise ValueError("selection must contain exactly one key")
        key = next(iter(value))
        if key not in {"ref", "inline", "builtin"} or value[key] is None:
            raise ValueError("selection must contain a non-null selection")
        if key == "builtin" and value[key] is not True:
            raise ValueError("builtin must be true")
        return value


def resolve_shape(
    node_id,
    node_type,
    *,
    shape_overrides=None,
    type_shapes=None,
    node_types=None,
    config_shapes=None,
    saved_shapes=None,
):
    """Resolve one whole selection. None definition means the legacy CustomNode.

    Saved definitions are supplied by the caller; a missing entry (including a
    tombstone omitted by the caller) never falls through to a lower priority.
    No library storage or graph mutation is performed here.
    """
    source = "builtin"
    selection = None
    for mapping, key, candidate in (
        (shape_overrides or {}, node_id, "node"),
        (type_shapes or {}, node_type, "session-type"),
    ):
        if key in mapping:
            source, selection = candidate, mapping[key]
            break
    else:
        name = (node_types or {}).get(node_type, {}).get("shape")
        if name is not None:
            source, selection = "config-type", {"ref": f"config:{name}"}
    result = {"definition": None, "source": source, "diagnostic": None}
    if source == "builtin":
        return result
    try:
        selected = ShapeSelection.model_validate(selection)
        if selected.builtin:
            return result
        definition = selected.inline
        if selected.ref:
            namespace, name = selected.ref.split(":", 1)
            definitions = config_shapes if namespace == "config" else saved_shapes
            raw = (definitions or {}).get(name)
            if raw is None:
                result["diagnostic"] = {"code": "missing_shape", "ref": selected.ref}
                return result
            definition = ShapeDefinition.model_validate(raw)
        result["definition"] = definition.model_dump()
    except (ValidationError, ValueError, TypeError):
        result["diagnostic"] = {"code": "invalid_shape", "source": source}
    return result


def sanitize_shapes(raw):
    """Isolate malformed external definitions and expose structured diagnostics."""
    from pydantic import TypeAdapter

    valid, diagnostics = {}, []
    definitions = raw.get("node_shapes", {})
    if not isinstance(definitions, dict):
        definitions = {}
        diagnostics.append({"code": "invalid_shape_map"})
    for name, definition in definitions.items():
        try:
            TypeAdapter(ShapeName).validate_python(name)
            valid[name] = ShapeDefinition.model_validate(definition).model_dump()
        except (ValidationError, TypeError):
            diagnostics.append({"code": "invalid_shape", "name": name})
    raw["node_shapes"] = valid
    schema = raw.get("schema")
    types = schema.get("node_types", {}) if isinstance(schema, dict) else {}
    types = types if isinstance(types, dict) else {}
    for name, config in types.items():
        if not isinstance(config, dict):
            continue
        ref = config.get("shape")
        if ref is not None and (not isinstance(ref, str) or ref not in valid):
            diagnostics.append({"code": "missing_shape", "node_type": name})
            config.pop("shape", None)
    return diagnostics


def resolve_bindings(definition, node, declared_fields):
    """Read only declared scalar attributes; missing or unsafe values stay empty."""
    values, diagnostics = {}, []
    allowed = {"type", "name", "summary", *declared_fields} - {"metadata"}
    for area, binding in definition.areas.model_dump().items():
        value = node.get(binding) if binding in allowed else None
        if binding is None:
            values[area] = ""
        elif value is None or type(value) not in (str, int, float, bool):
            values[area] = ""
            diagnostics.append(
                {"code": "unavailable_binding", "area": area, "field": binding}
            )
        else:
            values[area] = value
    return {"values": values, "diagnostics": diagnostics}
