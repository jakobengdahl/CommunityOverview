"""Regression tests for the repository Ruff file-selection config."""

import subprocess
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PROBE = REPO_ROOT / "config" / "_ruff_exclude_probe.py"


def ruff_show_files(*paths: str) -> set[Path]:
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--show-files", *paths],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return {
        Path(line).resolve().relative_to(REPO_ROOT)
        for line in result.stdout.splitlines()
        if line
    }


def test_ruff_lints_backend_config_but_excludes_top_level_config():
    CONFIG_PROBE.write_text("PROBE = True\n")
    try:
        backend_files = ruff_show_files("backend")
        all_files = ruff_show_files(".")
    finally:
        CONFIG_PROBE.unlink(missing_ok=True)

    assert Path("backend/config/config_loader.py") in backend_files
    assert Path("config/_ruff_exclude_probe.py") not in all_files
