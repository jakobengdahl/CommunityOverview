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

Two properties keep it from rotting into a green rubber stamp:

- The gate bodies are shell, so they are extracted from the parsed workflow and
  EXECUTED, rather than pattern-matched. A regex over the YAML would pass
  against a body rewritten to something equivalent-looking and wrong.
- The set of gates is DISCOVERED from the workflow and compared against the
  expected set, rather than only enumerated here. A hand-maintained list would
  let a fifth gate ship the very bug this file exists to prevent - and
  `ci.yml` names `frontend-lint` as the obvious next candidate for the same
  split, so that is a live possibility rather than a hypothetical.
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

REQUIRED_CHECK_NAMES = {
    "backend-tests": "Backend tests",
    "frontend-tests": "Frontend tests",
    "gateway-tests": "Gateway tests",
    "python-lint": "Python lint (ruff)",
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

# The two rows the whole change turns on. Pinned separately so emptying or
# trimming MATRIX cannot quietly reduce this file to a green no-op.
DECISIVE_ROWS = {
    ("success", "skipped", "true", FAIL),
    ("success", "skipped", "false", PASS),
}


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def is_always(expr):
    """`if: always()` parses as the string `always()`; `if: ${{ always() }}` as
    that literal; a bare `if: true` as a bool. All three mean the same thing."""
    if expr is True:
        return True
    return (
        str(expr).strip().removeprefix("${{").removesuffix("}}").strip() == "always()"
    )


def discover_gates(workflow):
    """Every job that runs unconditionally and waits on a `<id>-run` worker.

    Derived from the workflow rather than from GATES, so adding a gate without
    adding it here is a test failure instead of an invisible coverage hole.
    """
    return {
        job_id
        for job_id, job in workflow["jobs"].items()
        if is_always(job.get("if")) and f"{job_id}-run" in (job.get("needs") or [])
    }


def gate_step(workflow, job_id):
    """The verification step - the one declaring RESULT - rather than steps[0],
    so adding a job-summary or checkout step to a gate is not a failure."""
    steps = workflow["jobs"][job_id]["steps"]
    verifying = [s for s in steps if "RESULT" in (s.get("env") or {})]
    assert len(verifying) == 1, (
        f"{job_id}: expected one step declaring RESULT, got {len(verifying)}"
    )
    return verifying[0]


def run_gate(workflow, job_id, detect, result, code, tmp_path):
    # Stub the GITHUB_* file variables a gate step might plausibly append to.
    # Unset, `>> "$GITHUB_STEP_SUMMARY"` expands to an empty redirect target
    # and bash returns 1 - which would report a false FAIL for a case that
    # passes in CI.
    env = {
        "PATH": "/usr/bin:/bin",
        "DETECT": detect,
        "RESULT": result,
        "CODE": code,
        "GITHUB_STEP_SUMMARY": str(tmp_path / "summary"),
        "GITHUB_OUTPUT": str(tmp_path / "output"),
        "GITHUB_ENV": str(tmp_path / "env"),
    }
    return subprocess.run(
        ["bash", "-c", gate_step(workflow, job_id)["run"]],
        env=env,
        capture_output=True,
        text=True,
    )


class TestTheGateSetIsDiscovered:
    """Without these, every parametrised test below can collapse to zero cases
    and still report green."""

    def test_every_gate_in_the_workflow_is_covered(self, workflow):
        discovered = discover_gates(workflow)
        assert discovered == set(GATES), (
            "gate jobs in ci.yml do not match the set this file checks. "
            f"only in workflow: {sorted(discovered - set(GATES))}; "
            f"only here: {sorted(set(GATES) - discovered)}. "
            "A new gate must be added to GATES, or it ships unverified."
        )

    def test_the_decisive_rows_are_still_in_the_matrix(self):
        assert DECISIVE_ROWS <= set(MATRIX)

    def test_check_names_cover_the_same_gates(self):
        assert set(REQUIRED_CHECK_NAMES) == set(GATES)


class TestGateDistinguishesWhyTheWorkerSkipped:
    @pytest.mark.parametrize("job_id,flag", sorted(GATES.items()))
    @pytest.mark.parametrize("detect,result,code,expected", MATRIX)
    def test_gate_exit_code(
        self, workflow, job_id, flag, detect, result, code, expected, tmp_path
    ):
        proc = run_gate(workflow, job_id, detect, result, code, tmp_path)
        assert proc.returncode == expected, (
            f"{job_id}: DETECT={detect} RESULT={result} CODE={code!r} "
            f"exited {proc.returncode}, expected {expected}\n{proc.stdout}{proc.stderr}"
        )

    @pytest.mark.parametrize("job_id,flag", sorted(GATES.items()))
    def test_a_draft_skip_says_the_suite_did_not_run(
        self, workflow, job_id, flag, tmp_path
    ):
        """The failure has to be actionable: a reader must not mistake it for a
        real test failure, or they will hunt a bug that isn't there."""
        proc = run_gate(workflow, job_id, "success", "skipped", "true", tmp_path)
        assert proc.returncode == FAIL
        assert "has NOT run" in proc.stdout

    @pytest.mark.parametrize("job_id,flag", sorted(GATES.items()))
    def test_gate_reads_the_flag_its_own_worker_is_gated_on(
        self, workflow, job_id, flag
    ):
        """A gate wired to the wrong flag would pass a draft-skip whenever the
        other flag happened to be false."""
        env = gate_step(workflow, job_id)["env"]
        assert env["CODE"] == f"${{{{ needs.detect-changes.outputs.{flag} }}}}"
        assert env["RESULT"] == f"${{{{ needs.{job_id}-run.result }}}}"


class TestTheWorkerGateWiringItself:
    """The matrix above is only meaningful if the jobs are still wired the way
    it assumes."""

    @pytest.mark.parametrize("job_id,flag", sorted(GATES.items()))
    def test_gate_always_runs_and_needs_both(self, workflow, job_id, flag):
        gate = workflow["jobs"][job_id]
        assert is_always(gate["if"])
        assert set(gate["needs"]) == {"detect-changes", f"{job_id}-run"}

    @pytest.mark.parametrize("job_id,flag", sorted(GATES.items()))
    def test_gate_is_a_single_verification_step(self, workflow, job_id, flag):
        """Not a correctness requirement - a gate could legitimately gain a step.
        Named separately so that change fails here with an obvious message
        rather than scattering failures across the matrix."""
        assert len(workflow["jobs"][job_id]["steps"]) == 1

    @pytest.mark.parametrize("job_id,flag", sorted(GATES.items()))
    def test_required_check_name_is_unchanged(self, workflow, job_id, flag):
        """Branch protection matches on these names; renaming one silently
        removes a required check rather than failing it."""
        assert workflow["jobs"][job_id]["name"] == REQUIRED_CHECK_NAMES[job_id]

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
