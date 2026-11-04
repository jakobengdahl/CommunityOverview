#!/usr/bin/env python3
"""Check that the test files a branch changes stay within 1.5x what they cover.

Whole files count, not the lines the branch added: every test file covering the
same module counts towards its total, changed or not, but only changed files are
reported.

Only growth past the limit is reported, not historic size. A module already over the
limit at the merge base of ``--base`` and HEAD is printed for information and does
not fail. Its base total counts the test files that covered it on base, following
renames and including files since deleted or merged, unless HEAD exempts them or
points them elsewhere. A file that covered nothing on base and gets a header on the
branch adds its size on base to that total, and is not itself reported when only
its siblings grew past the limit. A module that does not exist on base, or has no
lines covered there, counts as within the limit.

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
from typing import Callable, Dict, Iterable, List, NamedTuple, Optional, Set, Tuple

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


class Tree(NamedTuple):
    paths: Set[str]
    read: Callable[[str], bytes]

    def lines(self, path: str) -> int:
        return len(self.read(path).splitlines())


class Base(NamedTuple):
    tree: Tree
    renames: Dict[str, str]  # path at HEAD -> path on base


def _git_bytes(*args: str) -> bytes:
    return subprocess.run(["git", *args], check=True, capture_output=True).stdout


def _git(*args: str) -> str:
    return _git_bytes(*args).decode(errors="surrogateescape")


def _is_test_file(path: str) -> bool:
    return bool(PY_TEST.match(path) or JS_TEST.match(path))


def _is_module(path: str, tree: Tree) -> bool:
    return (
        path in tree.paths
        and (path.endswith(MODULE_SUFFIXES) or posixpath.basename(path) == "Dockerfile")
        and not _is_test_file(path)
        and "tests" not in path.split("/")[:-1]
    )


def _name_match(test_path: str, tree: Tree) -> List[str]:
    py = PY_TEST.match(test_path)
    if not py:
        stem = JS_TEST.match(test_path)["stem"]
        candidates = [stem + suffix for suffix in JS_SUFFIXES]
        return [path for path in candidates if _is_module(path, tree)]
    directory = re.sub(r"(^|/)tests/$", r"\1", py["dir"])
    parts = py["name"].split("_")
    for end in range(len(parts), 0, -1):
        path = f"{directory}{'_'.join(parts[:end])}.py"
        if _is_module(path, tree):
            return [path]
    return []


def _header(test_path: str, tree: Tree, pattern: re.Pattern) -> Optional[str]:
    text = tree.read(test_path).decode("utf-8", errors="replace")
    for line in text.splitlines()[:HEADER_LINES]:
        match = pattern.match(line.strip())
        if match:
            return match[1].strip()
    return None


def _covered(test_path: str, tree: Tree) -> List[str]:
    header = _header(test_path, tree, COVERS_HEADER) or ""
    named = [posixpath.normpath(p.strip()) for p in header.split(",") if p.strip()]
    return list(dict.fromkeys(named)) or _name_match(test_path, tree)


def _scan(tree: Tree) -> Tuple[Dict[str, List[str]], Dict[str, str]]:
    """Return each test file's modules and each exempt test file's reason."""
    coverage: Dict[str, List[str]] = {}
    exempt: Dict[str, str] = {}
    for path in sorted(filter(_is_test_file, tree.paths)):
        reason = _header(path, tree, EXEMPT_HEADER)
        if reason:
            exempt[path] = reason
            continue
        modules = [m for m in _covered(path, tree) if _is_module(m, tree)]
        if modules:
            coverage[path] = modules
    return coverage, exempt


def _total(
    module: str,
    tests: Iterable[str],
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


def _failing_tests(
    module: str,
    tests: List[str],
    coverage: Dict[str, List[str]],
    head: Tree,
    base: Base,
    base_scan: Tuple[Dict[str, List[str]], Dict[str, str]],
) -> Set[str]:
    """Return which of the tests over-limit ``module`` counts against, given base."""
    base_module = base.renames.get(module, module)
    if base_module not in base.tree.paths:
        return set(tests)
    base_coverage, base_exempt = base_scan
    head_path = {old: new for new, old in base.renames.items()}

    stable: Dict[str, List[str]] = {}
    for test, modules in base_coverage.items():
        at_head = head_path.get(test, test)
        if base_module in modules and (
            at_head not in head.paths or module in coverage.get(at_head, [])
        ):
            stable[test] = modules

    # A file that covered nothing on base is charged by its header at HEAD. One that
    # was exempt or covered something else was no part of this module's total.
    retrofitted: Dict[str, List[str]] = {}
    retrofitted_at_head = set()
    for test in tests:
        old = base.renames.get(test, test)
        if (
            old in base.tree.paths
            and old not in base_coverage
            and old not in base_exempt
        ):
            named = [base.renames.get(m, m) for m in coverage[test]]
            retrofitted[old] = [m for m in named if m in base.tree.paths]
            retrofitted_at_head.add(test)

    limit = Fraction(MAX_RATIO) * base.tree.lines(base_module)
    charged = {**stable, **retrofitted}
    if _total(base_module, charged, charged, base.tree.lines) <= limit:
        return set(tests)
    # A retrofit must not hide siblings that grew past the limit on their own.
    siblings = [test for test in tests if test not in retrofitted_at_head]
    if _total(base_module, stable, stable, base.tree.lines) <= limit and _total(
        module, siblings, coverage, head.lines
    ) > Fraction(MAX_RATIO) * head.lines(module):
        return set(siblings)
    return set()


def _read_base(root: Path, ref: str) -> Base:
    """Return the tree at the merge base and the renames since it."""
    base = _git("merge-base", ref, "HEAD").strip()
    # ls-tree lists paths relative to the working directory unless run from the root.
    entries = {}
    for entry in _git("-C", str(root), "ls-tree", "-rz", base).split("\0"):
        if entry:
            metadata, path = entry.split("\t", 1)
            entries[path] = metadata.split()[0]
    paths = set(entries)
    fields = _git(
        "-C",
        str(root),
        "diff",
        "--find-renames",
        "--diff-filter=R",
        "--name-status",
        "-z",
        base,
        "HEAD",
    ).split("\0")
    renames = dict(zip(fields[2::3], fields[1::3]))
    blob = lru_cache(maxsize=None)(
        lambda path: _git_bytes("-C", str(root), "show", f"{base}:{path}")
    )

    def resolve(path: str) -> Optional[str]:
        seen = set()
        while path not in seen:
            seen.add(path)
            parts = path.split("/")
            for end in range(1, len(parts) + 1):
                link = "/".join(parts[:end])
                if entries.get(link) == "120000":
                    target = blob(link).decode(errors="surrogateescape")
                    path = posixpath.normpath(
                        posixpath.join(posixpath.dirname(link), target, *parts[end:])
                    )
                    if posixpath.isabs(path):
                        try:
                            path = Path(path).relative_to(root.resolve()).as_posix()
                        except ValueError:
                            return None
                    break
            else:
                return path if path in entries else None
        return None

    # Like HEAD's is_file/read_bytes, count linked content, not the link text.
    resolved = {path: resolve(path) for path in paths}
    paths = {path for path, target in resolved.items() if target is not None}
    read = lru_cache(maxsize=None)(lambda path: blob(resolved[path]))
    return Base(Tree(paths, read), renames)


def check(
    root: Path,
    tracked: List[str],
    changed: Set[str],
    base: Optional[Base] = None,
) -> Report:
    """Hold each module covered by a changed test file to the limit.

    A module ``base`` shows was over the limit before goes to ``over_on_base``.
    """
    head = Tree(
        {path for path in tracked if (root / path).is_file()},
        lru_cache(maxsize=None)(lambda path: (root / path).read_bytes()),
    )
    coverage, exempt = _scan(head)
    report = Report(
        [], {}, {path: why for path, why in exempt.items() if path in changed}, []
    )
    for path in sorted(changed & head.paths):
        if not _is_test_file(path) or path in exempt:
            continue
        covered = _covered(path, head)
        modules = coverage.get(path, [])
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

    # Reading every test file on base is only worth it once a module is over at HEAD.
    scan_base = lru_cache(maxsize=None)(lambda: _scan(base.tree))
    for module, tests in tests_of.items():
        reported = sorted(set(tests) & changed)
        if not reported:
            continue
        module_lines = head.lines(module)
        total = _total(module, tests, coverage, head.lines)
        if total <= Fraction(MAX_RATIO) * module_lines:
            continue
        failing = (
            _failing_tests(module, tests, coverage, head, base, scan_base())
            if base
            else set(tests)
        )
        for path in reported:
            (report.over if path in failing else report.over_on_base).append(
                Overage(
                    path,
                    head.lines(path),
                    math.ceil(total),
                    len(tests),
                    module,
                    module_lines,
                )
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
            _read_base(root, args.base),
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
