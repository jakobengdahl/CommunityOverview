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
# host's processes and CPU on them.
MAX_CONCURRENT_CHECKS = 4
_slots = threading.BoundedSemaphore(MAX_CONCURRENT_CHECKS)

# The child reads {"schema", "instance"} on standard input and writes the first
# problem's message, or nothing. It limits itself before reading anything.
_CHILD = f"""
import json, resource, sys
memory = {CHECK_MEMORY_MIB} * 1024 * 1024
resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
resource.setrlimit(resource.RLIMIT_CPU, ({int(CHECK_TIMEOUT_SECONDS)}, {int(CHECK_TIMEOUT_SECONDS) + 1}))
from jsonschema import Draft202012Validator
from referencing import Registry
try:
    request = json.loads(sys.stdin.buffer.read())
    # An empty registry: a $ref outside the schema itself resolves to nothing
    # instead of being fetched from the network.
    validator = Draft202012Validator(request["schema"], registry=Registry())
    error = next(iter(validator.iter_errors(request["instance"])), None)
    message = error.message if error is not None else ""
except RecursionError:
    message = "the value is nested too deeply to check"
sys.stdout.write(message[:{MAX_PROBLEM_CHARS}])
"""


class SchemaCheckFailed(Exception):
    """The check could not finish: too slow, too large, or a broken schema."""


class SchemaCheckBusy(Exception):
    """Every check slot is taken; the caller should try again shortly."""


def schema_problem(schema: Any, instance: Any, *, wait: bool = True) -> Optional[str]:
    """The first way ``instance`` breaks ``schema``, or None when it conforms.

    Raises SchemaCheckFailed when the check itself cannot be completed within
    its limits; callers treat that as the schema not being applicable. With
    ``wait=False`` it raises SchemaCheckBusy instead of waiting for a slot.
    """
    if not _slots.acquire(blocking=wait):
        raise SchemaCheckBusy("too many schema checks are running")
    try:
        return _check(schema, instance)
    finally:
        _slots.release()


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
