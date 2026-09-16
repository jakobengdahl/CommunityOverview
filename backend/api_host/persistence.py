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

    if backend == FILE_BACKEND:
        return None

    if backend != POSTGRES_BACKEND:
        raise PersistenceConfigurationError(
            f"GRAPH_BACKEND={backend!r} is not a backend this build knows. "
            f"Supported: {', '.join(SUPPORTED_BACKENDS)}."
        )

    # Falsiness rather than `is None`: an unset secret rendered into the
    # environment arrives as "", and an empty DSN is no DSN.
    if not config.graph_postgres_dsn or not config.graph_postgres_dsn.strip():
        raise PersistenceConfigurationError(
            "GRAPH_BACKEND=postgres needs GRAPH_POSTGRES_DSN, which is unset "
            "or empty. It is the libpq connection string for the graph store."
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
    pool_size = config.graph_postgres_pool_size
    if pool_size is not None:
        # The backend rejects this too, but its ValueError names `pool_size`,
        # which is not what an operator set. Refuse here so the message names
        # the variable they can actually change.
        if pool_size < 1:
            raise PersistenceConfigurationError(
                f"GRAPH_POSTGRES_POOL_SIZE={pool_size} is not usable; it is "
                "the number of connections this instance may hold, so it must "
                "be at least 1."
            )
        kwargs["pool_size"] = pool_size

    return PostgresGraphPersistenceBackend(config.graph_postgres_dsn, **kwargs)
