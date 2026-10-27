"""The step that runs the backend suite in CI.

`test_ci_gate_semantics.py` pins the worker/gate wiring; this pins what the
worker actually runs: the whole suite, in parallel, against a required
PostgreSQL, with the contract tests' length seed pinned once per run so every
xdist worker collects the same ids and the draw still changes between runs.
The worker job's own name, `needs` and `if` are pinned here too: the gate
tests check the gate's side of the wiring, and each of those three could
change on the worker's side with every gate test still green.
"""

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"


def _job():
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]["backend-tests-run"]


def _run_step():
    (step,) = [s for s in _job()["steps"] if s.get("name") == "Run backend test suite"]
    return step


def test_the_worker_keeps_the_name_the_gate_reports_it_by():
    # The gate's log line and its skip error name the worker this way.
    assert _job()["name"] == "Backend tests (run)"


def test_the_worker_needs_only_change_detection():
    # A needs on anything else lets that job's skip or failure skip the suite,
    # which the gate then has to tell apart from a draft or no-change skip.
    assert _job()["needs"] == "detect-changes"


def test_the_worker_runs_on_service_code_and_skips_only_on_a_draft():
    # Whole, not by substring: dropping the draft clause would run the suite
    # on drafts, and any extra clause is a new way for the suite to skip.
    assert " ".join(_job()["if"].split()) == (
        "needs.detect-changes.outputs.service_code == 'true' && "
        "(github.event_name != 'pull_request' || "
        "github.event.pull_request.draft == false)"
    )


def test_the_whole_backend_suite_runs_across_all_cores():
    assert _run_step()["run"] == "pytest backend/ -q -n auto"


def test_the_length_seed_is_pinned_per_run_for_every_worker():
    assert _run_step()["env"]["CO_TEST_PG_LENGTH_SEED"] == "${{ github.run_id }}"


def test_an_unreachable_postgres_fails_the_run_instead_of_skipping():
    assert _run_step()["env"]["CO_REQUIRE_POSTGRES"] == "1"
