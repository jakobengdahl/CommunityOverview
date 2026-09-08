"""A PostgreSQL persistence backend, for running more than one instance.

The file backend is the default and stays that way: clone-and-run needs no
database. This backend exists for the deployment the file backend cannot
serve - several application instances sharing one graph - because a file is
rewritten whole by whichever instance saved last, and on a FUSE-mounted
object store its locking gives no protection at all.

PostgreSQL is a client/server database, which is the property that matters
here: ten autoscaled instances are ten *clients* of one server named by the
connection string, not ten copies of a store. That is why an embedded
database (SQLite, DuckDB) is not an alternative for this - each instance
would open its own writer on a shared file.

This module is imported only by whoever chooses this backend. Nothing in the
always-imported path touches it, so `psycopg` stays an optional dependency
(`backend/requirements-postgres.txt`) and a base install is unaffected.

This slice implements the snapshot contract only. The per-entity operations
and cross-instance change notification are the following slices; until then
the backend declares SNAPSHOT_ONLY and `GraphStorage` drives it with
whole-graph writes, exactly as it drives any snapshot-only backend.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional

import psycopg
from psycopg import sql
from psycopg_pool import ConnectionPool

from backend.core.storage_backends import SNAPSHOT_ONLY, BackendCapabilities

# Every instance runs the same migration on boot, so they race. The lock is
# taken for the duration of the migrating transaction and released with it -
# no unlock to leak on an exception. The key is an arbitrary fixed constant,
# not derived from anything; it only has to be one this application uses
# nowhere else.
MIGRATION_LOCK_KEY = 4_872_015_733_882_119_001

# Connections are the resource that scales with instance count, and the
# server's ceiling is shared by every instance at once: stock PostgreSQL
# allows 100, three of them reserved for superusers. Ten instances at ten
# connections each would exhaust that on their own, before anything else
# connects. So the default per-instance pool is deliberately small, and a
# deployment that raises it should divide the server's max_connections by the
# instance count it scales to, leaving room for the cross-instance listener a
# later slice adds - that one is held open per instance, outside the pool.
DEFAULT_POOL_SIZE = 4


class PostgresGraphPersistenceBackend:
    """The graph in PostgreSQL: nodes, edges and metadata as JSONB rows.

    JSONB rather than columns per field, because the graph's schema is
    configuration (`config/*/schema_config.json`), not something this table
    should have an opinion about. It is the same payload the file backend
    writes, so the two stores hold the same shape.

    A `schema` other than the default keeps one database serving several
    independent graphs, which is also how the tests give each case a store of
    its own without a database each.
    """

    def __init__(
        self,
        conninfo: str,
        *,
        schema: str = "public",
        pool_size: int = DEFAULT_POOL_SIZE,
        graph_name: str = "graph",
        pool: Optional[ConnectionPool] = None,
    ):
        if pool_size < 1:
            raise ValueError("pool_size must be at least 1")
        self.conninfo = conninfo
        self.schema = schema
        self._graph_name = graph_name
        # min_size 0: a backend that is constructed and never used holds no
        # connection. The contract creates backends freely, and so does an
        # instance that boots against a store it turns out not to read.
        self._pool = pool or ConnectionPool(
            conninfo, min_size=0, max_size=pool_size, open=True
        )
        self._owns_pool = pool is None
        self._migrated = False
        self._migrate_lock = threading.Lock()

    # -- schema --------------------------------------------------------------

    def _table(self, name: str) -> sql.Composed:
        return sql.SQL("{}.{}").format(
            sql.Identifier(self.schema), sql.Identifier(name)
        )

    def _ensure_schema(self) -> None:
        """Create the tables if they are not there, safely under concurrency.

        Autoscaling means N instances boot at once and every one of them runs
        this. Without the advisory lock each would check, find nothing, and
        try to create: all but the first fail on the table another instance
        created in between, and an instance that fails here fails to start.
        `IF NOT EXISTS` alone does not close it either - two concurrent
        `CREATE TABLE IF NOT EXISTS` on the same name can still raise a
        duplicate-key error from the catalog insert.
        """
        if self._migrated:
            return
        with self._migrate_lock:
            if self._migrated:
                return
            with self._pool.connection() as conn:
                with conn.transaction():
                    conn.execute(
                        "SELECT pg_advisory_xact_lock(%s)", (MIGRATION_LOCK_KEY,)
                    )
                    conn.execute(
                        sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                            sql.Identifier(self.schema)
                        )
                    )
                    for table in ("graph_nodes", "graph_edges"):
                        conn.execute(
                            sql.SQL(
                                "CREATE TABLE IF NOT EXISTS {} ("
                                "  id text PRIMARY KEY,"
                                "  doc jsonb NOT NULL"
                                ")"
                            ).format(self._table(table))
                        )
                    # One row, enforced by the primary key on a column that
                    # can only hold true. Its presence is what `exists()`
                    # reads: the tables are created on boot, so their being
                    # there says nothing about whether a graph was ever saved.
                    conn.execute(
                        sql.SQL(
                            "CREATE TABLE IF NOT EXISTS {} ("
                            "  only_row boolean PRIMARY KEY DEFAULT true"
                            "    CHECK (only_row),"
                            "  doc jsonb NOT NULL"
                            ")"
                        ).format(self._table("graph_metadata"))
                    )
            self._migrated = True

    # -- snapshot contract ---------------------------------------------------

    def capabilities(self) -> BackendCapabilities:
        return SNAPSHOT_ONLY

    def exists(self) -> bool:
        self._ensure_schema()
        with self._pool.connection() as conn:
            row = conn.execute(
                sql.SQL("SELECT 1 FROM {} LIMIT 1").format(
                    self._table("graph_metadata")
                )
            ).fetchone()
        return row is not None

    def load_graph_data(self) -> Dict[str, Any]:
        self._ensure_schema()
        with self._pool.connection() as conn:
            nodes = [
                row[0]
                for row in conn.execute(
                    sql.SQL("SELECT doc FROM {} ORDER BY id").format(
                        self._table("graph_nodes")
                    )
                )
            ]
            edges = [
                row[0]
                for row in conn.execute(
                    sql.SQL("SELECT doc FROM {} ORDER BY id").format(
                        self._table("graph_edges")
                    )
                )
            ]
            row = conn.execute(
                sql.SQL("SELECT doc FROM {} LIMIT 1").format(
                    self._table("graph_metadata")
                )
            ).fetchone()
        # Freshly decoded from the rows on every call, so the dict is the
        # caller's: GraphStorage rewrites it in place (timestamps become
        # datetimes) and must not be rewriting the store.
        return {
            "nodes": nodes,
            "edges": edges,
            "metadata": dict(row[0]) if row else {},
        }

    def save_graph_data(self, data: Dict[str, Any]) -> None:
        """Replace the whole graph, in one transaction.

        Atomicity is the database's, not something this has to build: a
        reader sees the previous graph until the commit, and a failure
        part-way - including one raised while serialising a row - rolls the
        whole thing back rather than leaving half a graph.
        """
        self._ensure_schema()
        nodes = list(data.get("nodes") or [])
        edges = list(data.get("edges") or [])
        metadata = dict(data.get("metadata") or {})
        with self._pool.connection() as conn:
            with conn.transaction():
                for table in ("graph_nodes", "graph_edges"):
                    conn.execute(sql.SQL("DELETE FROM {}").format(self._table(table)))
                conn.cursor().executemany(
                    sql.SQL("INSERT INTO {} (id, doc) VALUES (%s, %s)").format(
                        self._table("graph_nodes")
                    ),
                    [(node["id"], psycopg.types.json.Jsonb(node)) for node in nodes],
                )
                conn.cursor().executemany(
                    sql.SQL("INSERT INTO {} (id, doc) VALUES (%s, %s)").format(
                        self._table("graph_edges")
                    ),
                    [(edge["id"], psycopg.types.json.Jsonb(edge)) for edge in edges],
                )
                conn.execute(
                    sql.SQL(
                        "INSERT INTO {} (only_row, doc) VALUES (true, %s) "
                        "ON CONFLICT (only_row) DO UPDATE SET doc = EXCLUDED.doc"
                    ).format(self._table("graph_metadata")),
                    (psycopg.types.json.Jsonb(metadata),),
                )

    def default_graph_name(self) -> str:
        return self._graph_name

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        """Release the pool's connections.

        Not part of the persistence contract - no backend is asked to close -
        but a pool holds server connections, and the server's ceiling is the
        scarce resource here. A caller that creates backends repeatedly
        (tests, a script) should close them; a long-lived application
        instance holds one for its lifetime by design.
        """
        if self._owns_pool:
            self._pool.close()


__all__ = [
    "PostgresGraphPersistenceBackend",
    "DEFAULT_POOL_SIZE",
    "MIGRATION_LOCK_KEY",
]
