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


def _runner_str(value):
    """A YAML env value as the Actions runner passes it: lowercase booleans, null as empty."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


def _step_env(workflow, job, step):
    """The env a step runs with: workflow, then job, then step env."""
    merged = {**(workflow.get("env") or {}), **(job.get("env") or {}), **(step.get("env") or {})}
    return {k: _runner_str(v) for k, v in merged.items()}


def _assert_expanded(step_env):
    """The runner expands ``${{ }}`` before the step runs; a local run would pass it through literally."""
    for key, value in step_env.items():
        assert "${{" not in value, f"env {key} holds an unexpanded expression: {value!r}"


class TestStepEnvHelpers(unittest.TestCase):
    def test_values_are_passed_as_the_runner_passes_them(self):
        self.assertEqual(
            _step_env({"env": {"A": True, "B": None, "C": 1, "D": False}}, {}, {}),
            {"A": "true", "B": "", "C": "1", "D": "false"},
        )

    def test_step_env_wins_over_job_env_which_wins_over_workflow_env(self):
        self.assertEqual(
            _step_env({"env": {"A": True, "B": None, "C": 1}}, {"env": {"C": 2}}, {"env": {"C": 3}}),
            {"A": "true", "B": "", "C": "3"},
        )
        self.assertEqual(_step_env({"env": {"C": 1}}, {"env": {"C": 2}}, {}), {"C": "2"})
        self.assertEqual(_step_env({}, {"env": {"C": 2}}, {"env": {"C": None}}), {"C": ""})

    def test_an_unexpanded_expression_is_rejected(self):
        _assert_expanded({"A": "x", "B": ""})
        with self.assertRaises(AssertionError):
            _assert_expanded({"A": "x", "B": "${{ github.repository }}"})


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
        "FROM\u2003python:3.12\n",
        "FROM --platform=x\u2003python:3.12\n",
        "FROM \u2003python:3.12\n",
    ]

    def _step(self):
        """The image-python step and the env it runs with: workflow, then job, then step env."""
        workflow = yaml.safe_load((HERE.parents[1] / ".github" / "workflows" / "ci.yml").read_text())
        job = workflow["jobs"]["gateway-tests-run"]
        (step,) = [s for s in job["steps"] if s.get("id") == "image-python"]
        self.assertEqual(step["working-directory"], "services/mcp_oauth_gateway")
        return step, _step_env(workflow, job, step)

    def _run_step(self, dockerfile):
        step, step_env = self._step()
        # Start from a runner whose every locale variable says C.UTF-8, whatever
        # the caller's locale, so only a pin that overrides all of them (LC_ALL)
        # keeps [[:space:]] to ASCII.
        env = {k: v for k, v in os.environ.items() if not k.startswith("LC_") and k not in ("LANG", "LANGUAGE")}
        env.update({"LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "LC_CTYPE": "C.UTF-8", **step_env})
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "Dockerfile").write_text(dockerfile)
            output = Path(tmp, "github_output")
            output.write_text("")
            env["GITHUB_OUTPUT"] = str(output)
            result = subprocess.run(
                ["bash", "-e", "-c", step["run"]],
                cwd=tmp,
                env=env,
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

    def test_step_pins_an_ascii_locale(self):
        # A locale the host lacks falls back to C, which would hide a UTF-8 one here.
        _, step_env = self._step()
        self.assertIn(step_env.get("LC_ALL"), ("C", "POSIX"))

    def test_step_reads_the_gateway_dockerfile(self):
        dockerfile = (HERE / "Dockerfile").read_text()
        code, output = self._run_step(dockerfile)
        self.assertEqual(code, 0)
        self.assertRegex(output, r"^version=(\d+\.\d+)(\.\d+)?\n$")
        self.assertEqual(output.strip().split("=")[1].split(".")[:2], list(_image_python(dockerfile)))


def _audit_workflow():
    return yaml.safe_load((HERE.parents[1] / ".github" / "workflows" / "gateway-deps-audit.yml").read_text())


def _audit_step(job, step_id):
    (step,) = [s for s in _audit_workflow()["jobs"][job]["steps"] if s.get("id") == step_id]
    return step


def _run_audit_step(job, step, files, extra_env=None):
    """Run step ``step`` of gateway-deps-audit.yml job ``job`` in a temp dir holding ``files``.

    Returns (exit code, GITHUB_OUTPUT text, temp dir); the caller cleans up the dir.
    """
    workflow = _audit_workflow()
    job_def = workflow["jobs"][job]
    assert step in job_def["steps"], f"step {step.get('id')!r} is not in job {job!r}"
    step_env = _step_env(workflow, job_def, step)
    # These are what the callers observe; an env key that redirected one would
    # leave nothing real to assert on.
    for key in ("PATH", "GITHUB_OUTPUT", "RUNNER_TEMP"):
        assert key not in step_env, f"workflow env sets {key}"
    _assert_expanded(step_env)
    tmp = tempfile.mkdtemp()
    work = Path(tmp, "work")
    work.mkdir()
    for name, text in files.items():
        Path(work, name).write_text(text)
    output = Path(tmp, "github_output")
    output.write_text("")
    env = {k: v for k, v in os.environ.items() if not k.startswith("LC_") and k not in ("LANG", "LANGUAGE")}
    env.update({"LANG": "C.UTF-8", **step_env})
    env.update({"GITHUB_OUTPUT": str(output), "RUNNER_TEMP": str(Path(tmp, "runner"))})
    env.update(extra_env or {})
    result = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=work, env=env, capture_output=True, text=True)
    return result.returncode, output.read_text(), Path(tmp)


_LOCKS = ("requirements.txt", "requirements-dev.txt")


class TestAuditMarkerStripStep(unittest.TestCase):
    """The audit must see every entry of the universal locks, whatever its marker."""

    def setUp(self):
        self.step = _audit_step("pip-audit", "strip-markers")
        self.assertEqual(self.step["working-directory"], "services/mcp_oauth_gateway")

    def _run(self, files):
        code, output, tmp = _run_audit_step("pip-audit", self.step, files)
        self.addCleanup(subprocess.run, ["rm", "-rf", str(tmp)])
        return code, output, tmp

    def test_markers_are_dropped_and_everything_else_kept(self):
        files = {lock: (HERE / lock).read_text() for lock in _LOCKS}
        self.assertTrue(any(" ; " in text for text in files.values()), "fixture needs a marker to drop")
        code, output, tmp = self._run(files)
        self.assertEqual(code, 0)
        self.assertEqual(output, f"dir={tmp / 'runner' / 'gateway-audit'}\n")
        for lock, text in files.items():
            stripped = (tmp / "runner" / "gateway-audit" / lock).read_text().splitlines()
            original = text.splitlines()
            self.assertEqual(len(stripped), len(original))
            for before, after in zip(original, stripped):
                m = _ENTRY.match(before)
                if m:
                    self.assertEqual(after, f"{m.group(1)}=={m.group(2)} \\")
                else:
                    self.assertEqual(after, before)

    def test_an_entry_the_rewrite_cannot_read_fails_the_step(self):
        good = "pyjwt==2.15.0 ; sys_platform == 'win32' \\\n    --hash=sha256:" + "0" * 64 + "\n"
        for bad in [
            "pyjwt==2.15.0 ; sys_platform == 'win32'\n    --hash=sha256:" + "0" * 64 + "\n",
            "pyjwt>=2.15.0 \\\n    --hash=sha256:" + "0" * 64 + "\n",
            "# only comments\n",
        ]:
            with self.subTest(bad=bad):
                code, output, _ = self._run({"requirements.txt": good, "requirements-dev.txt": bad})
                self.assertNotEqual(code, 0)
                self.assertEqual(output, "")


class TestAuditSteps(unittest.TestCase):
    """Each lock's pip-audit reads the marker-free copy with hashes enforced, and its failure fails the job."""

    _DIR_EXPR = "${{ steps.strip-markers.outputs.dir }}"

    def setUp(self):
        workflow = _audit_workflow()
        # Unset shell means GitHub runs `bash -e {0}`, without pipefail: what _run below reproduces.
        self.assertNotIn("defaults", workflow)
        job = workflow["jobs"]["pip-audit"]
        self.assertNotIn("defaults", job)
        self.workflow = workflow
        self.job = job
        steps = job["steps"]
        ids = [s.get("id") for s in steps]
        start = ids.index("strip-markers")
        self.strip = steps[start]
        self.audits = steps[start + 1 :]
        self.assertEqual(len(self.audits), 2)
        for step in [self.strip, *self.audits]:
            self.assertNotIn("shell", step)

        code, output, tmp = _run_audit_step("pip-audit", self.strip, {lock: (HERE / lock).read_text() for lock in _LOCKS})
        self.addCleanup(subprocess.run, ["rm", "-rf", str(tmp)])
        self.assertEqual(code, 0)
        self.assertTrue(output.startswith("dir="))
        self.dir = output.strip().removeprefix("dir=")

    def _run(self, step, audit_exit):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(tmp)])
        (tmp / "bin").mkdir()
        (tmp / "work").mkdir()
        stub = tmp / "bin" / "pip-audit"
        stub.write_text(
            f'#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a"; done > "{tmp}/argv"\n'
            f'echo "stub report"\nexit {audit_exit}\n'
        )
        stub.chmod(0o755)
        summary = tmp / "summary"
        summary.write_text("")
        self.assertEqual(step["run"].count("${{"), step["run"].count(self._DIR_EXPR))
        script = step["run"].replace(self._DIR_EXPR, self.dir)
        step_env = _step_env(self.workflow, self.job, step)
        # The stub and the summary file are what this test observes; an env key
        # that redirected either would leave nothing real to assert on.
        self.assertNotIn("PATH", step_env)
        self.assertNotIn("GITHUB_STEP_SUMMARY", step_env)
        env = {**os.environ, **step_env, "PATH": f"{tmp / 'bin'}:{os.environ['PATH']}", "GITHUB_STEP_SUMMARY": str(summary)}
        # A path that left the temp dir would be created on the host and never
        # cleaned up; an unexpanded expression would pass through literally.
        workdir = step.get("working-directory", "")
        self.assertFalse(Path(workdir).is_absolute(), workdir)
        self.assertNotIn("..", Path(workdir).parts)
        self.assertNotIn("${{", workdir)
        _assert_expanded(step_env)
        cwd = tmp / "work" / workdir
        cwd.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(["bash", "-e", "-c", script], cwd=cwd, env=env, capture_output=True, text=True)
        argv = (tmp / "argv").read_text().splitlines() if (tmp / "argv").exists() else None
        return result.returncode, argv, summary.read_text()

    def test_each_lock_is_audited_from_the_marker_free_copy_with_hashes_required(self):
        for step, lock in zip(self.audits, _LOCKS):
            with self.subTest(lock=lock):
                code, argv, summary = self._run(step, 0)
                self.assertEqual(code, 0)
                self.assertIsNotNone(argv)
                self.assertEqual(argv.count("-r"), 1)
                path = argv[argv.index("-r") + 1]
                self.assertEqual(path, f"{self.dir}/{lock}")
                self.assertNotIn(" ; ", Path(path).read_text())
                self.assertIn("--require-hashes", argv)
                self.assertIn("--disable-pip", argv)
                self.assertIn("stub report", summary)

    def test_a_failing_audit_fails_its_step_with_pip_audits_exit_code(self):
        for step, lock in zip(self.audits, _LOCKS):
            with self.subTest(lock=lock):
                code, argv, _ = self._run(step, 3)
                self.assertIsNotNone(argv)
                self.assertEqual(code, 3)

    def test_audit_steps_cannot_be_skipped_or_ignored(self):
        runtime, dev = self.audits
        for step in [self.strip, *self.audits]:
            with self.subTest(step=step.get("name")):
                self.assertNotIn("continue-on-error", step)
        self.assertNotIn("if", self.strip)
        self.assertNotIn("if", runtime)
        # The dev lock is audited even when the runtime audit failed, but not
        # when there is no marker-free copy to audit.
        self.assertEqual(dev["if"], "${{ !cancelled() && steps.strip-markers.outcome == 'success' }}")


class TestRecompileStep(unittest.TestCase):
    """The freshness job runs exactly each lock's recorded command, and nothing else."""

    def setUp(self):
        self.step = _audit_step("lock-freshness", "recompile")
        self.assertEqual(self.step["working-directory"], "services/mcp_oauth_gateway")

    def _run(self, files, uv_exit=0):
        bindir = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(bindir)])
        log = bindir / "calls"
        # uv logs its arguments; the other stubs log their name too, so a header
        # naming another binary is caught when it runs, not only when it is missing.
        for name, prefix in [("uv", ""), ("uvx", "uvx "), ("sh", "sh "), ("xuv", "xuv ")]:
            stub = bindir / name
            stub.write_text(f'#!/bin/sh\nprintf "%s\\n" "{prefix}$*" >> "{log}"\nexit {uv_exit}\n')
            stub.chmod(0o755)
        code, _, tmp = _run_audit_step("lock-freshness", self.step, files, {"PATH": f"{bindir}:{os.environ['PATH']}"})
        self.addCleanup(subprocess.run, ["rm", "-rf", str(tmp)])
        return code, log.read_text().splitlines() if log.exists() else []

    def _lock(self, command, banner="# This file was autogenerated by uv via the following command:"):
        return f"{banner}\n#    {command}\nfoo==1.0 \\\n    --hash=sha256:{'0' * 64}\n"

    def _real(self):
        return {lock: (HERE / lock).read_text() for lock in _LOCKS}

    def test_runs_the_command_each_real_lock_records(self):
        files = self._real()
        code, calls = self._run(files)
        self.assertEqual(code, 0)
        expected = []
        for lock in _LOCKS:
            recorded = files[lock].splitlines()[1]
            self.assertEqual(recorded, _recorded_header(lock, lock.replace(".txt", ".in")).splitlines()[1])
            expected.append(recorded.removeprefix("#    uv "))
        self.assertEqual(calls, expected)

    def test_another_python_version_is_accepted(self):
        files = self._real()
        files["requirements.txt"] = files["requirements.txt"].replace("--python-version 3.11", "--python-version 3.12")
        code, calls = self._run(files)
        self.assertEqual(code, 0)
        self.assertEqual(len(calls), 2)
        self.assertIn("--python-version 3.12 requirements.in", calls[0])

    def test_a_failing_compile_fails_the_step(self):
        code, _ = self._run(self._real(), uv_exit=2)
        self.assertNotEqual(code, 0)

    def test_a_header_that_is_not_the_fixed_compile_command_runs_nothing(self):
        base = "uv pip compile --universal --generate-hashes --python-version 3.11"
        ok = f"{base} requirements-dev.in -o requirements-dev.txt"
        for command in [
            f"{base} requirements.in -o requirements.txt; touch x",
            f"{base} requirements.in -o $(touch x)",
            f"{base} $(touch x) requirements.in -o requirements.txt",
            f"{base} requirements.in -o requirements.txt `touch x`",
            f"{base} --cache-dir=.. requirements.in -o requirements.txt",
            f"{base} --constraints=.. -o requirements.txt",
            f"{base} requirements.in --upgrade -o requirements.txt",
            f"{base} requirements-dev.in -o requirements.txt",
            f"{base} requirements.in -o other.txt",
            f"{base} requirements.in -o requirements.txt -q",
            f"{base} requirements.in -o ../requirements.txt",
            f"{base} requirements.in",
            f"{base} requirementsXin -o requirementsXtxt",
            "uv pip compile --universal --generate-hashes --python-version 3.x requirements.in -o requirements.txt",
            "uv pip compile --universal --python-version 3.11 requirements.in -o requirements.txt",
            "uv pip sync --universal --generate-hashes --python-version 3.11 requirements.in -o requirements.txt",
            f"uv  pip {base[7:]} requirements.in -o requirements.txt",
            f"env {base} requirements.in -o requirements.txt",
            f" {base} requirements.in -o requirements.txt",
            f"x{base} requirements.in -o requirements.txt",
            f"uvx {base[3:]} requirements.in -o requirements.txt",
            f"sh {base[3:]} requirements.in -o requirements.txt",
            f"{base} --upgrade requirements.in -o requirements.txt",
            f"{base} -U requirements.in -o requirements.txt",
            f"{base} requirements.in -o requirements.txt --upgrade",
            f"{base} ../requirements.in -o requirements.txt",
            f"{base} requirementsXin -o requirements.txt",
            f"{base} requirements.in -o requirementsXtxt",
            "uv pip compile --generate-hashes --python-version 3.11 requirements.in -o requirements.txt",
            "uv pip compile --universal --generate-hashes --python-version 3 9 requirements.in -o requirements.txt",
            "uv pip compile --universal --generate-hashes --python-version 3 requirements.in -o requirements.txt",
            "uv pip compile --universal --generate-hashes --python-version 3..11 requirements.in -o requirements.txt",
        ]:
            with self.subTest(command=command):
                code, calls = self._run(
                    {"requirements.txt": self._lock(command), "requirements-dev.txt": self._lock(ok)}
                )
                self.assertNotEqual(code, 0)
                self.assertEqual(calls, [])

    def test_a_command_below_the_second_header_line_runs_nothing(self):
        command = "uv pip compile --universal --generate-hashes --python-version 3.11 requirements.in -o requirements.txt"
        files = self._real()
        banner, rest = files["requirements.txt"].split("\n", 1)
        files["requirements.txt"] = f"{banner}\n#\n#    {command}\n{rest}"
        code, calls = self._run(files)
        self.assertNotEqual(code, 0)
        self.assertEqual(calls, [])

    def test_a_lock_without_uvs_banner_runs_nothing(self):
        files = self._real()
        files["requirements.txt"] = "# hand-written\n" + files["requirements.txt"].split("\n", 1)[1]
        code, calls = self._run(files)
        self.assertNotEqual(code, 0)
        self.assertEqual(calls, [])


class TestAuditWorkflowShape(unittest.TestCase):
    """gateway-deps-audit.yml runs where its checks need it, read-only, and cannot pass by skipping."""

    def setUp(self):
        workflows = HERE.parents[1] / ".github" / "workflows"
        self.workflow = yaml.safe_load((workflows / "gateway-deps-audit.yml").read_text())
        self.ci = yaml.safe_load((workflows / "ci.yml").read_text())

    def test_triggers_cover_every_lock_input(self):
        # PyYAML reads the bare key `on` as True; a quoted "on" stays a string.
        triggers = self.workflow.get("on", self.workflow.get(True))
        self.assertEqual(set(triggers), {"pull_request", "schedule", "workflow_dispatch"})
        self.assertEqual(
            triggers["pull_request"],
            {
                "paths": [
                    "services/mcp_oauth_gateway/requirements*.in",
                    "services/mcp_oauth_gateway/requirements*.txt",
                    "services/mcp_oauth_gateway/Dockerfile",
                    ".github/workflows/gateway-deps-audit.yml",
                ]
            },
        )
        (schedule,) = triggers["schedule"]
        self.assertEqual(set(schedule), {"cron"})

    def test_token_is_read_only(self):
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        for name, job in self.workflow["jobs"].items():
            with self.subTest(job=name):
                self.assertNotIn("permissions", job)

    def test_actions_are_pinned_as_in_ci(self):
        def pins(workflow):
            found = {}
            for job in workflow["jobs"].values():
                for step in job.get("steps", []):
                    if "uses" in step:
                        action, ref = step["uses"].split("@")
                        found.setdefault(action, set()).add(ref)
            return found

        ci = pins(self.ci)
        audit = pins(self.workflow)
        self.assertTrue(audit)
        for action, refs in audit.items():
            with self.subTest(action=action):
                self.assertEqual(refs, ci[action])

    def test_no_job_or_freshness_step_can_be_skipped_or_ignored(self):
        for name, job in self.workflow["jobs"].items():
            with self.subTest(job=name):
                self.assertNotIn("if", job)
                self.assertNotIn("continue-on-error", job)
        for step_id in ("recompile", "unchanged"):
            with self.subTest(step=step_id):
                step = _audit_step("lock-freshness", step_id)
                self.assertNotIn("if", step)
                self.assertNotIn("continue-on-error", step)


class TestLockChangedStep(unittest.TestCase):
    """After re-compiling, any change to the gateway directory must fail the job."""

    def setUp(self):
        workflow = yaml.safe_load((HERE.parents[1] / ".github" / "workflows" / "gateway-deps-audit.yml").read_text())
        ids = [s.get("id") for s in workflow["jobs"]["lock-freshness"]["steps"]]
        self.assertEqual(ids[-2:], ["recompile", "unchanged"])
        self.step = _audit_step("lock-freshness", "unchanged")
        self.assertEqual(self.step["working-directory"], "services/mcp_oauth_gateway")
        self.assertNotIn("if", self.step)

    def _run(self, change):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(subprocess.run, ["rm", "-rf", str(tmp)])
        work = tmp / "services" / "mcp_oauth_gateway"
        work.mkdir(parents=True)
        (work / "requirements.txt").write_text("a\n")
        (work / ".gitignore").write_text("build/\n")
        workflow = _audit_workflow()
        step_env = _step_env(workflow, workflow["jobs"]["lock-freshness"], self.step)
        _assert_expanded(step_env)
        # Keep the caller's git config (signing, hooks) out of the fixture repo.
        env = {**os.environ, **step_env, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-C", str(tmp)]
        subprocess.run([*git, "init", "-q"], check=True, env=env)
        subprocess.run([*git, "add", "."], check=True, env=env)
        subprocess.run([*git, "commit", "-q", "-m", "init"], check=True, env=env)
        change(work)
        result = subprocess.run(
            ["bash", "-e", "-c", self.step["run"]], cwd=work, env=env, capture_output=True, text=True
        )
        return result.returncode

    def test_an_unchanged_directory_passes(self):
        self.assertEqual(self._run(lambda work: None), 0)

    def test_a_modified_new_deleted_or_ignored_file_fails(self):
        def ignored(work):
            (work / "build").mkdir()
            (work / "build" / "x").write_text("b\n")

        for change in [
            lambda work: (work / "requirements.txt").write_text("b\n"),
            lambda work: (work / "requirements-new.txt").write_text("b\n"),
            lambda work: (work / "requirements.txt").unlink(),
            ignored,
        ]:
            with self.subTest(change=change):
                self.assertNotEqual(self._run(change), 0)


if __name__ == "__main__":
    unittest.main()
