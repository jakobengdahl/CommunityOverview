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

    if backend == FILE_BACKEND:
        return None

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

    return PostgresGraphPersistenceBackend(dsn, **kwargs)
