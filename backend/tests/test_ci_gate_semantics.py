"""Pins what a green required check means.

The CI workflow splits each heavy job into a WORKER (which skips on a draft
PR, and when no relevant files changed) and a GATE that carries the
branch-protection required-check name and runs with `if: always()`.

Both halves are deliberate, but together they once made a skip
indistinguishable from a pass: on a draft PR every required check reported
green while Backend, Frontend and Gateway tests had not executed at all.
`CLAUDE.md`'s definition of done and the orchestrate skill both read "CI is
green" as proof the suite ran, and draft-to-ready is the step immediately
before a merge, so the signal was wrong exactly where it was relied on.

The gate therefore has to distinguish WHY the worker was skipped. Nothing else
in the repo pins that distinction: reverting a gate to the old permissive
`success|skipped` is a one-word edit that no test would notice, and its only
symptom is a green tick that lies. Hence this test.

The gate bodies are shell, so they are extracted from the parsed workflow and
executed, rather than pattern-matched — a regex over the YAML would pass
against a body that had been rewritten to something equivalent-looking and
wrong.
"""

import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"

# gate job id -> the detect-changes output that decides whether its worker had
# anything to do. A skip is legitimate only when this flag is false.
GATES = {
    "backend-tests": "service_code",
    "frontend-tests": "service_code",
    "gateway-tests": "service_code",
    "python-lint": "python_code",
}

PASS, FAIL = 0, 1

# (detect-changes result, worker result, code flag) -> expected gate exit.
# CODE is empty exactly when detect-changes did not succeed, so those rows
# also pin that the DETECT guard is evaluated before the flag is read.
MATRIX = [
    ("success", "success", "true", PASS),
    ("success", "success", "false", PASS),
    ("success", "skipped", "false", PASS),  # nothing relevant changed
    ("success", "skipped", "true", FAIL),  # draft: the suite did not run
    ("success", "failure", "true", FAIL),
    ("success", "failure", "false", FAIL),
    ("success", "cancelled", "true", FAIL),
    ("success", "cancelled", "false", FAIL),
    ("failure", "skipped", "", FAIL),  # fail-safe
    ("skipped", "skipped", "", FAIL),
    ("cancelled", "skipped", "", FAIL),
]


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def gate_script(workflow, job_id):
    steps = workflow["jobs"][job_id]["steps"]
    assert len(steps) == 1, f"{job_id} gate should be a single verification step"
    return steps[0]["run"]


class TestGateDistinguishesWhyTheWorkerSkipped:
    @pytest.mark.parametrize("job_id,flag", sorted(GATES.items()))
    @pytest.mark.parametrize("detect,result,code,expected", MATRIX)
    def test_gate_exit_code(
        self, workflow, job_id, flag, detect, result, code, expected
    ):
        script = gate_script(workflow, job_id)
        proc = subprocess.run(
            ["bash", "-c", script],
            env={
                "PATH": "/usr/bin:/bin",
                "DETECT": detect,
                "RESULT": result,
                "CODE": code,
            },
            capture_output=True,
            text=True,
        )
        assert proc.returncode == expected, (
            f"{job_id}: DETECT={detect} RESULT={result} CODE={code!r} "
            f"exited {proc.returncode}, expected {expected}\n{proc.stdout}{proc.stderr}"
        )

    @pytest.mark.parametrize("job_id,flag", sorted(GATES.items()))
    def test_a_draft_skip_says_the_suite_did_not_run(self, workflow, job_id, flag):
        """The failure has to be actionable: a reader must not mistake it for a
        real test failure, or they will hunt a bug that isn't there."""
        proc = subprocess.run(
            ["bash", "-c", gate_script(workflow, job_id)],
            env={
                "PATH": "/usr/bin:/bin",
                "DETECT": "success",
                "RESULT": "skipped",
                "CODE": "true",
            },
            capture_output=True,
            text=True,
        )
        assert proc.returncode == FAIL
        assert "has NOT run" in proc.stdout

    @pytest.mark.parametrize("job_id,flag", sorted(GATES.items()))
    def test_gate_reads_the_flag_its_own_worker_is_gated_on(
        self, workflow, job_id, flag
    ):
        """A gate wired to the wrong flag would pass a draft-skip whenever the
        other flag happened to be false."""
        env = workflow["jobs"][job_id]["steps"][0]["env"]
        assert env["CODE"] == f"${{{{ needs.detect-changes.outputs.{flag} }}}}"
        assert env["RESULT"] == f"${{{{ needs.{job_id}-run.result }}}}"


class TestTheWorkerGateWiringItself:
    """The matrix above is only meaningful if the jobs are still wired the way
    it assumes."""

    @pytest.mark.parametrize("job_id,flag", sorted(GATES.items()))
    def test_gate_always_runs_and_needs_both(self, workflow, job_id, flag):
        gate = workflow["jobs"][job_id]
        assert gate["if"] is True or str(gate["if"]).strip() == "always()"
        assert set(gate["needs"]) == {"detect-changes", f"{job_id}-run"}

    @pytest.mark.parametrize("job_id,flag", sorted(GATES.items()))
    def test_required_check_name_is_unchanged(self, workflow, job_id, flag):
        """Branch protection matches on these names; renaming one silently
        removes a required check rather than failing it."""
        expected = {
            "backend-tests": "Backend tests",
            "frontend-tests": "Frontend tests",
            "gateway-tests": "Gateway tests",
            "python-lint": "Python lint (ruff)",
        }
        assert workflow["jobs"][job_id]["name"] == expected[job_id]

    @pytest.mark.parametrize("job_id,flag", sorted(GATES.items()))
    def test_worker_is_gated_on_the_flag(self, workflow, job_id, flag):
        assert (
            f"needs.detect-changes.outputs.{flag} == 'true'"
            in workflow["jobs"][f"{job_id}-run"]["if"]
        )

    def test_detect_changes_publishes_both_flags(self, workflow):
        outputs = workflow["jobs"]["detect-changes"]["outputs"]
        assert set(GATES.values()) <= set(outputs)

    def test_publishing_jobs_depend_on_workers_not_gates(self, workflow):
        """build/notify must key off real work, not off a gate that can pass on
        a legitimate path-skip."""
        for job_id in ("build", "notify-infra"):
            needs = workflow["jobs"][job_id].get("needs", [])
            needs = [needs] if isinstance(needs, str) else needs
            assert not (set(needs) & set(GATES)), (
                f"{job_id} depends on a gate job: {needs}"
            )
