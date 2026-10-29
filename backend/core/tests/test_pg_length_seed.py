"""The postgres contract module's length seed under pytest-xdist.

Every xdist worker imports that module and collects on its own, and the
controller aborts the whole run unless all workers collect the same ids. The
batch lengths parametrize those ids, so the seed they come from must agree
across the workers of one run - while still changing between runs, which is
the reason it is drawn at all.
"""

import os
import pathlib
import subprocess
import sys

from backend.core.tests import test_persistence_contract_postgres as pg_contract

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_MODULE = pathlib.Path(pg_contract.__file__).resolve()


def _env_without_seed():
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PYTEST_XDIST_")
        and key
        not in {"CO_TEST_PG_LENGTH_SEED", "CO_TEST_POSTGRES_DSN", "CO_REQUIRE_POSTGRES"}
    }
    env["PYTHONPATH"] = str(_REPO_ROOT)
    return env


def test_xdist_workers_collect_the_same_lengths_without_a_pinned_seed():
    # No DSN, so every case skips; collection - where the lengths are drawn
    # and where xdist compares workers - still runs in full on each worker.
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(_MODULE),
            "-q",
            "-n",
            "2",
            "-p",
            "no:cacheprovider",
        ],
        cwd=_REPO_ROOT,
        env=_env_without_seed(),
        capture_output=True,
        text=True,
        timeout=240,
    )
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    assert "Different tests were collected" not in result.stdout


def test_one_xdist_run_shares_a_seed_and_the_next_run_draws_another(monkeypatch):
    monkeypatch.delenv("CO_TEST_PG_LENGTH_SEED", raising=False)
    monkeypatch.setenv("PYTEST_XDIST_TESTRUNUID", "a" * 32)
    first = pg_contract._pg_length_seed()
    assert pg_contract._pg_length_seed() == first
    monkeypatch.setenv("PYTEST_XDIST_TESTRUNUID", "b" * 32)
    assert pg_contract._pg_length_seed() != first


def test_a_pinned_seed_wins_over_the_xdist_run(monkeypatch):
    monkeypatch.setenv("PYTEST_XDIST_TESTRUNUID", "a" * 32)
    monkeypatch.setenv("CO_TEST_PG_LENGTH_SEED", "4242")
    assert pg_contract._pg_length_seed() == 4242
