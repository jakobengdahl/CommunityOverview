"""Runtime hooks and model providers must support independent startup imports."""

from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.parametrize(
    "module",
    ["backend.runtime.extensions", "backend.llm.llm_providers"],
)
def test_runtime_import_before_core(module):
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            f"import {module}; "
            "from backend.runtime.extensions import MutationPolicyDenied; "
            "from backend.core import GraphWriteFailed; "
            "from backend.core.storage_backends import GraphWriteFailed as BackendError; "
            "assert BackendError is GraphWriteFailed; "
            "assert issubclass(MutationPolicyDenied, GraphWriteFailed)",
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
