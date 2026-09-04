"""
Mutation-history retention is configured by the application, not the library.

GraphHistoryStore defaults to unbounded on purpose — an audit trail is not
something a library should silently truncate. The open-core policy lives here,
and if it does not reach GraphStorage the sidecar grows without limit again,
which is the live out-of-memory risk on a 1 GiB instance.
"""

import os
import tempfile

from backend.api_host.config import AppConfig
from backend.api_host.server import create_app


def test_default_bounds_the_history():
    assert AppConfig().history_max_events == 100_000


def test_age_based_retention_is_opt_in():
    """Deleting records older than X is a policy an operator chooses, not one
    to inherit from a default and discover afterwards."""
    assert AppConfig().history_max_age_days is None


def test_env_vars_override_both(monkeypatch):
    monkeypatch.setenv("HISTORY_MAX_EVENTS", "25")
    monkeypatch.setenv("HISTORY_MAX_AGE_DAYS", "7.5")

    config = AppConfig()

    assert config.history_max_events == 25
    assert config.history_max_age_days == 7.5


def test_the_configured_policy_reaches_the_history_store():
    with tempfile.TemporaryDirectory() as tmpdir:
        config = AppConfig(
            graph_file=os.path.join(tmpdir, "graph.json"),
            history_max_events=17,
            history_max_age_days=3.0,
        )

        app = create_app(config)
        storage = app.state.graph_storage
        try:
            assert storage._history_store.max_events == 17
            assert storage._history_store.max_age_days == 3.0
        finally:
            storage.flush()


def test_the_default_policy_reaches_the_history_store():
    """The default is the one that matters operationally: nothing sets these
    env vars in a normal deployment."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config = AppConfig(graph_file=os.path.join(tmpdir, "graph.json"))

        app = create_app(config)
        storage = app.state.graph_storage
        try:
            assert storage._history_store.max_events == 100_000
        finally:
            storage.flush()


def test_zero_keeps_every_record():
    """A non-positive cap disables retention, which is how an operator opts out
    of trimming entirely."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config = AppConfig(
            graph_file=os.path.join(tmpdir, "graph.json"), history_max_events=0
        )

        app = create_app(config)
        storage = app.state.graph_storage
        try:
            assert storage._history_store.max_events is None
        finally:
            storage.flush()


def test_an_empty_env_var_falls_back_to_the_default(monkeypatch):
    """A set-but-empty variable is a routine compose/k8s artifact. Passed
    straight to int() it raises at app construction; the age field two lines
    below already tolerates it, so the pair must agree."""
    monkeypatch.setenv("HISTORY_MAX_EVENTS", "")
    monkeypatch.setenv("HISTORY_MAX_AGE_DAYS", "")

    config = AppConfig()

    assert config.history_max_events == 100_000
    assert config.history_max_age_days is None


def test_an_empty_env_var_does_not_break_app_construction(monkeypatch):
    monkeypatch.setenv("HISTORY_MAX_EVENTS", "")

    with tempfile.TemporaryDirectory() as tmpdir:
        app = create_app(AppConfig(graph_file=os.path.join(tmpdir, "graph.json")))
        storage = app.state.graph_storage
        try:
            assert storage._history_store.max_events == 100_000
        finally:
            storage.flush()
