"""Invariants of function packages and manifests (USER_FUNCTION_CONTRACT §4)."""

import json
import os

import pytest

from backend.functions.manifest import (
    MAX_FILES,
    list_function_directories,
    load_function,
    validate_manifest,
)
from backend.functions.settings import FunctionsSettings, FunctionsSettingsError

VALID = {
    "contract_version": "1",
    "id": "count-nodes",
    "version": "1.0.0",
    "name": "Count nodes",
    "runtime": "python",
    "entry_point": "main.py",
    "triggers": [{"kind": "manual"}],
    "grants": ["graph:read"],
}
FILES = {"main.py": b"", "function.json": b"{}"}


def errors_for(**changes):
    document = {**VALID, **changes}
    for key, value in changes.items():
        if value is ...:
            document.pop(key)
    return validate_manifest(document, "count-nodes", FILES)[1]


def test_a_complete_manifest_is_valid():
    manifest, errors = validate_manifest(
        dict(
            VALID,
            description="d",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            write_target="graph",
            limits={"timeout_seconds": 60, "memory_mib": 128, "max_tool_calls": 10},
            x_tooling_annotation={"ignored": True},
        ),
        "count-nodes",
        FILES,
    )
    assert errors == []
    assert manifest.limits == {
        "timeout_seconds": 60,
        "memory_mib": 128,
        "max_tool_calls": 10,
    }
    assert manifest.grants == ("graph:read",)


@pytest.mark.parametrize(
    "field",
    [
        "contract_version",
        "id",
        "version",
        "name",
        "runtime",
        "entry_point",
        "triggers",
        "grants",
    ],
)
def test_each_required_field_is_required(field):
    assert any(field in e for e in errors_for(**{field: ...}))


@pytest.mark.parametrize(
    "changes",
    [
        {"contract_version": "2"},
        {"contract_version": 1},
        {"id": "Count_Nodes"},
        {"id": "ab"},
        {"id": "other-name"},
        {"id": "runs"},
        {"id": "runner"},
        {"runtime": "ruby"},
        {"entry_point": "../main.py"},
        {"entry_point": "/main.py"},
        {"entry_point": "missing.py"},
        {"entry_point": "function.json"},
        {"triggers": [{"kind": "schedule"}]},
        {"triggers": [{"kind": "manual"}, {"kind": "manual"}]},
        {"triggers": []},
        {"grants": ["graph:delete"]},
        {"grants": ["graph:read", "graph:read"]},
        {"grants": "graph:read"},
        {"write_target": "session"},
        {"limits": {"timeout_seconds": 121}},
        {"limits": {"memory_mib": 257}},
        {"limits": {"max_tool_calls": 201}},
        {"limits": {"timeout_seconds": 0}},
        {"limits": {"timeout_seconds": True}},
        {"limits": {"timeout_seconds": 1.5}},
        {"limits": {"cpu": 1}},
        {"limits": []},
        {"input_schema": {"type": "not-a-type"}},
        {"output_schema": []},
        {"retry": {}},
        {"secrets": []},
        {"egress": []},
        {"name": 5},
        {"description": ["x"]},
    ],
)
def test_a_manifest_outside_version_1_is_rejected(changes):
    assert errors_for(**changes)


def test_a_node_entry_point_must_be_javascript():
    _, errors = validate_manifest(dict(VALID, runtime="node"), "count-nodes", FILES)
    assert errors


def _write(root, name, files):
    directory = root / name
    directory.mkdir(parents=True)
    for relative, content in files.items():
        path = directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    return directory


def test_a_package_is_read_with_all_its_files_as_content(tmp_path):
    _write(
        tmp_path,
        "count-nodes",
        {
            "function.json": json.dumps(VALID).encode(),
            "main.py": b"print(1)\n",
            "lib/util.py": b"X = 1\n",
            "__pycache__/main.cpython-311.pyc": b"cache",
        },
    )
    loaded = load_function(str(tmp_path), "count-nodes")
    assert loaded.valid, loaded.errors
    assert set(loaded.files) == {"function.json", "main.py", "lib/util.py"}
    assert loaded.files["lib/util.py"] == b"X = 1\n"


def test_a_symbolic_link_in_a_package_makes_it_invalid(tmp_path):
    directory = _write(
        tmp_path,
        "count-nodes",
        {"function.json": json.dumps(VALID).encode(), "main.py": b""},
    )
    os.symlink("/etc/passwd", directory / "secret.txt")
    loaded = load_function(str(tmp_path), "count-nodes")
    assert not loaded.valid
    assert "symbolic links" in loaded.errors[0]


def test_a_linked_directory_is_not_a_function(tmp_path):
    real = _write(tmp_path / "elsewhere", "count-nodes", {"main.py": b""})
    (tmp_path / "root").mkdir()
    os.symlink(real, tmp_path / "root" / "count-nodes")
    assert list_function_directories(str(tmp_path / "root")) == []


def test_a_package_with_too_many_files_is_invalid(tmp_path):
    files = {f"f{i}.py": b"" for i in range(MAX_FILES + 1)}
    files["function.json"] = json.dumps(VALID).encode()
    files["main.py"] = b""
    _write(tmp_path, "count-nodes", files)
    assert not load_function(str(tmp_path), "count-nodes").valid


@pytest.mark.parametrize("content", [b"{not json", b"[]", b"\xff\xfe"])
def test_a_manifest_that_is_not_a_json_object_is_invalid(tmp_path, content):
    _write(tmp_path, "count-nodes", {"function.json": content, "main.py": b""})
    assert not load_function(str(tmp_path), "count-nodes").valid


def test_a_missing_root_lists_no_functions(tmp_path):
    assert list_function_directories(str(tmp_path / "absent")) == []


def test_hidden_directories_are_not_functions(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "real-one").mkdir()
    assert list_function_directories(str(tmp_path)) == ["real-one"]


# --- instance settings (§3.1, §4.4, §8) ------------------------------------------


def test_settings_enable_functions_and_grants_explicitly():
    settings = FunctionsSettings.from_env(
        {"FUNCTIONS_ENABLED": '{"count-nodes": ["graph:read"]}'}
    )
    assert settings.is_enabled("count-nodes")
    assert settings.grant_enabled("count-nodes", "graph:read")
    assert not settings.grant_enabled("count-nodes", "graph:write")
    assert not settings.is_enabled("other")


def test_settings_lower_limits_but_never_raise_them():
    settings = FunctionsSettings.from_env(
        {
            "FUNCTIONS_TIMEOUT_SECONDS": "30",
            "FUNCTIONS_MEMORY_MIB": "4096",
            "FUNCTIONS_MAX_TOOL_CALLS": "500",
        }
    )
    assert (settings.timeout_seconds, settings.memory_mib, settings.max_tool_calls) == (
        30,
        256,
        200,
    )


@pytest.mark.parametrize(
    "environ",
    [
        {"FUNCTIONS_ENABLED": "not json"},
        {"FUNCTIONS_ENABLED": '["count-nodes"]'},
        {"FUNCTIONS_ENABLED": '{"count-nodes": "graph:read"}'},
        {"FUNCTIONS_ISOLATION_MINIMUM": "strict"},
        {"FUNCTIONS_TIMEOUT_SECONDS": "0"},
        {"FUNCTIONS_TIMEOUT_SECONDS": "ten"},
    ],
)
def test_malformed_settings_are_refused(environ):
    with pytest.raises(FunctionsSettingsError):
        FunctionsSettings.from_env(environ)


def test_by_default_nothing_is_enabled_and_degraded_runs_are_allowed():
    settings = FunctionsSettings.from_env({})
    assert settings.enabled == {}
    assert settings.isolation_minimum == "degraded"


def test_a_fifo_in_a_package_makes_it_invalid_without_blocking(tmp_path):
    directory = _write(
        tmp_path,
        "count-nodes",
        {"function.json": json.dumps(VALID).encode(), "main.py": b""},
    )
    os.mkfifo(directory / "pipe")
    loaded = load_function(str(tmp_path), "count-nodes")
    assert not loaded.valid
    assert "regular files" in loaded.errors[0]
