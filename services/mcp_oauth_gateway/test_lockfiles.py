"""
Offline guards for the gateway's hash-locked dependency files.

The Docker image installs requirements.txt with --require-hashes, but that build
only runs on deploy-branch pushes. These checks catch, at PR time and without
network access, a lock that drifted from requirements.in, a runtime lock out of
step with the dev lock the tests install, an entry that lost its hashes, and a
pin that fell below a known security floor.
"""

import re
import unittest
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

HERE = Path(__file__).resolve().parent

# Versions at or above which a published advisory is fixed. Raise these when a
# new advisory lands; never lower them to make a lock pass.
SECURITY_FLOORS = {
    "h11": "0.16.0",  # CVE-2025-43859
    "idna": "3.7",  # CVE-2024-3651
    "pyjwt": "2.10.1",  # CVE-2024-53861
    "cryptography": "44.0.1",  # CVE-2024-12797
    "python-multipart": "0.0.18",  # CVE-2024-53981
    "starlette": "0.49.1",  # CVE-2025-62727
}

_ENTRY = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+?)(?: ; (.+?))?(?: \\)?$")
_HASH_LINE = re.compile(r"^    --hash=(\S+?)(?: \\)?$")
_VALID_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


def _parse_lock(name):
    """Return ({package: [(version, marker, [hashes])]}, header text) for a uv lock.

    Every line must be a comment, a ``name==version`` entry or one of its
    ``--hash=`` continuation lines; anything else fails the parse, so an entry
    the parser cannot read never slips past the checks below.
    """
    entries = {}
    current = None
    header = []
    for number, line in enumerate((HERE / name).read_text().splitlines(), 1):
        if not line.strip():
            continue
        if line.lstrip().startswith("#"):
            if current is None:
                header.append(line)
            continue
        m = _ENTRY.match(line)
        if m:
            package = canonicalize_name(m.group(1))
            current = (m.group(2), m.group(3), [])
            markers = [marker for _, marker, _ in entries.get(package, [])]
            if None in markers or (markers and m.group(3) is None) or m.group(3) in markers:
                raise AssertionError(f"{name}:{number}: {package} is locked twice for the same environment")
            entries.setdefault(package, []).append(current)
            continue
        h = _HASH_LINE.match(line)
        if h and current is not None:
            current[2].append(h.group(1))
            continue
        raise AssertionError(f"{name}:{number}: unrecognised lock line {line!r}")
    return entries, "\n".join(header)


def _single(lock, package, lock_name):
    """The one unmarked entry for ``package``; packages that fork by marker are rejected."""
    entries = lock[package]
    if len(entries) != 1:
        raise AssertionError(f"{lock_name}: {package} is split by marker; compare it by hand")
    return entries[0]


def _parse_in(name):
    """Return ([Requirement], [-r includes]) for a requirements .in file."""
    reqs, includes = [], []
    for raw in (HERE / name).read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("-r "):
            includes.append(line[3:].strip())
        else:
            reqs.append(Requirement(line))
    return reqs, includes


def _recorded_command(lock, source):
    return f"#    uv pip compile --universal --generate-hashes --python-version 3.11 {source} -o {lock}"


def _documented_command(lock, source):
    return (
        "#   cd services/mcp_oauth_gateway\n"
        "#   uv pip compile --universal --generate-hashes --python-version 3.11 \\\n"
        f"#     {source} -o {lock}\n"
    )


class TestRuntimeLock(unittest.TestCase):
    def setUp(self):
        self.lock, self.header = _parse_lock("requirements.txt")
        self.reqs, self.includes = _parse_in("requirements.in")

    def test_direct_dependencies_are_exact_pins_matching_the_lock(self):
        self.assertEqual(self.includes, [])
        self.assertIn("python-multipart", {canonicalize_name(r.name) for r in self.reqs})
        for req in self.reqs:
            self.assertIsNone(req.marker, f"{req.name} must apply everywhere")
            specs = list(req.specifier)
            self.assertEqual(
                [s.operator for s in specs], ["=="], f"{req.name} must be exact-pinned in requirements.in"
            )
            name = canonicalize_name(req.name)
            self.assertIn(name, self.lock, f"{req.name} is missing from requirements.txt")
            self.assertEqual(
                Version(_single(self.lock, name, "requirements.txt")[0]),
                Version(specs[0].version),
                f"{req.name}: requirements.txt disagrees with requirements.in",
            )

    def test_lock_records_the_documented_compile_command(self):
        self.assertIn(_recorded_command("requirements.txt", "requirements.in"), self.header.splitlines())
        self.assertIn(_documented_command("requirements.txt", "requirements.in"), (HERE / "requirements.in").read_text())


class TestDevLock(unittest.TestCase):
    def setUp(self):
        self.runtime, _ = _parse_lock("requirements.txt")
        self.dev, self.header = _parse_lock("requirements-dev.txt")

    def test_dev_lock_includes_the_runtime_requirements(self):
        _, includes = _parse_in("requirements-dev.in")
        self.assertEqual(includes, ["requirements.in"])

    def test_every_runtime_pin_is_locked_identically_for_the_tests(self):
        for name, entries in self.runtime.items():
            self.assertIn(name, self.dev, f"{name} is in requirements.txt but not requirements-dev.txt")
            self.assertEqual(
                sorted((Version(v), m) for v, m, _ in self.dev[name]),
                sorted((Version(v), m) for v, m, _ in entries),
                f"{name}: the two locks disagree",
            )

    def test_lock_records_the_documented_compile_command(self):
        self.assertIn(_recorded_command("requirements-dev.txt", "requirements-dev.in"), self.header.splitlines())
        self.assertIn(
            _documented_command("requirements-dev.txt", "requirements-dev.in"),
            (HERE / "requirements-dev.in").read_text(),
        )


class TestLockIntegrity(unittest.TestCase):
    def test_every_locked_package_carries_valid_hashes(self):
        for lock_name in ("requirements.txt", "requirements-dev.txt"):
            lock, _ = _parse_lock(lock_name)
            self.assertTrue(lock, f"{lock_name} parsed to nothing")
            for name, entries in lock.items():
                for _, _, hashes in entries:
                    self.assertTrue(hashes, f"{lock_name}: {name} has no --hash")
                    for h in hashes:
                        self.assertRegex(h, _VALID_HASH, f"{lock_name}: {name} has a malformed hash")

    def test_security_floors_hold_in_both_locks(self):
        for lock_name in ("requirements.txt", "requirements-dev.txt"):
            lock, _ = _parse_lock(lock_name)
            for name, floor in SECURITY_FLOORS.items():
                self.assertIn(name, lock, f"{lock_name}: floor package {name} is not locked")
                version, marker, _ = _single(lock, name, lock_name)
                # A marker could keep the fixed version off the image's platform.
                self.assertIsNone(marker, f"{lock_name}: floor package {name} is locked under a marker")
                self.assertGreaterEqual(
                    Version(version),
                    Version(floor),
                    f"{lock_name}: {name} {version} is below the security floor {floor}",
                )


if __name__ == "__main__":
    unittest.main()
