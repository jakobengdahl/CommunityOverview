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

import importlib.util
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

from backend.api_host.config import AppConfig
from backend.api_host.persistence import (
    PersistenceConfigurationError,
    build_persistence_backend,
)
from backend.core.storage_backends import BackendCapabilities


# Mirrors the guard every other PostgreSQL-touching module in this repo uses
# (see backend/core/tests/test_traversal_equivalence.py). Only the tests that
# reach the real backend module carry it; the rest need no driver, and a
# module-level skip would make them vanish silently on a clone without the
# optional extra.
_REQUIRE_POSTGRES = os.environ.get("CO_REQUIRE_POSTGRES") == "1"
_HAS_PSYCOPG = importlib.util.find_spec("psycopg") is not None

requires_backend_module = pytest.mark.skipif(
    not _HAS_PSYCOPG and not _REQUIRE_POSTGRES,
    reason="psycopg is an optional dependency; CO_REQUIRE_POSTGRES=1 makes a skip here a failure",
)

REPO_ROOT = Path(__file__).resolve().parents[3]


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


class TestTheEnvironmentIsTheInterface:
    """The variable names are the deployment contract, so they get pinned.

    Every test above builds `AppConfig(...)` by keyword, which leaves the
    `default_factory` half — the only half a deployment exercises — unread.
    Renaming any of these four variables would make the field permanently
    unreachable from a container while the suite stayed green and this PR's
    own documentation went quietly false.
    """

    def test_graph_backend_is_read_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("GRAPH_BACKEND", "postgres")
        assert AppConfig().graph_backend == "postgres"

    def test_graph_backend_is_normalised(self, monkeypatch):
        """A Secret Manager value or a YAML block scalar brings whitespace."""
        monkeypatch.setenv("GRAPH_BACKEND", "  Postgres \n")
        assert AppConfig().graph_backend == "postgres"

    @pytest.mark.parametrize("blank", ["", "   ", "\n"])
    def test_an_empty_graph_backend_means_unset_not_unrecognised(
        self, monkeypatch, blank
    ):
        """`GRAPH_BACKEND=` is how a variable is templated out, not a typo.

        It reads as "unset" to whoever wrote the manifest, so it must select
        the file default rather than refuse to boot. Sibling fields in the
        same dataclass already read an empty value this way.
        """
        monkeypatch.setenv("GRAPH_BACKEND", blank)
        config = AppConfig()
        assert config.graph_backend == "file"
        assert build_persistence_backend(config) is None

    def test_dsn_is_read_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("GRAPH_POSTGRES_DSN", "postgresql:///from-env")
        assert AppConfig().graph_postgres_dsn == "postgresql:///from-env"

    def test_schema_is_read_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("GRAPH_POSTGRES_SCHEMA", "corp")
        assert AppConfig().graph_postgres_schema == "corp"

    def test_schema_defaults_to_public(self, monkeypatch):
        monkeypatch.delenv("GRAPH_POSTGRES_SCHEMA", raising=False)
        assert AppConfig().graph_postgres_schema == "public"

    def test_pool_size_is_read_as_an_integer(self, monkeypatch):
        monkeypatch.setenv("GRAPH_POSTGRES_POOL_SIZE", "6")
        assert AppConfig().graph_postgres_pool_size == 6

    def test_pool_size_unset_is_none_not_a_number_repeated_here(self, monkeypatch):
        monkeypatch.delenv("GRAPH_POSTGRES_POOL_SIZE", raising=False)
        assert AppConfig().graph_postgres_pool_size is None


class TestRefusals:
    """Each misconfiguration names the variable the operator can change."""

    @pytest.mark.parametrize(
        "name", ["postgresql", "sqlite", "none", "Postgres!", "file2"]
    )
    def test_every_unknown_name_is_refused(self, name):
        """Pinned by a set, not by the single string that first motivated it."""
        config = AppConfig(graph_file="graph.json", graph_backend=name)
        with pytest.raises(PersistenceConfigurationError) as exc:
            build_persistence_backend(config)
        assert name in str(exc.value)

    @pytest.mark.parametrize("dsn", ["", "   "])
    def test_an_empty_dsn_is_no_dsn(self, dsn):
        """An unset secret rendered into the environment arrives as ""."""
        config = AppConfig(
            graph_file="graph.json",
            graph_backend="postgres",
            graph_postgres_dsn=dsn,
        )
        with pytest.raises(PersistenceConfigurationError) as exc:
            build_persistence_backend(config)
        assert "GRAPH_POSTGRES_DSN" in str(exc.value)

    @pytest.mark.parametrize("size", [0, -1])
    def test_an_unusable_pool_size_names_its_own_variable(self, size):
        """Not the backend's `pool_size must be at least 1`, which names
        an argument the operator never set."""
        config = AppConfig(
            graph_file="graph.json",
            graph_backend="postgres",
            graph_postgres_dsn="postgresql:///example",
            graph_postgres_pool_size=size,
        )
        with pytest.raises(PersistenceConfigurationError) as exc:
            build_persistence_backend(config)
        assert "GRAPH_POSTGRES_POOL_SIZE" in str(exc.value)


class TestTheServerPropagatesAConfigurationError:
    """ "At boot" only means something at the layer that boots.

    `build_persistence_backend` raising is not the guarantee; `create_app`
    refusing to return is. A `try/except` around the call in server.py would
    restore the silent file fallback and every other test here would pass.
    """

    def _config(self, tmp_path, **overrides):
        from backend.api_host import AppConfig as Cfg

        return Cfg(
            graph_file=str(tmp_path / "graph.json"),
            sessions_dir=str(tmp_path / "sessions"),
            web_static_path=str(tmp_path / "web"),
            widget_static_path=str(tmp_path / "widget"),
            **overrides,
        )

    def test_an_unknown_backend_stops_the_server_starting(self, tmp_path):
        from backend.api_host import create_app

        with pytest.raises(PersistenceConfigurationError):
            create_app(self._config(tmp_path, graph_backend="postgresql"))

    def test_postgres_without_a_dsn_stops_the_server_starting(self, tmp_path):
        from backend.api_host import create_app

        with pytest.raises(PersistenceConfigurationError):
            create_app(self._config(tmp_path, graph_backend="postgres"))


class TestPsycopgStaysOffTheAlwaysImportedPath:
    """G6, which three separate passages of prose assert and nothing checked.

    CI cannot catch a hoisted import by failing to install psycopg, because
    backend/requirements-dev.txt pulls the postgres extra in — so the driver
    is always importable wherever pytest runs. A subprocess asking
    `sys.modules` is the only thing that can tell.
    """

    def test_importing_the_api_host_does_not_import_the_postgres_backend(self):
        probe = (
            "import backend.api_host, sys; "
            "leaked = [m for m in ('backend.core.postgres_backend', 'psycopg') "
            "if m in sys.modules]; "
            "print(','.join(leaked))"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr
        leaked = result.stdout.strip()
        assert leaked == "", (
            f"importing backend.api_host pulled in {leaked}; psycopg is an "
            "optional dependency and must stay off the always-imported path"
        )


class TestTheFactoryMatchesTheRealBackend:
    """The stub absorbs any keyword, so it cannot catch signature drift.

    Renaming `schema` on PostgresGraphPersistenceBackend would leave every
    stub-based test green while production raised TypeError at boot. This
    binds the factory's keywords against the real signature.
    """

    @requires_backend_module
    def test_the_factory_keywords_bind_to_the_real_constructor(self):
        import inspect

        from backend.core.postgres_backend import PostgresGraphPersistenceBackend

        signature = inspect.signature(PostgresGraphPersistenceBackend.__init__)
        # Raises TypeError if the factory's keywords stop matching.
        signature.bind(
            None,
            "postgresql:///example",
            schema="corp",
            pool_size=3,
        )
