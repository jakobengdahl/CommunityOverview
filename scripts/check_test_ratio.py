#!/usr/bin/env python3
"""Check that the test files a branch changes stay within 1.5x what they cover.

Whole files count, not the lines the branch added: every test file covering the
same module counts towards its total, changed or not, but only changed files are
reported.

Only growth past the limit is reported, not historic size. A module already over the
limit at the merge base of ``--base`` and HEAD is printed for information and does
not fail; its base total applies HEAD's coverage to the base content, so adding a
header to an old file judges that file on its size before the branch. A module that
does not exist on base, or has no lines covered there, counts as within the limit.

A test file covers the modules, repo-relative, in a header comment within its first
10 lines; its lines count towards each in proportion to that module's lines, so a
file alone is held to 1.5x their summed lines and a header should name only what the
file really tests:

    # covers: backend/core/storage.py, backend/core/storage_backends.py

A module is a tracked code, shell, YAML or Dockerfile source that is neither a test
file nor under a ``tests/`` directory; a changed file whose header names anything
else, such as docs or data, fails.

Without a header, ``test_<name>.py`` covers the module matching the longest prefix
of ``<name>`` at a ``_`` boundary (``tests/test_x_guards.py`` covers ``x.py``), one
directory up from ``tests/`` and beside the test anywhere else; ``x.test.jsx``
covers ``x.js`` or ``x.jsx`` beside it. A header in the same place skips a file,
which is then left out of totals and printed when changed:

    # ratio-exempt: <reason>

Usage: check_test_ratio.py [--base REF]

Exit status: 0 no growth past the limit, 1 a file grew past it or covers nothing,
2 git failed.
"""

import argparse
import math
import posixpath
import re
import subprocess
import sys
from collections import defaultdict
from fractions import Fraction
from functools import lru_cache
from pathlib import Path
from typing import Callable, Dict, List, NamedTuple, Optional, Set

MAX_RATIO = 1.5
HEADER_LINES = 10
COVERS_HEADER = re.compile(r"^(?:#|//)\s*covers:\s*(.+)$")
EXEMPT_HEADER = re.compile(r"^(?:#|//)\s*ratio-exempt:\s*(.+)$")
PY_TEST = re.compile(r"^(?P<dir>(?:.*/)?)test_(?P<name>[^/]+)\.py$")
JS_TEST = re.compile(r"^(?P<stem>.+)\.test\.[jt]sx?$")
JS_SUFFIXES = (".js", ".jsx", ".ts", ".tsx")
MODULE_SUFFIXES = (".py", *JS_SUFFIXES, ".mjs", ".cjs", ".sh", ".yml", ".yaml")


class Overage(NamedTuple):
    path: str
    lines: int
    total_lines: int
    test_files: int
    module: str
    module_lines: int


class Report(NamedTuple):
    over: List[Overage]
    unresolved: Dict[str, str]
    exempt: Dict[str, str]
    over_on_base: List[Overage]


def _git_bytes(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


def _git(*args: str) -> str:
    return _git_bytes(*args).decode(errors="surrogateescape")


def _is_test_file(path: str) -> bool:
    return bool(PY_TEST.match(path) or JS_TEST.match(path))


def _line_count(path: Path) -> int:
    return len(path.read_bytes().splitlines())


def _is_module(root: Path, path: str, tracked: Set[str]) -> bool:
    return (
        path in tracked
        and (path.endswith(MODULE_SUFFIXES) or posixpath.basename(path) == "Dockerfile")
        and not _is_test_file(path)
        and "tests" not in path.split("/")[:-1]
        and (root / path).is_file()
    )


def _name_match(root: Path, test_path: str, tracked: Set[str]) -> List[str]:
    py = PY_TEST.match(test_path)
    if not py:
        stem = JS_TEST.match(test_path)["stem"]
        candidates = [stem + suffix for suffix in JS_SUFFIXES]
        return [path for path in candidates if _is_module(root, path, tracked)]
    directory = re.sub(r"(^|/)tests/$", r"\1", py["dir"])
    parts = py["name"].split("_")
    for end in range(len(parts), 0, -1):
        path = f"{directory}{'_'.join(parts[:end])}.py"
        if _is_module(root, path, tracked):
            return [path]
    return []


def _header(root: Path, test_path: str, pattern: re.Pattern) -> Optional[str]:
    text = (root / test_path).read_text(encoding="utf-8", errors="replace")
    for line in text.splitlines()[:HEADER_LINES]:
        match = pattern.match(line.strip())
        if match:
            return match[1].strip()
    return None


def _covered(root: Path, test_path: str, tracked: Set[str]) -> List[str]:
    header = _header(root, test_path, COVERS_HEADER) or ""
    named = [posixpath.normpath(p.strip()) for p in header.split(",") if p.strip()]
    return list(dict.fromkeys(named)) or _name_match(root, test_path, tracked)


def _total(
    module: str,
    tests: List[str],
    coverage: Dict[str, List[str]],
    line_count: Callable[[str], int],
) -> Fraction:
    total = Fraction()
    for test in tests:
        covered_lines = sum(line_count(other) for other in coverage[test])
        # An all-empty set of modules has no proportions; each takes the whole file.
        share = (
            Fraction(line_count(module), covered_lines)
            if covered_lines
            else Fraction(1)
        )
        total += line_count(test) * share
    return total


def _within_limit_on_base(
    module: str,
    tests: List[str],
    coverage: Dict[str, List[str]],
    base_lines: Callable[[str], Optional[int]],
) -> bool:
    module_lines = base_lines(module)
    if module_lines is None:
        return True
    total = _total(module, tests, coverage, lambda path: base_lines(path) or 0)
    return total <= Fraction(MAX_RATIO) * module_lines


def _base_line_reader(root: Path, ref: str) -> Callable[[str], Optional[int]]:
    """Return a line counter for files at the merge base; None for a file not there."""
    base = _git("merge-base", ref, "HEAD").strip()
    # ls-tree lists paths relative to the working directory unless run from the root.
    present = set(
        _git("-C", str(root), "ls-tree", "-rz", "--name-only", base).split("\0")
    )

    def base_lines(path: str) -> Optional[int]:
        if path not in present:
            return None
        return len(_git_bytes("-C", str(root), "show", f"{base}:{path}").splitlines())

    return base_lines


def check(
    root: Path,
    tracked: List[str],
    changed: Set[str],
    base_lines: Callable[[str], Optional[int]] = lambda path: None,
) -> Report:
    """Hold each module covered by a changed test file to the limit.

    A module ``base_lines`` shows was over the limit before goes to ``over_on_base``.
    """
    report = Report([], {}, {}, [])
    coverage: Dict[str, List[str]] = {}
    known = set(tracked)
    for path in tracked:
        if not _is_test_file(path) or not (root / path).is_file():
            continue
        reason = _header(root, path, EXEMPT_HEADER)
        if reason:
            if path in changed:
                report.exempt[path] = reason
            continue
        covered = _covered(root, path, known)
        modules = [module for module in covered if _is_module(root, module, known)]
        if modules:
            coverage[path] = modules
        if path not in changed:
            continue
        if not covered:
            report.unresolved[path] = (
                "no module matches its name; add a '# covers: <path>' header"
            )
        elif len(modules) < len(covered):
            rejected = sorted(set(covered) - set(modules))
            report.unresolved[path] = (
                f"covers header names non-modules: {', '.join(rejected)}"
            )

    tests_of: Dict[str, List[str]] = defaultdict(list)
    for test, modules in coverage.items():
        for module in modules:
            tests_of[module].append(test)

    head_lines = lru_cache(maxsize=None)(lambda path: _line_count(root / path))
    base_lines = lru_cache(maxsize=None)(base_lines)
    for module, tests in tests_of.items():
        reported = sorted(set(tests) & changed)
        if not reported:
            continue
        module_lines = head_lines(module)
        total = _total(module, tests, coverage, head_lines)
        if total <= Fraction(MAX_RATIO) * module_lines:
            continue
        grew = _within_limit_on_base(module, tests, coverage, base_lines)
        (report.over if grew else report.over_on_base).extend(
            Overage(
                path,
                head_lines(path),
                math.ceil(total),
                len(tests),
                module,
                module_lines,
            )
            for path in reported
        )
    return report


def _size(item: Overage) -> str:
    return (
        f"{item.lines} lines; {item.total_lines} across {item.test_files} test "
        f"file(s) against {item.module_lines} in {item.module}; the limit is "
        f"{MAX_RATIO}x"
    )


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base", default="origin/main", help="ref to compare against")
    args = parser.parse_args(argv)

    try:
        root = Path(_git("rev-parse", "--show-toplevel").strip())
        changed = _git(
            "diff", "--name-only", "--diff-filter=d", "-z", f"{args.base}...HEAD"
        )
        tracked = _git("-C", str(root), "ls-files", "-z")
        report = check(
            root,
            tracked.split("\0"),
            set(changed.split("\0")),
            _base_line_reader(root, args.base),
        )
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.decode(errors="replace").strip()
        print(f"git failed: {stderr}", file=sys.stderr)
        return 2

    for path, reason in report.exempt.items():
        print(f"{path}: ratio-exempt: {reason}")
    for item in report.over_on_base:
        print(f"{item.path}: already over the limit on base: {_size(item)}")
    for path, problem in report.unresolved.items():
        print(f"{path}: {problem}")
    for item in report.over:
        print(f"{item.path}: {_size(item)}")
    return 1 if report.over or report.unresolved else 0


if __name__ == "__main__":
    sys.exit(main())
