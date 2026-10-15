"""
Bounded JSON Schema checks for function input and output
(docs/USER_FUNCTION_CONTRACT.md §4.2, §11).

The data a schema is checked against comes from callers and from function
processes, and some schemas cost far more than their size suggests (nested
``$ref`` fan-out, ``uniqueItems`` over objects, backtracking ``pattern``s). So
the check runs in a short-lived child process with a CPU, memory and wall-clock
limit, never in the host process.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from typing import Any, Optional

CHECK_TIMEOUT_SECONDS = 5.0
CHECK_MEMORY_MIB = 512
MAX_PROBLEM_CHARS = 500
# At most this many checks run at once, so that callers cannot spend the
# host's processes and CPU on them. Output checks have slots of their own, so
# callers' input checks can never hold back a run that is finishing.
MAX_INPUT_CHECKS = 3
MAX_OUTPUT_CHECKS = 2
_input_slots = threading.BoundedSemaphore(MAX_INPUT_CHECKS)
_output_slots = threading.BoundedSemaphore(MAX_OUTPUT_CHECKS)
_TOO_DEEP = "\x00too-deep"
_ENDLESS = "\x00endless"


# The child reads {"schema", "instance"} on standard input and writes the first
# problem's message, or nothing. It limits itself before reading anything.
# When the check recurses too deeply, the schema is at fault if the value is
# shallow, or if a trivial value recurses too; otherwise the value is too deep.
# A plain recursive schema reaches Python's recursion limit only for values
# about 250 levels deep (one with long $ref chains per level, sooner), so
# recursing on a value this shallow means the schema, not the value, is too
# deep to check: either it never ends or it costs dozens of frames per level.
SHALLOW_LEVELS = 16
_CHILD = f"""
import json, resource, sys
memory = {CHECK_MEMORY_MIB} * 1024 * 1024
resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
resource.setrlimit(resource.RLIMIT_CPU, ({int(CHECK_TIMEOUT_SECONDS)}, {int(CHECK_TIMEOUT_SECONDS) + 1}))
from jsonschema import Draft202012Validator
from referencing import Registry

def shallow(value):
    level = [value]
    for _ in range({SHALLOW_LEVELS}):
        children = []
        for item in level:
            if isinstance(item, dict):
                children.extend(item.values())
            elif isinstance(item, list):
                children.extend(item)
        if not children:
            return True
        level = children
    return False

def first_problem(validator, instance):
    error = next(iter(validator.iter_errors(instance)), None)
    return error.message if error is not None else ""

validator = None
try:
    request = json.loads(sys.stdin.buffer.read())
    # An empty registry: a $ref outside the schema itself resolves to nothing
    # instead of being fetched from the network.
    validator = Draft202012Validator(request["schema"], registry=Registry())
    message = first_problem(validator, request["instance"])
except RecursionError:
    message = {_TOO_DEEP!r}
    if validator is not None and shallow(request["instance"]):
        message = {_ENDLESS!r}
    elif validator is not None:
        for probe in (None, {{}}, [], "", 0):
            try:
                first_problem(validator, probe)
            except RecursionError:
                message = {_ENDLESS!r}
                break
sys.stdout.buffer.write(message[:{MAX_PROBLEM_CHARS}].encode("utf-8", "replace"))
"""


class SchemaCheckFailed(Exception):
    """The check could not finish: too slow, too large, or a broken schema."""


class SchemaCheckBusy(Exception):
    """Every check slot is taken; the caller should try again shortly."""


def schema_problem(
    schema: Any, instance: Any, *, output: bool = False
) -> Optional[str]:
    """The first way ``instance`` breaks ``schema``, or None when it conforms.

    Raises SchemaCheckFailed when the check itself cannot be completed within
    its limits; callers treat that as the schema not being applicable. An
    input check raises SchemaCheckBusy when every input slot is taken; an
    output check waits for its own slot.
    """
    slots = _output_slots if output else _input_slots
    if not slots.acquire(blocking=output):
        raise SchemaCheckBusy("too many schema checks are running")
    try:
        problem = _check(schema, instance)
    finally:
        slots.release()
    if problem == _ENDLESS:
        raise SchemaCheckFailed(
            "the schema refers to itself without end or nests too deeply"
        )
    if problem == _TOO_DEEP:
        return "the value is nested too deeply to check"
    return problem


def depth_below(value: Any, limit: int) -> bool:
    """Whether ``value`` nests fewer than ``limit`` levels, without recursion."""
    level = [value]
    for _ in range(limit):
        children = []
        for item in level:
            if isinstance(item, dict):
                children.extend(item.values())
            elif isinstance(item, list):
                children.extend(item)
        if not children:
            return True
        level = children
    return False


def is_utf8(value: Any) -> bool:
    """Whether a JSON value's text can be encoded as UTF-8: JSON escapes can
    carry lone surrogates, which no stored record or response can hold."""
    try:
        json.dumps(value, ensure_ascii=False).encode("utf-8")
    except (UnicodeEncodeError, ValueError, TypeError, RecursionError):
        return False
    return True


def _check(schema: Any, instance: Any) -> Optional[str]:
    payload = json.dumps({"schema": schema, "instance": instance}).encode("utf-8")
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-c", _CHILD],
            input=payload,
            capture_output=True,
            timeout=CHECK_TIMEOUT_SECONDS,
            env={},
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SchemaCheckFailed("the schema check exceeded its time limit") from exc
    if completed.returncode != 0:
        raise SchemaCheckFailed("the schema check could not be completed")
    problem = completed.stdout.decode("utf-8", "replace")
    return problem or None
