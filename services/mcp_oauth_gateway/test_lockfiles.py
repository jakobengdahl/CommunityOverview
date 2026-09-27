"""
Offline guards for the gateway's hash-locked dependency files.

The Docker image installs requirements.txt with --require-hashes, but that build
only runs on deploy-branch pushes. These checks catch, at PR time and without
network access, a lock that drifted from requirements.in, a runtime lock out of
step with the dev lock the tests install, an entry that lost its hashes, and a
pin that fell below a known security floor.
"""

import itertools
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

from packaging.markers import Marker, default_environment
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

_ENTRY = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+?)(?: ; (.+?))?( \\)?$")
_HASH_LINE = re.compile(r"^    --hash=(\S+?)( \\)?$")
_VALID_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")
_INCLUDE = re.compile(r"^(?:-r|--requirement)(?:\s+|=)(\S+)$")

# Environments a universal lock may be installed into. Two entries for one
# package must never both apply to any of them, whatever their marker text.
_ENVIRONMENTS = [
    {
        **default_environment(),
        "sys_platform": platform,
        "platform_system": system,
        "os_name": os_name,
        "platform_machine": machine,
        "implementation_name": implementation.lower(),
        "platform_python_implementation": implementation,
        "python_version": ".".join(version.split(".")[:2]),
        "python_full_version": version,
        "implementation_version": version,
    }
    for (platform, system, os_name), machine, implementation, version in itertools.product(
        [
            ("linux", "Linux", "posix"),
            ("darwin", "Darwin", "posix"),
            ("win32", "Windows", "nt"),
            ("cygwin", "CYGWIN_NT-10.0", "posix"),
            ("emscripten", "Emscripten", "posix"),
        ],
        ["x86_64", "aarch64"],
        ["CPython", "PyPy"],
        ["3.11.0", "3.12.0", "3.13.0", "3.14.0"],
    )
]


# The final stage's base image, which the image runs on. ci.yml's image-python
# step applies the same rule in shell; TestImagePythonStep holds them together.
_PYTHON_BASE = re.compile(r"\s*(?i:FROM)\s+(?:--platform=\S+\s+)?(?:\S*/)?python:(\d+)\.(\d+)", re.ASCII)


def _image_python(dockerfile):
    """(major, minor) of the Python on the Dockerfile's last FROM line."""
    # awk's records and default fields: lines on \n only, fields on space and tab only.
    lines = dockerfile.split("\n")
    froms = [line for line in lines if re.split(r"[ \t]+", line.strip(" \t"))[0].upper() == "FROM"]
    if not froms:
        raise AssertionError("Dockerfile: no FROM line to read the image's Python from")
    match = _PYTHON_BASE.match(froms[-1])
    if not match:
        raise AssertionError(f"Dockerfile: the last FROM line is not a python:X.Y image: {froms[-1]!r}")
    return match.groups()


def _image_environment():
    """The marker environment of the gateway image, read from its final FROM line."""
    major, minor = _image_python((HERE / "Dockerfile").read_text())
    return {
        **default_environment(),
        "sys_platform": "linux",
        "platform_system": "Linux",
        "os_name": "posix",
        "platform_machine": "x86_64",
        "implementation_name": "cpython",
        "platform_python_implementation": "CPython",
        "python_version": f"{major}.{minor}",
        "python_full_version": f"{major}.{minor}.0",
        "implementation_version": f"{major}.{minor}.0",
    }


def _applies(marker, environment):
    return marker is None or Marker(marker).evaluate(environment)


def _parse_lock(name):
    """Return ({package: [(version, marker, [hashes])]}, header text) for a uv lock.

    Every line must be a comment, a ``name==version`` entry or one of its
    ``--hash=`` continuation lines; anything else fails the parse, so an entry
    the parser cannot read never slips past the checks below. pip joins lines
    on a trailing `` \\``, so an entry and every hash line but its last must
    carry one, and the last must not: a lost backslash silently drops hashes
    from what pip reads.
    """
    entries = {}
    current = None
    continued = False
    header = []
    for number, line in enumerate((HERE / name).read_text().splitlines(), 1):
        h = _HASH_LINE.match(line)
        if continued:
            if not h:
                raise AssertionError(f"{name}:{number}: expected a --hash continuation line, got {line!r}")
            current[2].append(h.group(1))
            continued = bool(h.group(2))
            continue
        if not line.strip():
            continue
        if line.lstrip().startswith("#"):
            if current is None:
                header.append(line)
            continue
        m = _ENTRY.match(line)
        if m:
            package = canonicalize_name(m.group(1))
            if not m.group(4):
                raise AssertionError(f"{name}:{number}: {package} does not continue onto its --hash lines")
            current = (m.group(2), m.group(3), [])
            others = [marker for _, marker, _ in entries.get(package, [])]
            if (
                None in others
                or (others and m.group(3) is None)
                or m.group(3) in others
                or any(
                    _applies(m.group(3), env) and any(_applies(other, env) for other in others)
                    for env in _ENVIRONMENTS
                )
            ):
                raise AssertionError(f"{name}:{number}: {package} is locked twice for the same environment")
            entries.setdefault(package, []).append(current)
            continued = True
            continue
        if h:
            raise AssertionError(f"{name}:{number}: --hash line outside an entry's continuation")
        raise AssertionError(f"{name}:{number}: unrecognised lock line {line!r}")
    if continued:
        raise AssertionError(f"{name}: ends inside a continuation")
    return entries, "\n".join(header)


def _parse_in(name):
    """Return ([Requirement], [-r includes]) for a requirements .in file."""
    reqs, includes = [], []
    for raw in (HERE / name).read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        include = _INCLUDE.match(line)
        if include:
            includes.append(include.group(1))
        elif line.startswith("-"):
            raise AssertionError(f"{name}: unsupported option line {line!r}")
        else:
            reqs.append(Requirement(line))
    return reqs, includes


def _recorded_header(lock, source):
    return (
        "# This file was autogenerated by uv via the following command:\n"
        f"#    uv pip compile --universal --generate-hashes --python-version 3.11 {source} -o {lock}"
    )


def _documented_block(lock, source):
    return (
        "regenerate the lock:\n"
        "#\n"
        "#   cd services/mcp_oauth_gateway\n"
        "#   uv pip compile --universal --generate-hashes --python-version 3.11 \\\n"
        f"#     {source} -o {lock}\n"
        "#\n"
    )


class _CommandChecks:
    """Both locks must be regenerated by exactly the command their .in documents."""

    def assert_commands(self, lock, source):
        _, header = _parse_lock(lock)
        self.assertEqual(header, _recorded_header(lock, source))
        self.assertEqual((HERE / lock).read_text().count("uv pip compile"), 1, f"{lock}: more than one command")
        text = (HERE / source).read_text()
        self.assertEqual(text.count(_documented_block(lock, source)), 1, f"{source}: documented command drifted")
        self.assertEqual(text.count("uv pip compile"), 1, f"{source}: more than one compile command")
        self.assertEqual(len(re.findall(r"^\s*#\s*cd\b", text, re.MULTILINE)), 1, f"{source}: more than one cd line")


class TestRuntimeLock(_CommandChecks, unittest.TestCase):
    def setUp(self):
        self.lock, _ = _parse_lock("requirements.txt")
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
                {Version(v) for v, _, _ in self.lock[name]},
                {Version(specs[0].version)},
                f"{req.name}: requirements.txt disagrees with requirements.in",
            )

    def test_lock_records_the_documented_compile_command(self):
        self.assert_commands("requirements.txt", "requirements.in")


class TestDevLock(_CommandChecks, unittest.TestCase):
    def setUp(self):
        self.runtime, _ = _parse_lock("requirements.txt")
        self.dev, _ = _parse_lock("requirements-dev.txt")

    def test_dev_lock_includes_the_runtime_requirements(self):
        _, includes = _parse_in("requirements-dev.in")
        self.assertEqual(includes, ["requirements.in"])

    def test_every_runtime_pin_is_locked_identically_for_the_tests(self):
        # Versions only: a dev dependency may legitimately widen a runtime
        # package's marker without changing what the image installs.
        for name, entries in self.runtime.items():
            self.assertIn(name, self.dev, f"{name} is in requirements.txt but not requirements-dev.txt")
            self.assertEqual(
                {Version(v) for v, _, _ in self.dev[name]},
                {Version(v) for v, _, _ in entries},
                f"{name}: the two locks disagree",
            )

    def test_lock_records_the_documented_compile_command(self):
        self.assert_commands("requirements-dev.txt", "requirements-dev.in")


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
        image = _image_environment()
        for lock_name in ("requirements.txt", "requirements-dev.txt"):
            lock, _ = _parse_lock(lock_name)
            for name, floor in SECURITY_FLOORS.items():
                self.assertIn(name, lock, f"{lock_name}: floor package {name} is not locked")
                # A marker could keep the fixed version off the image's platform.
                self.assertEqual(
                    sum(_applies(marker, image) for _, marker, _ in lock[name]),
                    1,
                    f"{lock_name}: floor package {name} is not locked for the image's platform",
                )
                for version, _, _ in lock[name]:
                    self.assertGreaterEqual(
                        Version(version),
                        Version(floor),
                        f"{lock_name}: {name} {version} is below the security floor {floor}",
                    )


class TestImagePythonStep(unittest.TestCase):
    """ci.yml's image-python step must read the same Python as _image_python."""

    CASES = [
        ("FROM python:3.12-slim\n", "3.12"),
        ("FROM python:3.12.4-slim AS app\n", "3.12.4"),
        ("from python:3.13\n", "3.13"),
        ("  FROM --platform=linux/amd64 python:3.11-slim\n", "3.11"),
        ("FROM docker.io/library/python:3.12-slim\n", "3.12"),
        ("FROM python:3.11 AS build\nRUN true\n# FROM python:3.10\nFROM python:3.12-slim\n", "3.12"),
        ("FROM python:3.12\nRUN echo from python:3.10\n", "3.12"),
        ("From python:3.12\n", "3.12"),
        ("FROM python:3.12.10-slim\n", "3.12.10"),
        ("FROM python:10.1\n", "10.1"),
        ("FROM\tpython:3.11\n", "3.11"),
        ("FROM python:3.12-slim\r\n", "3.12"),
    ]
    REJECTED = [
        "",
        "# FROM python:3.12\n",
        "FROM python:3.12 AS build\nFROM debian:bookworm-slim\n",
        "FROM mypython:3.12\n",
        "FROM python:3-slim\n",
        "FROM python:3.x\n",
        "FROM busybox from python:3.12\n",
        "FROM\u00a0python:3.12\n",
        "FROM python:\u0663.\u0661\u0662\n",
    ]

    def _step_script(self):
        workflow = yaml.safe_load((HERE.parents[1] / ".github" / "workflows" / "ci.yml").read_text())
        steps = workflow["jobs"]["gateway-tests-run"]["steps"]
        (step,) = [s for s in steps if s.get("id") == "image-python"]
        self.assertEqual(step["working-directory"], "services/mcp_oauth_gateway")
        return step["run"]

    def _run_step(self, dockerfile):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "Dockerfile").write_text(dockerfile)
            output = Path(tmp, "github_output")
            output.write_text("")
            result = subprocess.run(
                ["bash", "-e", "-c", self._step_script()],
                cwd=tmp,
                env={**os.environ, "GITHUB_OUTPUT": str(output)},
                capture_output=True,
                text=True,
            )
            return result.returncode, output.read_text()

    def test_step_and_parser_agree_on_the_final_stage(self):
        for dockerfile, version in self.CASES:
            with self.subTest(dockerfile=dockerfile):
                self.assertEqual(self._run_step(dockerfile), (0, f"version={version}\n"))
                self.assertEqual(".".join(_image_python(dockerfile)), ".".join(version.split(".")[:2]))

    def test_step_and_parser_reject_a_final_stage_without_python(self):
        for dockerfile in self.REJECTED:
            with self.subTest(dockerfile=dockerfile):
                code, output = self._run_step(dockerfile)
                self.assertNotEqual(code, 0)
                self.assertEqual(output, "")
                with self.assertRaises(AssertionError):
                    _image_python(dockerfile)

    def test_step_reads_the_gateway_dockerfile(self):
        dockerfile = (HERE / "Dockerfile").read_text()
        code, output = self._run_step(dockerfile)
        self.assertEqual(code, 0)
        self.assertRegex(output, r"^version=(\d+\.\d+)(\.\d+)?\n$")
        self.assertEqual(output.strip().split("=")[1].split(".")[:2], list(_image_python(dockerfile)))


if __name__ == "__main__":
    unittest.main()
