"""The step that runs the backend suite in CI.

`test_ci_gate_semantics.py` pins the worker/gate wiring; this pins what the
worker actually runs: the whole suite, in parallel, against a required
PostgreSQL, with the contract tests' length seed pinned once per run so every
xdist worker collects the same ids and the draw still changes between runs.
"""

from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"


def _run_step():
    job = yaml.safe_load(WORKFLOW.read_text())["jobs"]["backend-tests-run"]
    (step,) = [s for s in job["steps"] if s.get("name") == "Run backend test suite"]
    return step


def test_the_whole_backend_suite_runs_across_all_cores():
    assert _run_step()["run"] == "pytest backend/ -q -n auto"


def test_the_length_seed_is_pinned_per_run_for_every_worker():
    assert _run_step()["env"]["CO_TEST_PG_LENGTH_SEED"] == "${{ github.run_id }}"


def test_an_unreachable_postgres_fails_the_run_instead_of_skipping():
    assert _run_step()["env"]["CO_REQUIRE_POSTGRES"] == "1"
