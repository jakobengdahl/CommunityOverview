"""Pins that the Security Scan workflow reports what its scanners found.

Two layers once hid every finding. `continue-on-error` kept a hit from failing
the workflow, and underneath it each scanner step piped its report through
`tee -a "$GITHUB_STEP_SUMMARY"` with no pipefail, so the step took tee's exit
status and reported success on its own account. Removing `continue-on-error`
alone would therefore still leave a green run over a real finding.

The step bodies are shell, so they are extracted from the parsed workflow and
EXECUTED against stub scanners, under the same `bash -e` GitHub uses for a
`run:` step with no `shell:`. The set of tee'd steps is discovered from the
workflow rather than listed here, so a new scanner step cannot ship the same
masking unnoticed.
"""

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "security-scan.yml"

SCANNERS = ("pip-audit", "npm", "bandit")

# `|tee`, `| tee` and `|& tee` alike; a spelling the discovery missed would
# escape every exit-status check below.
TEE = re.compile(r"\|&?\s*tee\b")

# Bash reads these at startup. SHELLOPTS and BASH_ENV can switch pipefail on
# without a `shell:` key - and under pipefail `-e` kills the bandit step at the
# pipeline, before it closes its summary fence. BASHOPTS carries only `shopt`
# options, not pipefail, but it too changes the shell the steps run under.
SHELL_STARTUP_VARS = ("SHELLOPTS", "BASHOPTS", "BASH_ENV")

# Owner decision: bandit stays reporting-only until its last findings on main
# are cleared. Every other scanner blocks. Promoting bandit edits this set.
REPORTING_ONLY_STEPS = {("bandit", "Run bandit (medium+ severity, non-blocking)")}


def _workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def _teed_steps():
    steps = []
    for job_id, job in _workflow()["jobs"].items():
        for step in job.get("steps", []):
            if TEE.search(step.get("run", "")):
                steps.append(pytest.param(step, id=f"{job_id}:{step['name']}"))
    return steps


def test_every_scanner_report_step_is_discovered():
    # pip-audit x3, npm audit, bandit. A drop here means a step stopped teeing
    # (fine) or the discovery broke (not fine) - either way, look.
    assert len(_teed_steps()) == 5


def _run_step(step, tmp_path, scanner_exit):
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    for name in SCANNERS:
        stub = stub_dir / name
        stub.write_text(f"#!/bin/sh\necho 'stub {name} report'\nexit {scanner_exit}\n")
        stub.chmod(0o755)
    summary = tmp_path / "summary.md"
    summary.touch()
    script = tmp_path / "step.sh"
    script.write_text(step["run"])
    inherited = {k: v for k, v in os.environ.items() if k not in SHELL_STARTUP_VARS}
    env = dict(
        inherited,
        PATH=f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
        GITHUB_STEP_SUMMARY=str(summary),
    )
    result = subprocess.run(
        ["bash", "-e", str(script)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.returncode, summary.read_text()


@pytest.mark.parametrize("step", _teed_steps())
@pytest.mark.parametrize("scanner_exit", [0, 1, 2])
def test_step_exits_with_the_scanners_status_not_tees(step, tmp_path, scanner_exit):
    returncode, summary = _run_step(step, tmp_path, scanner_exit)
    assert returncode == scanner_exit, (
        f"step {step['name']!r} exited {returncode} for a scanner that exited "
        f"{scanner_exit}; a tee'd scanner step must exit with the scanner's status"
    )
    assert "stub" in summary and "report" in summary


@pytest.mark.parametrize("step", _teed_steps())
def test_code_fences_opened_in_the_summary_are_closed_on_a_finding(step, tmp_path):
    _, summary = _run_step(step, tmp_path, scanner_exit=1)
    assert summary.count("```") % 2 == 0


def test_gitleaks_is_blocking():
    steps = _workflow()["jobs"]["gitleaks"]["steps"]
    scan = [
        s for s in steps if s.get("uses", "").startswith("gitleaks/gitleaks-action")
    ]
    assert len(scan) == 1
    assert not scan[0].get("continue-on-error", False)
    assert not _workflow()["jobs"]["gitleaks"].get("continue-on-error", False)


@pytest.mark.parametrize("job_id", ["pip-audit", "npm-audit"])
def test_dependency_audits_are_blocking(job_id):
    workflow = _workflow()
    job = workflow["jobs"][job_id]
    assert not job.get("continue-on-error", False)
    audits = [s for s in job["steps"] if TEE.search(s.get("run", ""))]
    assert audits, f"no audit step found in job {job_id!r}"
    for step in audits:
        assert not step.get("continue-on-error", False), (
            f"{job_id}:{step['name']} is reporting-only; dependency audits block"
        )
    assert "--ignore-vuln" not in str(job)


def _triggers(workflow):
    # PyYAML reads the bare `on:` key as boolean True.
    return workflow.get("on", workflow.get(True))


def test_pull_request_trigger_names_no_retired_branch():
    workflow = _workflow()
    assert "dev" not in _triggers(workflow)["pull_request"]["branches"]


def test_trigger_set_is_pinned():
    triggers = _triggers(_workflow())
    assert set(triggers) == {"pull_request", "schedule", "workflow_dispatch"}
    assert triggers["pull_request"] == {"branches": ["main", "preview"]}
    assert triggers["schedule"], "the weekly re-audit of unchanged deps is gone"


def test_exactly_the_expected_steps_are_reporting_only():
    workflow = _workflow()
    assert not workflow.get("continue-on-error", False)
    reporting_only = set()
    for job_id, job in workflow["jobs"].items():
        assert not job.get("continue-on-error", False), (
            f"job {job_id!r} is reporting-only"
        )
        for step in job.get("steps", []):
            if step.get("continue-on-error", False):
                reporting_only.add((job_id, step.get("name")))
    assert reporting_only == REPORTING_ONLY_STEPS


def test_gitleaks_cannot_be_switched_off_by_a_condition():
    job = _workflow()["jobs"]["gitleaks"]
    assert "if" not in job
    for step in job["steps"]:
        assert "if" not in step, f"gitleaks:{step.get('name')} is conditional"


def test_gitleaks_checks_out_the_full_history():
    # A shallow clone leaves the scheduled scan nothing but the tip commit.
    steps = _workflow()["jobs"]["gitleaks"]["steps"]
    checkout = [s for s in steps if s.get("uses", "").startswith("actions/checkout")]
    assert len(checkout) == 1
    assert checkout[0].get("with", {}).get("fetch-depth") == 0


def test_no_env_switches_on_pipefail_behind_the_default_shell():
    workflow = _workflow()
    scopes = [("workflow", workflow)]
    for job_id, job in workflow["jobs"].items():
        scopes.append((job_id, job))
        for step in job.get("steps", []):
            scopes.append((f"{job_id}:{step.get('name')}", step))
    for where, scope in scopes:
        for var in SHELL_STARTUP_VARS:
            assert var not in (scope.get("env") or {}), f"{where} sets {var}"
    for job_id, job in workflow["jobs"].items():
        for step in job.get("steps", []):
            body = step.get("run", "")
            for var in SHELL_STARTUP_VARS:
                writes_env = "GITHUB_ENV" in body or "github.env" in body
                assert not (var in body and writes_env), (
                    f"{job_id}:{step.get('name')} may export {var} via GITHUB_ENV"
                )


def test_teed_steps_run_under_the_default_shell_the_test_executes():
    # The steps above are executed under `bash -e`, GitHub's default. An explicit
    # `shell: bash` adds pipefail, under which `-e` kills the bandit step at the
    # pipeline and its summary fence is never closed - so pin the default.
    workflow = _workflow()
    assert "shell" not in workflow.get("defaults", {}).get("run", {})
    for job in workflow["jobs"].values():
        assert "shell" not in job.get("defaults", {}).get("run", {})
    for param in _teed_steps():
        assert "shell" not in param.values[0]
