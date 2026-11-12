# covers: backend/config/config_loader.py
"""Shape transport isolation guarantees for config_loader."""

import json

import pytest
from pydantic import ValidationError


def test_shape_transport_isolates_external_errors(tmp_path, monkeypatch):
    """Malformed shapes cannot discard valid schema or shape transport."""
    from backend.config import config_loader as config

    definition = {
        "version": 1,
        "base": "rectangle",
        "scale": 1,
        "placement": "top-left",
        "areas": {"first": "type", "second": "name", "third": None},
    }
    raw = {
        "node_shapes": {"card": definition, "broken": {"version": 2}},
        "schema": {
            "node_types": {"Risk": {"shape": "card"}, "Other": {"shape": "broken"}}
        },
    }
    with pytest.raises(ValidationError):
        config.SchemaFileConfig.model_validate(raw)
    path = tmp_path / "schema.json"
    path.write_text(json.dumps(raw))
    monkeypatch.setenv("SCHEMA_FILE", str(path))
    config.reset_loader()
    try:
        transported = config.get_schema()
        assert transported["node_shapes"] == {"card": definition}
        assert transported["node_types"]["Risk"]["shape"] == "card"
        assert "Other" in transported["node_types"]
        assert "shape" not in transported["node_types"]["Skill"]
        assert len(transported["shape_diagnostics"]) == 2
    finally:
        config.reset_loader()
