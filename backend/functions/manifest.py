"""
Function packages and their manifests (docs/USER_FUNCTION_CONTRACT.md §4).

``load_function`` reads one function directory: its ``function.json`` and its
source files, as content. A manifest that breaks §4.3 is returned with the
reasons, never raised, so that an operator can list a broken function with its
validation state. The host validates again before every run.
"""

from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from backend.functions.broker import GRANT_GRAPH_READ, GRANT_GRAPH_WRITE
from backend.functions.runner import (
    MAX_MEMORY_MIB,
    MAX_TIMEOUT_SECONDS,
    RUNTIME_PYTHON,
    safe_relative_path,
)
from backend.functions.broker import MAX_TOOL_CALLS

MANIFEST_FILE = "function.json"
SUPPORTED_CONTRACT_VERSIONS = ("1",)
KNOWN_GRANTS = (GRANT_GRAPH_READ, GRANT_GRAPH_WRITE)
KNOWN_RUNTIMES = (RUNTIME_PYTHON, "node")
RESERVED_FIELDS = ("retry", "secrets", "egress")

# Contract maximums for the per-function ``limits`` block (§8).
LIMIT_MAXIMUMS = {
    "timeout_seconds": MAX_TIMEOUT_SECONDS,
    "memory_mib": MAX_MEMORY_MIB,
    "max_tool_calls": MAX_TOOL_CALLS,
}

# A function is a few source files, not a project: bound what the host reads.
MAX_FILES = 200
MAX_PACKAGE_BYTES = 5 * 1024 * 1024
MAX_MANIFEST_BYTES = 256 * 1024

_ID_PATTERN = re.compile(r"[a-z0-9-]{3,64}")
# Path segments the /functions API uses for itself.
RESERVED_IDS = frozenset({"runs", "runner"})
_ENTRY_SUFFIXES = {"python": (".py",), "node": (".js", ".mjs")}


@dataclass(frozen=True)
class Manifest:
    contract_version: str
    id: str
    version: str
    name: str
    runtime: str
    entry_point: str
    grants: Tuple[str, ...]
    description: str = ""
    input_schema: Optional[Dict[str, Any]] = None
    output_schema: Optional[Dict[str, Any]] = None
    limits: Mapping[str, int] = field(default_factory=dict)


@dataclass
class LoadedFunction:
    """A function directory as read: the manifest if it is valid, else errors."""

    directory_name: str
    manifest: Optional[Manifest] = None
    files: Dict[str, bytes] = field(default_factory=dict)
    errors: List[str] = field(default_factory=list)

    @property
    def valid(self) -> bool:
        return self.manifest is not None and not self.errors


def load_function(root: str, directory_name: str) -> LoadedFunction:
    """Read and validate ``root/directory_name`` (§4.1–§4.3)."""
    loaded = LoadedFunction(directory_name=directory_name)
    directory = os.path.join(root, directory_name)
    try:
        loaded.files = _read_package(directory)
    except ValueError as exc:
        loaded.errors.append(str(exc))
        return loaded
    except OSError as exc:
        # The message, not the host path, is what a reader of the list sees.
        loaded.errors.append(f"the package could not be read: {exc.strerror}")
        return loaded
    raw = loaded.files.get(MANIFEST_FILE)
    if raw is None:
        loaded.errors.append(f"{MANIFEST_FILE} is missing")
        return loaded
    if len(raw) > MAX_MANIFEST_BYTES:
        loaded.errors.append(
            f"{MANIFEST_FILE} is larger than {MAX_MANIFEST_BYTES} bytes"
        )
        return loaded
    try:
        document = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        loaded.errors.append(f"{MANIFEST_FILE} is not valid JSON: {exc}")
        return loaded
    manifest, errors = validate_manifest(document, directory_name, loaded.files)
    loaded.errors.extend(errors)
    if not errors:
        loaded.manifest = manifest
    return loaded


def list_function_directories(root: str) -> List[str]:
    try:
        entries = sorted(os.listdir(root))
    except FileNotFoundError:
        return []
    return [
        name
        for name in entries
        if not name.startswith(".")
        and os.path.isdir(os.path.join(root, name))
        and not os.path.islink(os.path.join(root, name))
    ]


def _read_package(directory: str) -> Dict[str, bytes]:
    """Every regular file under ``directory``, keyed by its relative path.

    Symbolic links are refused rather than followed: a package can only hold
    what is inside its own directory.
    """
    if os.path.islink(directory) or not os.path.isdir(directory):
        raise ValueError("the function directory does not exist")
    files: Dict[str, bytes] = {}
    total = 0
    for current, dirnames, filenames in os.walk(directory, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for dirname in dirnames:
            if os.path.islink(os.path.join(current, dirname)):
                raise ValueError(f"symbolic links are not allowed: {dirname}")
        for filename in sorted(filenames):
            path = os.path.join(current, filename)
            relative = os.path.relpath(path, directory).replace(os.sep, "/")
            if len(files) >= MAX_FILES:
                raise ValueError(f"a function may hold at most {MAX_FILES} files")
            content = _read_regular_file(path, relative, MAX_PACKAGE_BYTES - total + 1)
            total += len(content)
            if total > MAX_PACKAGE_BYTES:
                raise ValueError(
                    f"a function may hold at most {MAX_PACKAGE_BYTES} bytes"
                )
            files[safe_relative_path(relative)] = content
    return files


def _read_regular_file(path: str, relative: str, limit: int) -> bytes:
    """Read a regular file without following a link or blocking on a FIFO,
    checked on the descriptor itself so the file cannot be swapped between
    the check and the read."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise ValueError(f"only regular files are allowed: {relative}") from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError(f"only regular files are allowed: {relative}")
        chunks = []
        remaining = limit
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def _expect(document, name, kind, errors, *, required=True):
    if name not in document:
        if required:
            errors.append(f"{name} is required")
        return None
    value = document[name]
    if not isinstance(value, kind) or isinstance(value, bool):
        errors.append(f"{name} has the wrong type")
        return None
    return value


def _check_schema(name, value, errors) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if not isinstance(value, dict):
        errors.append(f"{name} must be a JSON Schema object")
        return None
    try:
        Draft202012Validator.check_schema(value)
    except SchemaError as exc:
        errors.append(f"{name} is not a valid JSON Schema: {exc.message}")
        return None
    return value


def validate_manifest(
    document: Any, directory_name: str, files: Mapping[str, bytes]
) -> Tuple[Optional[Manifest], List[str]]:
    """Apply §4.3 to a parsed ``function.json``; returns the manifest or errors."""
    errors: List[str] = []
    if not isinstance(document, dict):
        return None, [f"{MANIFEST_FILE} must hold a JSON object"]

    contract_version = _expect(document, "contract_version", str, errors)
    if (
        contract_version is not None
        and contract_version not in SUPPORTED_CONTRACT_VERSIONS
    ):
        errors.append(f"contract_version {contract_version!r} is not supported")

    function_id = _expect(document, "id", str, errors)
    if function_id is not None:
        if not _ID_PATTERN.fullmatch(function_id):
            errors.append("id must be 3-64 lowercase letters, digits or '-'")
        elif function_id in RESERVED_IDS:
            errors.append(f"id {function_id!r} is reserved")
        elif function_id != directory_name:
            errors.append("id must equal the directory name")

    version = _expect(document, "version", str, errors)
    name = _expect(document, "name", str, errors)
    description = _expect(document, "description", str, errors, required=False)

    runtime = _expect(document, "runtime", str, errors)
    if runtime is not None and runtime not in KNOWN_RUNTIMES:
        errors.append(f"runtime {runtime!r} is not one of {', '.join(KNOWN_RUNTIMES)}")

    entry_point = _expect(document, "entry_point", str, errors)
    if entry_point is not None:
        try:
            entry_point = safe_relative_path(entry_point)
        except ValueError:
            errors.append("entry_point must stay inside the function directory")
            entry_point = None
    if entry_point is not None:
        if entry_point not in files or entry_point == MANIFEST_FILE:
            errors.append("entry_point is not a file of the function")
        elif runtime in _ENTRY_SUFFIXES and not entry_point.endswith(
            _ENTRY_SUFFIXES[runtime]
        ):
            errors.append(f"entry_point does not fit the {runtime} runtime")

    input_schema = _check_schema("input_schema", document.get("input_schema"), errors)
    output_schema = _check_schema(
        "output_schema", document.get("output_schema"), errors
    )

    triggers = _expect(document, "triggers", list, errors)
    if triggers is not None and triggers != [{"kind": "manual"}]:
        errors.append('triggers must be exactly [{"kind": "manual"}] in version 1')

    grants = _expect(document, "grants", list, errors)
    if grants is not None:
        unknown = [g for g in grants if g not in KNOWN_GRANTS]
        if unknown:
            errors.append(f"unknown grants: {unknown!r}")
        if len(set(map(str, grants))) != len(grants):
            errors.append("grants must not repeat")

    if "write_target" in document and document["write_target"] != "graph":
        errors.append('write_target must be "graph" in version 1')

    limits = _expect(document, "limits", dict, errors, required=False) or {}
    for key, value in limits.items():
        maximum = LIMIT_MAXIMUMS.get(key)
        if maximum is None:
            errors.append(f"limits.{key} is not a known limit")
        elif isinstance(value, bool) or not isinstance(value, int):
            errors.append(f"limits.{key} must be an integer")
        elif not 0 < value <= maximum:
            errors.append(f"limits.{key} must be above 0 and at most {maximum}")

    for reserved in RESERVED_FIELDS:
        if reserved in document:
            errors.append(f"{reserved} is reserved for a later contract version")

    if errors:
        return None, errors
    return (
        Manifest(
            contract_version=contract_version,
            id=function_id,
            version=version,
            name=name,
            description=description or "",
            runtime=runtime,
            entry_point=entry_point,
            input_schema=input_schema,
            output_schema=output_schema,
            grants=tuple(grants),
            limits=dict(limits),
        ),
        [],
    )
