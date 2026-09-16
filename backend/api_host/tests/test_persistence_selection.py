"""The configured backend is the one the graph actually runs on.

Until this module's production code existed, `backend/core/postgres_backend.py`
was unreachable from a deployment: `server.py` built `GraphStorage` with no
`persistence_backend`, so the file backend was used no matter what was
configured. The assertions here are written against that failure mode — a
change that quietly ignores the configuration has to fail, not merely be
unrepresented.

Not covered here, and deliberately: sessions. `docs/CAPACITY.md` states that
the session store is file-backed and its directory derives from the graph
path, so selecting PostgreSQL moves the graph and leaves sessions where they
were. `AppConfig.resolve_sessions_dir` and its tests in `test_session_api.py`
hold that half.
"""

from typing import Any, Dict

import pytest

from backend.api_host.config import AppConfig
from backend.api_host.persistence import (
    PersistenceConfigurationError,
    build_persistence_backend,
)
from backend.core.storage_backends import BackendCapabilities


class StubBackend:
    """A snapshot-contract backend that keeps the graph in memory.

    Stands in for the PostgreSQL backend so these tests need no server. It is
    a real implementation of the protocol rather than a mock, because
    `GraphStorage` reads its capabilities and loads through it during
    construction.
    """

    def __init__(self, conninfo: str, **kwargs: Any):
        self.conninfo = conninfo
        self.kwargs = kwargs
        self.data: Dict[str, Any] = {}

    def exists(self) -> bool:
        return bool(self.data)

    def load_graph_data(self) -> Dict[str, Any]:
        return dict(self.data)

    def save_graph_data(self, data: Dict[str, Any]) -> None:
        self.data = dict(data)

    def default_graph_name(self) -> str:
        return "stub"

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities()


class TestBackendSelection:
    def test_unset_configuration_keeps_the_file_default(self):
        """No configuration means no change: GraphStorage builds its own.

        None rather than a constructed file backend is the point — the default
        path is the same code it was before selection existed.
        """
        assert build_persistence_backend(AppConfig(graph_file="graph.json")) is None

    def test_file_is_selectable_by_name_too(self):
        config = AppConfig(graph_file="graph.json", graph_backend="file")
        assert build_persistence_backend(config) is None

    def test_an_unknown_backend_name_is_refused_not_silently_filed(self):
        """A typo must not boot happily on the file backend.

        This is the expensive failure: a deployment that meant to move to
        PostgreSQL, wrote the name wrong, and looks correct right up until a
        second instance starts writing the same graph.
        """
        config = AppConfig(graph_file="graph.json", graph_backend="postgresql")
        with pytest.raises(PersistenceConfigurationError) as exc:
            build_persistence_backend(config)
        assert "postgresql" in str(exc.value)
        assert "postgres" in str(exc.value)

    def test_postgres_without_a_dsn_is_refused_at_boot(self):
        config = AppConfig(graph_file="graph.json", graph_backend="postgres")
        with pytest.raises(PersistenceConfigurationError) as exc:
            build_persistence_backend(config)
        assert "GRAPH_POSTGRES_DSN" in str(exc.value)

    def test_missing_psycopg_names_the_extra_rather_than_raising_importerror(
        self, monkeypatch
    ):
        """The operator gets an instruction, not a traceback from the load path."""
        import builtins

        real_import = builtins.__import__

        def refuse_postgres_backend(name, *args, **kwargs):
            if name == "backend.core.postgres_backend":
                raise ImportError("No module named 'psycopg'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", refuse_postgres_backend)

        config = AppConfig(
            graph_file="graph.json",
            graph_backend="postgres",
            graph_postgres_dsn="postgresql:///example",
        )
        with pytest.raises(PersistenceConfigurationError) as exc:
            build_persistence_backend(config)
        assert "requirements-postgres.txt" in str(exc.value)

    def test_postgres_settings_reach_the_backend(self, monkeypatch):
        monkeypatch.setattr(
            "backend.core.postgres_backend.PostgresGraphPersistenceBackend",
            StubBackend,
        )
        config = AppConfig(
            graph_file="graph.json",
            graph_backend="postgres",
            graph_postgres_dsn="postgresql:///example",
            graph_postgres_schema="corp",
            graph_postgres_pool_size=3,
        )
        backend = build_persistence_backend(config)

        assert isinstance(backend, StubBackend)
        assert backend.conninfo == "postgresql:///example"
        assert backend.kwargs["schema"] == "corp"
        assert backend.kwargs["pool_size"] == 3

    def test_pool_size_unset_leaves_the_backend_default(self, monkeypatch):
        """Unset means "whatever the backend chose", not a number repeated here."""
        monkeypatch.setattr(
            "backend.core.postgres_backend.PostgresGraphPersistenceBackend",
            StubBackend,
        )
        config = AppConfig(
            graph_file="graph.json",
            graph_backend="postgres",
            graph_postgres_dsn="postgresql:///example",
        )
        backend = build_persistence_backend(config)
        assert "pool_size" not in backend.kwargs


class TestTheServerUsesWhatWasSelected:
    """The assertion the whole task exists for.

    Selection that builds the right object and then hands `GraphStorage`
    nothing is the exact bug this change fixes, and it is invisible to every
    test above.
    """

    def test_a_selected_backend_is_the_one_the_graph_runs_on(
        self, monkeypatch, tmp_path
    ):
        from backend.api_host import AppConfig as Cfg, create_app

        monkeypatch.setattr(
            "backend.core.postgres_backend.PostgresGraphPersistenceBackend",
            StubBackend,
        )
        config = Cfg(
            graph_file=str(tmp_path / "graph.json"),
            sessions_dir=str(tmp_path / "sessions"),
            web_static_path=str(tmp_path / "web"),
            widget_static_path=str(tmp_path / "widget"),
            graph_backend="postgres",
            graph_postgres_dsn="postgresql:///example",
        )
        app = create_app(config)

        backend = app.state.graph_storage._persistence_backend
        assert isinstance(backend, StubBackend), (
            "the server built the configured backend and then did not use it"
        )

    def test_the_default_server_is_still_file_backed(self, tmp_path):
        from backend.api_host import AppConfig as Cfg, create_app
        from backend.core.storage_backends import FileGraphPersistenceBackend

        config = Cfg(
            graph_file=str(tmp_path / "graph.json"),
            sessions_dir=str(tmp_path / "sessions"),
            web_static_path=str(tmp_path / "web"),
            widget_static_path=str(tmp_path / "widget"),
        )
        app = create_app(config)

        assert isinstance(
            app.state.graph_storage._persistence_backend,
            FileGraphPersistenceBackend,
        )
