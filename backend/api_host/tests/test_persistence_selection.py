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
# (see backend/core/tests/test_traversal_equivalence.py). It goes on every
# test that reaches the real backend module -- which includes the ones that
# only monkeypatch it: pytest's string-form setattr IMPORTS the target module,
# and postgres_backend imports psycopg at module scope, so those tests need
# the driver too even though they never open a connection. Verified rather
# than assumed. The tests that build an AppConfig and call the factory for a
# file or refused backend genuinely do not, and a module-level skip would make
# those vanish silently on a clone without the optional extra.
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

    @requires_backend_module
    def test_postgres_settings_reach_the_backend(self, monkeypatch):
        monkeypatch.setattr(
            "backend.core.postgres_backend.PostgresGraphPersistenceBackend",
            StubBackend,
        )
        config = AppConfig(
            graph_file="graph.json",
            graph_backend="postgres",
            graph_postgres_dsn="postgresql://u:pw@db.example:5432/one",
            graph_postgres_schema="corp",
            graph_postgres_pool_size=3,
        )
        backend = build_persistence_backend(config)

        assert isinstance(backend, StubBackend)
        # Against the config, not a literal this file repeats: a backend built
        # with a hardcoded DSN equal to the shared fixture would satisfy a
        # literal comparison while discarding what the deployment configured.
        assert backend.conninfo == config.graph_postgres_dsn
        assert backend.kwargs["schema"] == config.graph_postgres_schema
        assert backend.kwargs["pool_size"] == config.graph_postgres_pool_size

    @requires_backend_module
    def test_a_second_distinct_dsn_also_reaches_the_backend(self, monkeypatch):
        """One hardcoded constant cannot satisfy two different DSNs."""
        monkeypatch.setattr(
            "backend.core.postgres_backend.PostgresGraphPersistenceBackend",
            StubBackend,
        )
        config = AppConfig(
            graph_file="graph.json",
            graph_backend="postgres",
            graph_postgres_dsn="postgresql://other@elsewhere:6543/two",
        )
        backend = build_persistence_backend(config)
        assert backend.conninfo == "postgresql://other@elsewhere:6543/two"

    @requires_backend_module
    def test_a_padded_dsn_is_stripped_before_it_reaches_libpq(self, monkeypatch):
        """Detecting the whitespace without removing it is worse than not looking.

        libpq parses a string as a URI only when it STARTS with
        `postgresql://` (or `postgres://`). One leading space demotes it to
        keyword/value
        parsing, and the resulting error quotes the whole connection string
        back — password included — into the process log, which the comment on
        `AppConfig.graph_postgres_dsn` promises never happens. Verified: the
        padded DSN raises `missing "=" after "postgresql://u:pw@..."` while
        the same DSN unpadded reports only a refused connection.
        """
        from psycopg.conninfo import conninfo_to_dict

        monkeypatch.setattr(
            "backend.core.postgres_backend.PostgresGraphPersistenceBackend",
            StubBackend,
        )
        config = AppConfig(
            graph_file="graph.json",
            graph_backend="postgres",
            graph_postgres_dsn="  postgresql://u:pw@db.example/graph\n",
        )
        backend = build_persistence_backend(config)

        assert backend.conninfo == "postgresql://u:pw@db.example/graph"
        # The property that matters is not the equality above but that libpq
        # accepts it as a URI at all.
        assert conninfo_to_dict(backend.conninfo)["password"] == "pw"

    @requires_backend_module
    @pytest.mark.parametrize(("schema", "pool_size"), [("corp", 1), ("tenant_b", 7)])
    def test_a_second_schema_and_pool_size_reach_the_backend_too(
        self, monkeypatch, schema, pool_size
    ):
        """No single hardcoded constant can satisfy both cases.

        `pool_size=1` also pins the refusal boundary from the accepting side:
        the guard refuses below 1 and its message says "at least 1", so a
        `<= 1` would contradict the text while every refusing test still
        passed.
        """
        monkeypatch.setattr(
            "backend.core.postgres_backend.PostgresGraphPersistenceBackend",
            StubBackend,
        )
        config = AppConfig(
            graph_file="graph.json",
            graph_backend="postgres",
            graph_postgres_dsn="postgresql:///example",
            graph_postgres_schema=schema,
            graph_postgres_pool_size=pool_size,
        )
        backend = build_persistence_backend(config)
        assert backend.kwargs["schema"] == schema
        assert backend.kwargs["pool_size"] == pool_size

    @requires_backend_module
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

    @requires_backend_module
    @pytest.mark.parametrize("scope", ["scope-a", "0198c1d4", " padded ", "MiXeD"])
    def test_a_scope_reaches_the_backend(self, monkeypatch, scope):
        """Passed through as given: an opaque identifier, not something this
        layer parses, shortens or lower-cases on the way.

        The padded and mixed-case cases are the ones with teeth. A `.strip()`
        anywhere on this path would make `GRAPH_POSTGRES_SCOPE=" a "` and
        `scope=" a "` two different scopes for one operator input, and the
        rows written under one unreachable under the other."""
        monkeypatch.setattr(
            "backend.core.postgres_backend.PostgresGraphPersistenceBackend",
            StubBackend,
        )
        config = AppConfig(
            graph_file="graph.json",
            graph_backend="postgres",
            graph_postgres_dsn="postgresql:///example",
            graph_postgres_scope=scope,
        )
        backend = build_persistence_backend(config)
        assert backend.kwargs["scope"] == scope

    @requires_backend_module
    def test_no_scope_is_not_passed_at_all(self, monkeypatch):
        """Unset constructs the backend with exactly the arguments it took
        before this setting existed, rather than with an explicit None that a
        future default would have to keep agreeing with."""
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
        assert "scope" not in backend.kwargs

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_a_set_but_empty_scope_is_refused_at_boot(self, blank):
        """Refused here, because the backend never sees it.

        An empty identifier keeps nothing apart, so treating it as "no scope
        wanted" would run an instance with no isolation while its configuration
        says it has some. The backend's own constructor refuses an empty scope
        for the same reason and cannot be reached with one, so the guard has to
        be where the operator's variable can be named.
        """
        config = AppConfig(
            graph_file="graph.json",
            graph_backend="postgres",
            graph_postgres_dsn="postgresql:///example",
            graph_postgres_scope=blank,
        )
        with pytest.raises(PersistenceConfigurationError) as exc:
            build_persistence_backend(config)
        assert "GRAPH_POSTGRES_SCOPE" in str(exc.value)

    @pytest.mark.parametrize("backend", ["file", ""])
    def test_a_scope_against_a_backend_that_cannot_hold_one_is_refused(self, backend):
        """Including the DEFAULT backend, which is the dangerous one.

        `GRAPH_BACKEND` unset with a scope set is a deployment that asked to be
        separated and would have been handed the file backend, which has no
        scope column and no policy - silently, since the factory returns before
        it ever looks at the scope.
        """
        config = AppConfig(
            graph_file="graph.json",
            graph_backend=backend or "file",
            graph_postgres_scope="scope-a",
        )
        with pytest.raises(PersistenceConfigurationError) as exc:
            build_persistence_backend(config)
        assert "GRAPH_POSTGRES_SCOPE" in str(exc.value)

    def test_an_unknown_backend_is_still_named_before_the_scope(self):
        """Two things wrong at once: the operator hears about the typo.

        A misspelled backend name is the error that explains the other one, and
        reporting the scope instead would send them looking at the wrong
        variable.
        """
        config = AppConfig(
            graph_file="graph.json",
            graph_backend="postgresql",
            graph_postgres_scope="scope-a",
        )
        with pytest.raises(PersistenceConfigurationError) as exc:
            build_persistence_backend(config)
        assert "GRAPH_BACKEND" in str(exc.value)
        assert "GRAPH_POSTGRES_SCOPE" not in str(exc.value)


class TestTheServerUsesWhatWasSelected:
    """The assertion the whole task exists for.

    Selection that builds the right object and then hands `GraphStorage`
    nothing is the exact bug this change fixes, and it is invisible to every
    test above.
    """

    @requires_backend_module
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

    def test_scope_is_read_from_the_environment(self, monkeypatch):
        monkeypatch.setenv("GRAPH_POSTGRES_SCOPE", "scope-a")
        assert AppConfig().graph_postgres_scope == "scope-a"

    def test_scope_unset_is_none(self, monkeypatch):
        monkeypatch.delenv("GRAPH_POSTGRES_SCOPE", raising=False)
        assert AppConfig().graph_postgres_scope is None

    @pytest.mark.parametrize("raw", ["", "   ", " padded "])
    def test_a_scope_survives_the_environment_intact(self, monkeypatch, raw):
        """Read as configured, then refused or used — never quietly rewritten.

        The one setting here that does NOT normalise. Empty reading as unset
        would hand a deployment that asked to be separated an instance with no
        isolation, and stripping would make the operator's value and the stored
        scope two different identifiers; `build_persistence_backend` refuses
        the empty one by name instead.
        """
        monkeypatch.setenv("GRAPH_POSTGRES_SCOPE", raw)
        assert AppConfig().graph_postgres_scope == raw

    def test_pool_size_is_read_as_an_integer(self, monkeypatch):
        monkeypatch.setenv("GRAPH_POSTGRES_POOL_SIZE", "6")
        assert AppConfig().graph_postgres_pool_size == 6

    def test_pool_size_unset_is_none_not_a_number_repeated_here(self, monkeypatch):
        monkeypatch.delenv("GRAPH_POSTGRES_POOL_SIZE", raising=False)
        assert AppConfig().graph_postgres_pool_size is None

    def test_a_negative_pool_size_survives_the_environment_intact(self, monkeypatch):
        """Read as configured, then refused — never quietly rewritten.

        Every other bad-pool-size test passes the value as a keyword and so
        never runs the default_factory. An abs() there would turn -3 into a
        working 3 and the operator would never learn their value was wrong.
        """
        monkeypatch.setenv("GRAPH_POSTGRES_POOL_SIZE", "-3")
        config = AppConfig(
            graph_file="graph.json",
            graph_backend="postgres",
            graph_postgres_dsn="postgresql:///example",
        )
        assert config.graph_postgres_pool_size == -3
        with pytest.raises(PersistenceConfigurationError) as exc:
            build_persistence_backend(config)
        assert "-3" in str(exc.value)

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_an_empty_pool_size_reads_as_unset(self, monkeypatch, blank):
        """`GRAPH_POSTGRES_POOL_SIZE=` is templated out, not set to nothing.

        The case `GRAPH_BACKEND` already handles above. Without it, int("")
        raises a bare ValueError inside a dataclass default_factory, naming
        no variable the operator can act on.
        """
        monkeypatch.setenv("GRAPH_POSTGRES_POOL_SIZE", blank)
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
        message = str(exc.value)
        assert name in message
        # The literal names, not `', '.join(SUPPORTED_BACKENDS)`: asserting
        # against the constant is circular and survives shrinking it. This is
        # the only thing telling an operator what to type instead.
        assert "file" in message
        assert "postgres" in message

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
        message = str(exc.value)
        assert "GRAPH_POSTGRES_POOL_SIZE" in message
        assert str(size) in message, "the message should echo what was set"


class TestRefusalOrder:
    """Which misconfiguration is reported when more than one is present.

    Both of these were unobservable to the suite: the round-3 relocation of
    the pool-size guard above the psycopg import, and the order of the DSN
    and pool-size guards relative to each other. A source comment claiming a
    property is not the same as a test holding it — this branch has now been
    caught out by that three times.
    """

    def _refuse_the_backend_import(self, monkeypatch):
        import builtins

        real_import = builtins.__import__

        def refuse(name, *args, **kwargs):
            if name == "backend.core.postgres_backend":
                raise ImportError("No module named 'psycopg'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", refuse)

    def test_a_bad_pool_size_is_diagnosable_without_the_driver(self, monkeypatch):
        """The whole point of checking the pool size before the import.

        With the guard back below it, the operator is told to install psycopg
        when what is actually wrong is a number they set.
        """
        self._refuse_the_backend_import(monkeypatch)
        config = AppConfig(
            graph_file="graph.json",
            graph_backend="postgres",
            graph_postgres_dsn="postgresql:///example",
            graph_postgres_pool_size=0,
        )
        with pytest.raises(PersistenceConfigurationError) as exc:
            build_persistence_backend(config)
        message = str(exc.value)
        assert "GRAPH_POSTGRES_POOL_SIZE" in message
        assert "requirements-postgres.txt" not in message

    def test_a_missing_dsn_outranks_a_bad_pool_size(self):
        """With both wrong, the more fundamental one is named.

        A pool size cannot be acted on by an operator who has no connection
        string yet, so the DSN is the useful thing to report first.
        """
        config = AppConfig(
            graph_file="graph.json",
            graph_backend="postgres",
            graph_postgres_dsn="",
            graph_postgres_pool_size=0,
        )
        with pytest.raises(PersistenceConfigurationError) as exc:
            build_persistence_backend(config)
        assert "GRAPH_POSTGRES_DSN" in str(exc.value)


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
