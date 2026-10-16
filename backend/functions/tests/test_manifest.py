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


def test_a_folder_swapped_for_a_link_while_the_package_is_read_is_refused(
    tmp_path, monkeypatch
):
    import backend.functions.manifest as manifest_module

    directory = _write(
        tmp_path / "root",
        "count-nodes",
        {"function.json": json.dumps(VALID).encode(), "main.py": b""},
    )
    (directory / "a" / "b").mkdir(parents=True)
    outside = tmp_path / "outside"
    (outside / "b").mkdir(parents=True)
    (outside / "b" / "secret.txt").write_bytes(b"host secret")
    open_folder = manifest_module._open_folder

    def swap_then_open(path):
        if path.endswith(os.path.join("a", "b")):
            os.rename(directory / "a", tmp_path / "a-moved")
            os.symlink(outside, directory / "a")
        return open_folder(path)

    monkeypatch.setattr(manifest_module, "_open_folder", swap_then_open)
    loaded = load_function(str(tmp_path / "root"), "count-nodes")
    assert not loaded.valid
    assert "changed while it was read" in loaded.errors[0]
    assert not any(b"host secret" in content for content in loaded.files.values())


def test_a_manifest_with_text_that_is_not_unicode_is_invalid(tmp_path):
    _write(
        tmp_path,
        "count-nodes",
        {
            "function.json": json.dumps(dict(VALID, version="1\ud800")).encode(),
            "main.py": b"",
        },
    )
    loaded = load_function(str(tmp_path), "count-nodes")
    assert not loaded.valid
    assert "not valid Unicode" in loaded.errors[0]


@pytest.mark.parametrize(
    "schema",
    [
        {
            "properties": {"": {"const": {"$ref": "https://x/s"}}},
            "$ref": "#/properties//const",
        },
        {"properties": {"a": {"properties": {}}}, "$ref": "#/properties/a/required"},
        {"patternProperties": {"x": {"$ref": "https://x/s"}}},
        {"dependentSchemas": {"x": {"$ref": "https://x/s"}}},
        {"$recursiveRef": "https://x/s"},
    ],
)
def test_references_that_leave_the_schema_or_point_into_data_are_refused(schema):
    assert errors_for(input_schema=schema)


def test_a_reference_to_a_plain_name_anchor_is_allowed():
    schema = {
        "$defs": {"n": {"$anchor": "required", "type": "integer"}},
        "$ref": "#required",
    }
    assert errors_for(input_schema=schema) == []


def test_the_digest_tells_paths_and_contents_apart_and_ignores_order():
    from backend.functions.manifest import package_digest

    assert package_digest({"a": b"bc"}) != package_digest({"ab": b"c"})
    assert package_digest({"a": b"x"}) != package_digest({"b": b"x"})
    assert package_digest({"a": b"1", "b": b"2"}) == package_digest(
        {"b": b"2", "a": b"1"}
    )


def test_a_package_may_hold_exactly_the_most_entries_and_no_more(tmp_path):
    from backend.functions.manifest import MAX_ENTRIES

    files = {"function.json": json.dumps(VALID).encode(), "main.py": b""}
    for index in range(MAX_FILES - 2):
        files[f"d{index}/f.py"] = b""
    directory = _write(tmp_path, "count-nodes", files)
    # Empty folders fill the rest: files and folders count alike.
    for index in range(MAX_ENTRIES - 2 * MAX_FILES + 2):
        (directory / f"empty{index}").mkdir()
    assert load_function(str(tmp_path), "count-nodes").valid
    (directory / "one-more").mkdir()
    loaded = load_function(str(tmp_path), "count-nodes")
    assert not loaded.valid
    assert "files and folders" in loaded.errors[0]


@pytest.mark.parametrize("number", ["NaN", "Infinity", "1e400"])
def test_a_manifest_with_a_number_json_cannot_represent_is_invalid(tmp_path, number):
    text = json.dumps(dict(VALID, input_schema={"maximum": 0}))
    text = text.replace('"maximum": 0', f'"maximum": {number}')
    _write(tmp_path, "count-nodes", {"function.json": text.encode(), "main.py": b""})
    loaded = load_function(str(tmp_path), "count-nodes")
    assert not loaded.valid
    assert "not valid JSON" in loaded.errors[0]


def _open_descriptors():
    return len(os.listdir("/proc/self/fd"))


def test_reading_a_package_reports_why_it_is_missing_and_leaks_no_descriptor(
    tmp_path,
):
    from backend.functions.manifest import read_package

    (tmp_path / "plain-file").write_bytes(b"")
    real = _write(tmp_path / "elsewhere", "count-nodes", {"main.py": b""})
    os.symlink(real, tmp_path / "linked")
    before = _open_descriptors()
    for name in ("missing", "plain-file", "linked"):
        loaded = read_package(str(tmp_path), name)
        assert loaded.errors == ["the function directory does not exist"], name
    directory = _write(
        tmp_path, "count-nodes", {"function.json": b"{}", "main.py": b"", "d/x": b""}
    )
    assert read_package(str(tmp_path), "count-nodes").errors == []
    os.symlink("/etc/passwd", directory / "d" / "y")
    assert read_package(str(tmp_path), "count-nodes").errors
    assert _open_descriptors() == before


def test_a_manifest_may_nest_exactly_to_the_limit(tmp_path):
    from backend.functions.manifest import MAX_MANIFEST_DEPTH

    def package(levels):
        # The manifest and input_schema are two levels; default holds the
        # rest as lists around an empty one.
        value = []
        for _ in range(levels - 3):
            value = [value]
        document = dict(VALID, input_schema={"type": "object", "default": value})
        return {"function.json": json.dumps(document).encode(), "main.py": b""}

    _write(tmp_path / "a", "count-nodes", package(MAX_MANIFEST_DEPTH))
    assert load_function(str(tmp_path / "a"), "count-nodes").valid
    _write(tmp_path / "b", "count-nodes", package(MAX_MANIFEST_DEPTH + 1))
    loaded = load_function(str(tmp_path / "b"), "count-nodes")
    assert "nests deeper than" in loaded.errors[0]


def test_names_beyond_ascii_are_allowed_and_names_outside_utf8_are_shown_escaped(
    tmp_path,
):
    files = {"function.json": json.dumps(VALID).encode(), "main.py": b"", "é.txt": b""}
    directory = _write(tmp_path, "count-nodes", files)
    assert load_function(str(tmp_path), "count-nodes").valid
    with open(os.fsencode(directory) + b"/y\xff.txt", "wb"):
        pass
    loaded = load_function(str(tmp_path), "count-nodes")
    assert loaded.errors == ["names must be valid UTF-8: y\\udcff.txt"]


@pytest.mark.parametrize("error", [PermissionError, OSError])
def test_a_package_that_cannot_be_read_names_the_reason_not_the_path(
    tmp_path, monkeypatch, error
):
    import errno

    import backend.functions.manifest as manifest_module
    from backend.functions.manifest import read_package

    def fail(directory):
        raise error(errno.EIO, "Input/output error", "/host/secret/path")

    monkeypatch.setattr(manifest_module, "_read_package", fail)
    assert read_package(str(tmp_path), "count-nodes").errors == [
        "the package could not be read: Input/output error"
    ]


def test_the_digest_cannot_be_fooled_by_names_or_contents_holding_separators():
    from backend.functions.manifest import package_digest

    pairs = [
        ({"a": b"1:b"}, {"a:1:b": b""}),
        ({"a": b":"}, {"a:": b""}),
        ({"ab": b""}, {"a": b"b"}),
        ({"a\udcff": b""}, {"a\udcfe": b""}),
    ]
    for first, second in pairs:
        assert package_digest(first) != package_digest(second), (first, second)


@pytest.mark.parametrize("version", ["1.0.0", "2.1.0-rc.1+build.5", "v" + "1" * 63])
def test_a_short_semantic_version_is_allowed(version):
    assert errors_for(version=version) == []


@pytest.mark.parametrize("version", ["", "9" * 65, "1.0 beta", "-1", "1/2"])
def test_a_long_or_odd_version_makes_the_manifest_invalid(version):
    assert errors_for(version=version)
