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

_ENTRY = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+)")
_HASH = re.compile(r"--hash=sha256:([0-9a-f]*)")


def _parse_lock(name):
    """Return ({package: (version, [hashes])}, header text) for a uv lock file."""
    entries = {}
    current = None
    header = []
    for line in (HERE / name).read_text().splitlines():
        m = _ENTRY.match(line)
        if m:
            current = canonicalize_name(m.group(1))
            if current in entries:
                raise AssertionError(f"{name}: {current} is locked twice")
            entries[current] = (m.group(2), [])
        elif current is not None:
            entries[current][1].extend(_HASH.findall(line))
        elif line.startswith("#"):
            header.append(line)
    return entries, "\n".join(header)


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
    return f"uv pip compile --universal --generate-hashes --python-version 3.11 {source} -o {lock}"


class TestRuntimeLock(unittest.TestCase):
    def setUp(self):
        self.lock, self.header = _parse_lock("requirements.txt")
        self.reqs, self.includes = _parse_in("requirements.in")

    def test_direct_dependencies_are_exact_pins_matching_the_lock(self):
        self.assertEqual(self.includes, [])
        for req in self.reqs:
            specs = list(req.specifier)
            self.assertEqual(
                [s.operator for s in specs], ["=="], f"{req.name} must be exact-pinned in requirements.in"
            )
            name = canonicalize_name(req.name)
            self.assertIn(name, self.lock, f"{req.name} is missing from requirements.txt")
            self.assertEqual(
                Version(self.lock[name][0]),
                Version(specs[0].version),
                f"{req.name}: requirements.txt disagrees with requirements.in",
            )

    def test_lock_records_the_documented_compile_command(self):
        self.assertIn(_recorded_command("requirements.txt", "requirements.in"), self.header)
        self.assertIn(
            "uv pip compile --universal --generate-hashes --python-version 3.11 \\\n#     requirements.in -o requirements.txt",
            (HERE / "requirements.in").read_text(),
        )


class TestDevLock(unittest.TestCase):
    def setUp(self):
        self.runtime, _ = _parse_lock("requirements.txt")
        self.dev, self.header = _parse_lock("requirements-dev.txt")

    def test_dev_lock_includes_the_runtime_requirements(self):
        _, includes = _parse_in("requirements-dev.in")
        self.assertEqual(includes, ["requirements.in"])

    def test_every_runtime_pin_is_locked_identically_for_the_tests(self):
        for name, (version, _) in self.runtime.items():
            self.assertIn(name, self.dev, f"{name} is in requirements.txt but not requirements-dev.txt")
            self.assertEqual(self.dev[name][0], version, f"{name}: the two locks disagree")

    def test_lock_records_the_documented_compile_command(self):
        self.assertIn(_recorded_command("requirements-dev.txt", "requirements-dev.in"), self.header)
        self.assertIn(
            "uv pip compile --universal --generate-hashes --python-version 3.11 \\\n#     requirements-dev.in -o requirements-dev.txt",
            (HERE / "requirements-dev.in").read_text(),
        )


class TestLockIntegrity(unittest.TestCase):
    def test_every_locked_package_carries_valid_hashes(self):
        for lock_name in ("requirements.txt", "requirements-dev.txt"):
            lock, _ = _parse_lock(lock_name)
            self.assertTrue(lock, f"{lock_name} parsed to nothing")
            for name, (_, hashes) in lock.items():
                self.assertTrue(hashes, f"{lock_name}: {name} has no --hash")
                for h in hashes:
                    self.assertRegex(h, r"^[0-9a-f]{64}$", f"{lock_name}: {name} has a malformed hash")

    def test_security_floors_hold_in_both_locks(self):
        for lock_name in ("requirements.txt", "requirements-dev.txt"):
            lock, _ = _parse_lock(lock_name)
            for name, floor in SECURITY_FLOORS.items():
                self.assertIn(name, lock, f"{lock_name}: floor package {name} is not locked")
                self.assertGreaterEqual(
                    Version(lock[name][0]),
                    Version(floor),
                    f"{lock_name}: {name} {lock[name][0]} is below the security floor {floor}",
                )


if __name__ == "__main__":
    unittest.main()
