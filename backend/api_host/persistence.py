"""Construct the graph persistence backend the configuration asks for.

This is the step that was missing between the PostgreSQL backend and a
deployment that can use it. `backend/core/postgres_backend.py` has existed
since PR #564 and nothing outside the tests and `scripts/measure_capacity.py`
ever reached it, so `GraphStorage` fell back to the file backend in every
deployment regardless of configuration.

Kept out of `config.py` on purpose. `AppConfig` is data plus path derivation
and is imported everywhere; importing a storage backend from it would put
`psycopg` on the always-imported path and undo the property
`postgres_backend.py` documents about itself. The import here is inside the
function for the same reason.
"""

from dataclasses import dataclass

from backend.core.layer_store import (
    DEFAULT_MERGE_RECORD_RETENTION_DAYS,
    MAX_MERGE_RECORD_RETENTION_DAYS,
)
from backend.core.write_modes import (
    DEFAULT_LAYER_MAX_ENTRIES,
    WRITE_MODE_DIRECT,
    WRITE_MODE_STAGED,
    WRITE_MODES,
)

from .config import AppConfig


FILE_BACKEND = "file"
POSTGRES_BACKEND = "postgres"
SUPPORTED_BACKENDS = (FILE_BACKEND, POSTGRES_BACKEND)


class PersistenceConfigurationError(RuntimeError):
    """The configured backend cannot be built, and the message says why.

    Raised at boot rather than on the first write. A graph server that starts
    and only then discovers it has nowhere to persist has already accepted
    requests it cannot honour.
    """


@dataclass(frozen=True)
class SessionSettings:
    """The staged-session settings, validated."""

    staged_sessions: bool = False
    default_write_mode: str = WRITE_MODE_DIRECT
    layer_max_entries: int = DEFAULT_LAYER_MAX_ENTRIES
    merge_record_retention_days: int = DEFAULT_MERGE_RECORD_RETENTION_DAYS


_TRUE = ("1", "true", "yes", "on")
_FALSE = ("", "0", "false", "no", "off")


def build_session_settings(config: AppConfig) -> SessionSettings:
    """The staged-session settings ``config`` asks for, or a boot error.

    A default of `staged` without a backend that can hold layers is refused
    here, naming both settings (§5.2): a deployment that asked for every
    session to stage would otherwise run with every session direct.
    """
    raw = (config.graph_staged_sessions or "").strip().lower()
    if raw not in _TRUE + _FALSE:
        raise PersistenceConfigurationError(
            f"GRAPH_STAGED_SESSIONS={config.graph_staged_sessions!r} is not a "
            "boolean; use true or false."
        )
    staged = raw in _TRUE

    mode = (config.session_write_mode_default or "").strip().lower() or (
        WRITE_MODE_DIRECT
    )
    if mode not in WRITE_MODES:
        raise PersistenceConfigurationError(
            f"SESSION_WRITE_MODE_DEFAULT={config.session_write_mode_default!r} is "
            f"not a write mode; use {' or '.join(WRITE_MODES)}."
        )
    if mode == WRITE_MODE_STAGED and not staged:
        raise PersistenceConfigurationError(
            "SESSION_WRITE_MODE_DEFAULT=staged needs a graph backend that can "
            "hold staged sessions, and GRAPH_STAGED_SESSIONS is not enabled. "
            "Set GRAPH_STAGED_SESSIONS=true, or unset the default."
        )

    cap_raw = (config.session_layer_max_entries or "").strip()
    cap = DEFAULT_LAYER_MAX_ENTRIES
    if cap_raw:
        try:
            cap = int(cap_raw)
        except ValueError:
            cap = 0
        if cap < 1:
            raise PersistenceConfigurationError(
                f"SESSION_LAYER_MAX_ENTRIES={config.session_layer_max_entries!r} "
                "is not usable; it is the most entries one session's layer may "
                "hold, so it must be a whole number of at least 1."
            )

    retention_raw = (config.session_merge_record_retention_days or "").strip()
    retention = DEFAULT_MERGE_RECORD_RETENTION_DAYS
    if retention_raw:
        try:
            retention = int(retention_raw)
        except ValueError:
            retention = 0
        if not 1 <= retention <= MAX_MERGE_RECORD_RETENTION_DAYS:
            raise PersistenceConfigurationError(
                "SESSION_MERGE_RECORD_RETENTION_DAYS="
                f"{config.session_merge_record_retention_days!r} is not usable; "
                "it is how many days a deleted session's merge records are "
                "kept, so it must be a whole number from 1 to "
                f"{MAX_MERGE_RECORD_RETENTION_DAYS}."
            )
    return SessionSettings(
        staged_sessions=staged,
        default_write_mode=mode,
        layer_max_entries=cap,
        merge_record_retention_days=retention,
    )


def build_persistence_backend(config: AppConfig):
    """The backend `config` selects, or None to keep GraphStorage's default.

    None means the file backend. Returning it rather than constructing a
    `FileGraphPersistenceBackend` here leaves the default path byte-identical
    to what it was before this module existed: `GraphStorage` builds its own
    from `json_path`, exactly as it always has. There is then no second
    construction site to drift from the first.
    """
    backend = config.graph_backend

    if backend not in SUPPORTED_BACKENDS:
        raise PersistenceConfigurationError(
            f"GRAPH_BACKEND={backend!r} is not a backend this build knows. "
            f"Supported: {', '.join(SUPPORTED_BACKENDS)}."
        )

    # Before the file backend returns, because both of these are a scope that
    # was asked for and cannot be honoured, and the failure they would
    # otherwise reach is silence: an instance running with no isolation at all
    # while its configuration says it has some. The backend refuses an empty
    # scope too, but it never sees one of these - the first is refused before
    # it is constructed and the second is never constructed at all - so the
    # guard has to be here, where the operator's variable can be named. The
    # pool-size guard below is the same shape for the same reason.
    scope = config.graph_postgres_scope
    if scope is not None and not scope.strip():
        raise PersistenceConfigurationError(
            "GRAPH_POSTGRES_SCOPE is set to an empty value. It is the opaque "
            "identifier that decides which rows this instance may see, so an "
            "empty one is not read as 'no scope wanted': unset the variable "
            "to run without one."
        )
    if scope is not None and backend != POSTGRES_BACKEND:
        raise PersistenceConfigurationError(
            f"GRAPH_POSTGRES_SCOPE is set, and GRAPH_BACKEND={backend!r} has "
            f"nowhere to apply it - the scope is a property of the PostgreSQL "
            f"graph tables. Either set GRAPH_BACKEND=postgres or unset the "
            f"scope; ignoring it would run this instance with no isolation "
            f"while the configuration says it has some."
        )

    staged = build_session_settings(config).staged_sessions

    if backend == FILE_BACKEND:
        if not staged:
            layers = config.get_graph_path()
            layers = layers.with_name(layers.stem + ".layers.json")
            if layers.exists():
                # Staged sessions were enabled here: booting without them
                # would let every staged session be written as direct.
                raise PersistenceConfigurationError(
                    f"GRAPH_STAGED_SESSIONS is not enabled, but {layers.name} "
                    "beside the graph holds staged sessions. Set "
                    "GRAPH_STAGED_SESSIONS=true; it cannot be turned off once "
                    "sessions have been staged."
                )
            return None
        from backend.core.storage_backends import FileGraphPersistenceBackend

        return FileGraphPersistenceBackend(
            config.get_graph_path(), staged_sessions=True
        )

    # Normalised once, then used. Detecting the whitespace without removing
    # it is worse than not looking: libpq treats a string as a URI only when
    # it STARTS with postgresql:// (or postgres://), so one leading space
    # demotes it to keyword/value parsing, and the resulting error quotes
    # the whole connection string back - password included - into the log.
    # An unset secret rendered into the environment also arrives as "", and
    # an empty DSN is no DSN.
    dsn = (config.graph_postgres_dsn or "").strip()
    if not dsn:
        raise PersistenceConfigurationError(
            "GRAPH_BACKEND=postgres needs GRAPH_POSTGRES_DSN, which is unset "
            "or empty. It is the libpq connection string for the graph store."
        )

    # Checked before the import, so a misconfiguration is diagnosable on a
    # clone without the optional extra. The backend rejects this too, but its
    # ValueError names `pool_size`, which is not what an operator set.
    pool_size = config.graph_postgres_pool_size
    if pool_size is not None and pool_size < 1:
        raise PersistenceConfigurationError(
            f"GRAPH_POSTGRES_POOL_SIZE={pool_size} is not usable; it is "
            "the number of connections this instance may hold, so it must "
            "be at least 1."
        )

    try:
        from backend.core.postgres_backend import PostgresGraphPersistenceBackend
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatch
        raise PersistenceConfigurationError(
            "GRAPH_BACKEND=postgres needs the psycopg extra, which is not "
            "installed. Install it with: "
            "pip install -r backend/requirements-postgres.txt"
        ) from exc

    kwargs = {"schema": config.graph_postgres_schema}
    if pool_size is not None:
        kwargs["pool_size"] = pool_size
    # Passed only when it was set, so a deployment that names no scope
    # constructs the backend with exactly the arguments it did before the
    # setting existed. Passed AS GIVEN: an opaque identifier is not this
    # layer's to trim.
    if scope is not None:
        kwargs["scope"] = scope
    if staged:
        kwargs["staged_sessions"] = True

    built = PostgresGraphPersistenceBackend(dsn, **kwargs)
    if not staged and built.holds_staged_sessions():
        built.close()
        # As for the file backend above: the records decide which sessions
        # stage, and an instance that cannot read them would write those
        # sessions as direct.
        raise PersistenceConfigurationError(
            "GRAPH_STAGED_SESSIONS is not enabled, but the PostgreSQL graph "
            "store holds staged sessions. Set GRAPH_STAGED_SESSIONS=true on "
            "every instance; it cannot be turned off once sessions have been "
            "staged."
        )
    return built
