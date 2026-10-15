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
from typing import Any, Optional

CHECK_TIMEOUT_SECONDS = 5.0
CHECK_MEMORY_MIB = 512
MAX_PROBLEM_CHARS = 500

# The child reads {"schema", "instance"} on standard input and writes the first
# problem's message, or nothing. It limits itself before reading anything.
_CHILD = f"""
import json, resource, sys
memory = {CHECK_MEMORY_MIB} * 1024 * 1024
resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
resource.setrlimit(resource.RLIMIT_CPU, ({int(CHECK_TIMEOUT_SECONDS)}, {int(CHECK_TIMEOUT_SECONDS) + 1}))
from jsonschema import Draft202012Validator
request = json.loads(sys.stdin.buffer.read())
error = next(iter(Draft202012Validator(request["schema"]).iter_errors(request["instance"])), None)
sys.stdout.write(error.message[:{MAX_PROBLEM_CHARS}] if error is not None else "")
"""


class SchemaCheckFailed(Exception):
    """The check could not finish: too slow, too large, or a broken schema."""


def schema_problem(schema: Any, instance: Any) -> Optional[str]:
    """The first way ``instance`` breaks ``schema``, or None when it conforms.

    Raises SchemaCheckFailed when the check itself cannot be completed within
    its limits; callers treat that as the schema not being applicable.
    """
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
