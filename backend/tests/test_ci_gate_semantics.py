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

# Every check name `main`'s branch protection requires - the four gates above
# plus `frontend-lint`, which is unconditional and does its own work rather
# than gating a worker. Listed in full because the property that actually
# matters is about these NAMES, not about the gate pattern: GitHub reports a
# SKIPPED job to branch protection as success, so any job carrying one of these
# names either has to be a gate this file verifies, or has to be incapable of
# being skipped at all.
BRANCH_PROTECTION_CHECKS = set(REQUIRED_CHECK_NAMES.values()) | {
    "Frontend lint (eslint + prettier)",
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
    # Unreachable today - detect-changes writes both flags before it can
    # succeed - but pinned because defaulting an unknown flag to "pass" is the
    # exact shape of the bug this gate exists to fix. Only an explicit false
    # lets a skip through.
    ("success", "skipped", "", FAIL),
    ("failure", "skipped", "", FAIL),  # fail-safe
    ("skipped", "skipped", "", FAIL),
    ("cancelled", "skipped", "", FAIL),
]

# The two rows the whole change turns on. Pinned separately so emptying or
# trimming MATRIX cannot quietly reduce this file to a green no-op. Both are
# needed: without the FAIL row a draft-skip goes green again, and without the
# PASS row a "fix" that failed the gate on EVERY skip would look correct while
# breaking every docs-only PR.
DECISIVE_ROWS = {
    ("success", "skipped", "true", FAIL),
    ("success", "skipped", "false", PASS),
}

# Jobs that legitimately run regardless of an upstream result but are not
# required-check gates. Empty today; an entry here is a deliberate statement
# that a job needs no gate coverage, which is reviewable - silently falling
# outside a narrow discovery predicate is not.
NON_GATE_UNCONDITIONAL_JOBS: set[str] = set()


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(WORKFLOW.read_text())


def is_always(expr):
    """Strictly unconditional: `always()`, `${{ always() }}` or a bare `true`.

    Deliberately strict, because the one caller that asserts with it is
    checking the gates really do run unconditionally - `${{ always() && ... }}`
    is a CONDITIONAL always and must fail there. Do NOT reuse this for
    discovery: a predicate that only recognises this spelling is a hole, since
    the gate it fails to recognise is exactly the one that ships unverified.
    """
    if expr is True:
        return True
    return (
        str(expr).strip().removeprefix("${{").removesuffix("}}").strip() == "always()"
    )


def runs_despite_upstream_result(expr):
    """Broad on purpose - the mirror image of is_always().

    Any of `always()`, `!cancelled()`, `success() || failure()` or
    `always() && <something>` makes a job run when an upstream job did not
    succeed, which is what makes it capable of reporting green for work that
    never happened. GitHub's own docs steer people towards `!cancelled()` over
    `always()`, so recognising only the latter would miss the most likely
    spelling of the next gate someone writes.
    """
    if expr is True:
        return True
    if expr is None:
        return False
    text = str(expr)
    # All four of GitHub's status-check functions. `success()` belongs here
    # too: `${{ success() || needs.W.result == 'skipped' }}` suppresses the
    # implicit success() and runs the job after its worker skipped.
    return any(
        tok in text for tok in ("always()", "cancelled()", "failure()", "success()")
    )


def discover_gates(workflow):
    """Jobs that could report a required-check name for work that did not run.

    Two independent angles, because either alone has a blind spot:

    1. A job that waits on something AND runs despite an upstream failure or
       skip. Catches a gate however its `if` is spelled, and whatever its
       worker is called.
    2. The stem of any `<id>-run` job. Catches a gate that follows the naming
       convention even if its `if` is spelled in some way angle 1 misses.

    Anything found here must be in GATES, or explicitly excused in
    NON_GATE_UNCONDITIONAL_JOBS.
    """
    jobs = workflow["jobs"]
    by_condition = {
        job_id
        for job_id, job in jobs.items()
        if job.get("needs") and runs_despite_upstream_result(job.get("if"))
    }
    by_naming = {
        job_id.removesuffix("-run")
        for job_id in jobs
        if job_id.endswith("-run") and job_id.removesuffix("-run") in jobs
    }
    return (by_condition | by_naming) - NON_GATE_UNCONDITIONAL_JOBS


def gate_step(workflow, job_id):
    """The verification step - the one declaring RESULT - rather than steps[0].

    This keeps the truth-table tests working if a gate gains a step; the
    separate single-step assertion below still flags that change deliberately.
    """
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
            "Add a new gate to GATES and REQUIRED_CHECK_NAMES so its truth "
            "table is verified, or - if it does not carry a branch-protection "
            "required-check name - list it in NON_GATE_UNCONDITIONAL_JOBS."
        )

    def test_the_decisive_rows_are_still_in_the_matrix(self):
        # Pin the rows literally, not just their count: any two MATRIX rows
        # satisfy a length check, so a cardinality assertion alone still lets
        # the draft-skip row be swapped out and the whole bug restored.
        assert DECISIVE_ROWS == {
            ("success", "skipped", "true", FAIL),
            ("success", "skipped", "false", PASS),
        }
        assert DECISIVE_ROWS <= set(MATRIX)

    def test_check_names_cover_the_same_gates(self):
        assert set(REQUIRED_CHECK_NAMES) == set(GATES)

    def test_the_gate_set_itself_is_pinned(self):
        """GATES is what every other test parametrises over, so allowlisting a
        gate and deleting it from GATES would leave the file green while that
        gate carries any body it likes."""
        assert set(GATES) == {
            "backend-tests",
            "frontend-tests",
            "gateway-tests",
            "python-lint",
        }

    def test_the_allowlist_cannot_excuse_a_real_gate(self, workflow):
        """Having a `<id>-run` worker is what makes a job a gate. Excusing one
        is the single edit that would silence this file."""
        for job_id in NON_GATE_UNCONDITIONAL_JOBS:
            assert f"{job_id}-run" not in workflow["jobs"], (
                f"{job_id} has a worker, so it is a gate and cannot be "
                "excused in NON_GATE_UNCONDITIONAL_JOBS"
            )

    def test_every_required_check_either_gates_or_cannot_be_skipped(self, workflow):
        """The property the whole file is about, stated over the check names
        branch protection actually requires.

        GitHub reports a job skipped by its own condition to branch protection
        as success. So a required check is safe only if it is a gate this file
        verifies, or if it can never be skipped - no `needs` and no `if`. The
        cheapest way to make `frontend-lint` cost-aware is to add a condition
        to it in place rather than split it, and that alone would restore the
        bug with every gate-discovery angle still green.
        """
        for job_id, job in workflow["jobs"].items():
            if job.get("name") not in BRANCH_PROTECTION_CHECKS:
                continue
            if job_id in GATES:
                continue
            assert not job.get("needs") and job.get("if") is None, (
                f"{job_id} carries the required check {job['name']!r} but is "
                "neither a verified gate nor unconditional: if it is skipped, "
                "branch protection sees success for work that never ran."
            )


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
