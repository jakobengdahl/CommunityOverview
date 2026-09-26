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
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "security-scan.yml"

SCANNERS = ("pip-audit", "npm", "bandit")


def _workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def _teed_steps():
    steps = []
    for job_id, job in _workflow()["jobs"].items():
        for step in job.get("steps", []):
            if "| tee" in step.get("run", ""):
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
    env = dict(
        os.environ,
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


def test_pull_request_trigger_names_no_retired_branch():
    # PyYAML reads the bare `on:` key as boolean True.
    workflow = _workflow()
    triggers = workflow.get("on", workflow.get(True))
    assert "dev" not in triggers["pull_request"]["branches"]


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
