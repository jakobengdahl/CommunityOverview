#!/usr/bin/env python3
"""Check that every test file a branch changes is at most 1.5x what it covers.

The whole file counts, not the lines the branch added, and unchanged files are
never reported. A test file covers the module of the same name (``tests/test_x.py``
covers ``x.py`` one directory up; ``x.test.jsx`` covers ``x.js`` or ``x.jsx`` beside
it). Any other test file names its modules, repo-relative, in a header comment
within its first 10 lines; several modules count together:

    # covers: backend/core/storage.py, backend/core/storage_backends.py

Usage: check_test_ratio.py [--base REF]

Exit status: 0 within the limit, 1 a file is over it or covers nothing, 2 git failed.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

MAX_RATIO = 1.5
HEADER_LINES = 10
COVERS_HEADER = re.compile(r"^(?:#|//)\s*covers:\s*(.+)$")
PY_TEST = re.compile(r"^(?P<dir>(?:.*/)?)tests/test_(?P<name>[^/]+)\.py$")
JS_TEST = re.compile(r"^(?P<stem>.+)\.test\.[jt]sx?$")
JS_SUFFIXES = (".js", ".jsx", ".ts", ".tsx")


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True
    ).stdout


def _is_test_file(path: str) -> bool:
    return bool(PY_TEST.match(path) or JS_TEST.match(path))


def _line_count(path: Path) -> int:
    return len(path.read_bytes().splitlines())


def _name_match(root: Path, test_path: str) -> List[str]:
    py = PY_TEST.match(test_path)
    if py:
        candidates = [f"{py['dir']}{py['name']}.py"]
    else:
        stem = JS_TEST.match(test_path)["stem"]
        candidates = [stem + suffix for suffix in JS_SUFFIXES]
    return [path for path in candidates if (root / path).is_file()]


def _covers_header(root: Path, test_path: str) -> Optional[List[str]]:
    text = (root / test_path).read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines()[:HEADER_LINES]:
        match = COVERS_HEADER.match(line.strip())
        if match:
            return [part.strip() for part in match[1].split(",") if part.strip()]
    return None


def check_file(root: Path, test_path: str) -> Optional[str]:
    """Return what is wrong with one test file, or None when it is within the limit."""
    covered = _covers_header(root, test_path)
    if covered is None:
        covered = _name_match(root, test_path)
        if not covered:
            return f"{test_path}: no module of the same name; add a '# covers: <path>' header"
    missing = [path for path in covered if not (root / path).is_file()]
    if missing:
        return f"{test_path}: covers header names missing files: {', '.join(missing)}"
    test_lines = _line_count(root / test_path)
    module_lines = sum(_line_count(root / path) for path in covered)
    if test_lines > MAX_RATIO * module_lines:
        return (
            f"{test_path}: {test_lines} lines against {module_lines} in "
            f"{', '.join(covered)}; the limit is {MAX_RATIO}x"
        )
    return None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="origin/main", help="ref to compare against")
    args = parser.parse_args(argv)

    try:
        root = Path(_git("rev-parse", "--show-toplevel").strip())
        changed = _git(
            "diff", "--name-only", "--diff-filter=d", "-z", f"{args.base}...HEAD"
        )
    except subprocess.CalledProcessError as exc:
        print(f"git failed: {exc.stderr.strip()}", file=sys.stderr)
        return 2

    problems = [
        problem
        for path in changed.split("\0")
        if _is_test_file(path) and (problem := check_file(root, path))
    ]
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
