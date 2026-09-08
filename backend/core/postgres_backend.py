r"""A PostgreSQL persistence backend, for running more than one instance.

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

This slice implements the snapshot contract only, and that bounds what it
can promise. Whole-graph writes are serialised per store, so the store never
ends holding a graph nobody saved - but serialising a race does not resolve
it: two instances writing at once still means the later save discards what
the earlier one committed, because a whole-graph write says nothing about
what changed. **Until the per-entity slice lands, one writer at a time is
the limit**, and this backend's value so far is a shared store several
instances can *read* consistently, not one they can safely both write.

The per-entity operations and cross-instance change notification are the
following slices; until then the backend declares SNAPSHOT_ONLY and
`GraphStorage` drives it with whole-graph writes.

Two payload restrictions come from JSONB and are shared with neither the
file backend nor `graph.json`. Because writes are whole-graph, one offending
value stops the entire graph from persisting rather than one node:

- Non-finite floats. Python's `json` writes bare `NaN` and `Infinity` and
  reads them back; `jsonb` rejects them. This is the likelier of the two,
  because a backend with no vector sidecar receives every node's embedding
  inline, so one degenerate vector is enough.
- A NUL in a string. `\u0000` is valid JSON and round-trips through
  `graph.json`; `jsonb` cannot store it.
"""

from __future__ import annotations

import threading
from typing import Any, Dict

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

# Whole-graph saves are serialised per store. Two of them running at once do
# not merely race for last place: PostgreSQL's default isolation takes each
# statement's snapshot when the statement starts, so a DELETE that waited for
# another writer's commit skips the rows that writer deleted and never sees
# the rows it inserted. The store then ends holding the union of two saves -
# a graph neither instance ever wrote - or the second save dies on a
# duplicate key. Keyed per schema, so two graphs in one database do not wait
# for each other - and that two-argument form of the lock takes int4, not the
# bigint the one-argument form above accepts, so this key is deliberately
# smaller rather than arbitrarily so.
SAVE_LOCK_KEY = 1_872_015_733


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
    ):
        if pool_size < 1:
            raise ValueError("pool_size must be at least 1")
        self.conninfo = conninfo
        self.schema = schema
        self._graph_name = graph_name
        # min_size 0: a backend that is constructed and never used holds no
        # connection. The contract creates backends freely, and so does an
        # instance that boots against a store it turns out not to read.
        self._pool = ConnectionPool(conninfo, min_size=0, max_size=pool_size, open=True)
        self._migrated = False
        self._migrate_lock = threading.Lock()

    # -- schema --------------------------------------------------------------

    def _table(self, name: str) -> sql.Composed:
        return sql.SQL("{}.{}").format(
            sql.Identifier(self.schema), sql.Identifier(name)
        )

    def _create_missing(self, conn, table: str, columns: str) -> None:
        """Create one table, asking only when it is not already there.

        The catalog answers this for a role with nothing but DML, where a
        bare `CREATE TABLE IF NOT EXISTS` would raise instead - it checks
        CREATE on the schema before it checks whether the table is there.

        An exact match on the two catalog columns, not `to_regclass`: that
        function *parses* its argument as an SQL name, so it case-folds an
        unquoted part and splits on a dot. It answers "missing" for a table
        that exists under a mixed-case schema - dropping this guard for
        exactly the least-privilege role it was written for - and raises
        outright on a schema name containing a dot. The schema guard below
        matches `pg_namespace.nspname` for the same reason.
        """
        if conn.execute(
            "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            " WHERE n.nspname = %s AND c.relname = %s",
            (self.schema, table),
        ).fetchone():
            return
        conn.execute(
            sql.SQL("CREATE TABLE IF NOT EXISTS {} ({})").format(
                self._table(table), sql.SQL(columns)
            )
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
                    # Asked for only when it is actually missing. `CREATE
                    # SCHEMA IF NOT EXISTS` checks the caller's CREATE
                    # privilege on the *database* before it checks whether
                    # the schema is there, so it raises for a role that owns
                    # its own schema but holds nothing at database level -
                    # which is the ordinary least-privilege role on managed
                    # PostgreSQL, and it fails at boot rather than at a write.
                    # It bites the default `public` too, so this is not an
                    # exotic-configuration guard.
                    if not conn.execute(
                        "SELECT 1 FROM pg_namespace WHERE nspname = %s",
                        (self.schema,),
                    ).fetchone():
                        conn.execute(
                            sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                                sql.Identifier(self.schema)
                            )
                        )
                    # Same guard, same reason: `CREATE TABLE IF NOT EXISTS`
                    # also checks CREATE on the *schema* before it checks
                    # whether the table is there. The operator who
                    # pre-provisions the tables and grants the app role DML
                    # only is the ordinary managed-PostgreSQL setup, and
                    # without this the instance dies at boot against a store
                    # it has every permission it actually needs on.
                    self._create_missing(
                        conn,
                        "graph_nodes",
                        "id text PRIMARY KEY, doc jsonb NOT NULL",
                    )
                    self._create_missing(
                        conn,
                        "graph_edges",
                        "id text PRIMARY KEY, doc jsonb NOT NULL",
                    )
                    # One row, enforced by the primary key on a column that
                    # can only hold true. Its presence is what `exists()`
                    # reads: the tables are created on boot, so their being
                    # there says nothing about whether a graph was ever saved.
                    self._create_missing(
                        conn,
                        "graph_metadata",
                        "only_row boolean PRIMARY KEY DEFAULT true"
                        " CHECK (only_row), doc jsonb NOT NULL",
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
        """The whole graph as one moment in time.

        Nodes, edges and metadata read separately are three moments, and
        another instance saving in between hands this one a graph that never
        existed: edges whose endpoints are not in the nodes it got. That is
        not a race to engineer - an instance loading while another saves is
        the normal case for the deployment this backend exists for, and
        PostgreSQL's default isolation takes a fresh snapshot per *statement*,
        not per transaction.

        REPEATABLE READ is what fixes it: the snapshot is taken once, at the
        first statement, and the three reads below share it. The obvious
        alternative - one statement with the three as subqueries - is a trap.
        It reads as cheaper, but `jsonb_agg` builds a single jsonb value, and
        a jsonb value cannot exceed 256 MB. Since a save writes one row per
        entity and has no such limit, a store would grow past that line and
        then be permanently unloadable by the instance that wrote it. With
        vectors carried inline, as the seam requires of a backend with no
        sidecar, that ceiling arrives at a few tens of thousands of nodes.

        `SET TRANSACTION ISOLATION LEVEL` is transaction-scoped, so nothing
        is left on the connection when it goes back to the pool.
        """
        self._ensure_schema()
        with self._pool.connection() as conn:
            with conn.transaction():
                # Must be the transaction's first statement.
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
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
                # Stated rather than inherited. The lock below only delivers
                # what it promises because the DELETE that follows takes a
                # fresh snapshot when the statement starts - a READ COMMITTED
                # property. Under a server, database or role default of
                # REPEATABLE READ the transaction's snapshot is taken here,
                # at the lock, before it blocks; the writer that waited then
                # sees the store as it was and dies on a serialization
                # failure instead of proceeding. The load pins its own level
                # for the opposite reason, so leaving this one to the
                # environment was an asymmetry, not a default.
                conn.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                # Before the first statement takes its snapshot, so a writer
                # that waited here re-reads the store the other one left.
                conn.execute(
                    "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                    (SAVE_LOCK_KEY, self.schema),
                )
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
        self._pool.close()


__all__ = [
    "PostgresGraphPersistenceBackend",
    "SAVE_LOCK_KEY",
    "DEFAULT_POOL_SIZE",
    "MIGRATION_LOCK_KEY",
]
