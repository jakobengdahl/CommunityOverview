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

The backend declares `incremental_writes` and `transactions`, so a mutation
reaches it as the entity operations that describe it rather than as a
rewrite of the whole graph. That is what makes several writers safe: two
instances editing different parts of the graph now touch different rows and
do not contend, where whole-graph saves had the later one discard the
earlier one's work whatever it was.

What is still missing is the other direction. Nothing tells an instance that
another one wrote, so a running instance serves what it last loaded until it
reloads - correct, but stale. Cross-instance change notification is the next
slice; the seam for it already exists and this backend does not yet declare
it.

Two payload restrictions come from JSONB and are shared with neither the
file backend nor `graph.json`. A whole-graph save carrying one fails
entirely; an entity write fails only its own operation, which GraphStorage
answers by re-issuing the whole graph - so the value has to go either way:

- Non-finite floats. Python's `json` writes bare `NaN` and `Infinity` and
  reads them back; `jsonb` rejects them. This is the likelier of the two,
  because a backend with no vector sidecar receives every node's embedding
  inline, so one degenerate vector is enough.
- A NUL in a string. `\u0000` is valid JSON and round-trips through
  `graph.json`; `jsonb` cannot store it.
"""

from __future__ import annotations

import threading
from typing import Any, Dict, Sequence

import psycopg
from psycopg import sql
from psycopg_pool import ConnectionPool

from backend.core.storage_backends import BackendCapabilities, EntityOperation

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
        return BackendCapabilities(incremental_writes=True, transactions=True)

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

    # -- entity contract -----------------------------------------------------

    # A deadlock is transient by nature and the batch is atomic, so a retry
    # starts from the state the aborted one left - which is the state it
    # found. Small: the contention it resolves is between two writers, not a
    # queue of them.
    DEADLOCK_RETRIES = 3

    def upsert_node(self, node: Dict[str, Any]) -> None:
        self.apply_batch([EntityOperation.upsert_node(node)])

    def delete_node(self, node_id: str) -> None:
        self.apply_batch([EntityOperation.delete_node(node_id)])

    def upsert_edge(self, edge: Dict[str, Any]) -> None:
        self.apply_batch([EntityOperation.upsert_edge(edge)])

    def delete_edge(self, edge_id: str) -> None:
        self.apply_batch([EntityOperation.delete_edge(edge_id)])

    def apply_batch(self, operations: Sequence[EntityOperation]) -> None:
        """Apply the operations in order, in one transaction.

        This is where the shared store starts paying: a renamed node costs
        one row rather than a rewrite of every node, so two instances
        editing different parts of the graph stop overwriting each other.
        Atomicity is the database's - `transactions` is declared on the
        strength of this method, not approximated by a journal the way the
        file backend has to.

        The save's lock is taken here too, but in SHARE mode, so entity
        operations do not wait for each other - only for a whole-graph save,
        which takes it exclusively. Row locks alone are not enough, and the
        case that proves it is a delete. A save replaces a row rather than
        updating it (`DELETE` then `INSERT`), so a `DELETE ... WHERE id`
        that waited on the save's row lock unblocks to find its target tuple
        dead and the replacement outside its own statement snapshot: it
        removes nothing, reports nothing, and the caller is told the entity
        is gone while it is still there. Measured before this lock existed.
        An upsert survives the same interleaving because `ON CONFLICT` sees
        the new row, which is why the anomaly is easy to miss.

        The economy the entity path exists for is intact: two instances
        editing different entities still never wait for each other, because
        SHARE mode is only incompatible with the save's EXCLUSIVE mode.

        A batch takes one row lock per operation and holds them to commit,
        so two batches touching the same entities in opposite orders
        deadlock. The order is the caller's and cannot be sorted - a delete
        followed by an upsert of one id is not the same batch reordered - so
        the deadlock is retried instead. That is safe precisely because the
        batch is atomic: the aborted transaction left nothing behind. Left
        to propagate it would be worse than one lost mutation, because
        GraphStorage answers a failed entity write by re-issuing the whole
        graph, which is the overwrite this slice exists to eliminate.
        """
        self._ensure_schema()
        for attempt in range(self.DEADLOCK_RETRIES + 1):
            try:
                self._apply_batch_once(operations)
                return
            except psycopg.errors.DeadlockDetected:
                if attempt == self.DEADLOCK_RETRIES:
                    raise

    def _apply_batch_once(self, operations: Sequence[EntityOperation]) -> None:
        with self._pool.connection() as conn:
            with conn.transaction():
                # Same reason as the save: last-writer-wins on a contended
                # row is a READ COMMITTED behaviour. Under a REPEATABLE READ
                # default the second writer would abort with a serialization
                # failure instead of waiting and winning.
                conn.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                conn.execute(
                    "SELECT pg_advisory_xact_lock_shared(%s, hashtext(%s))",
                    (SAVE_LOCK_KEY, self.schema),
                )
                for operation in operations:
                    self._apply_one(conn, operation)

    def _apply_one(self, conn, operation: EntityOperation) -> None:
        table = "graph_nodes" if operation.kind == "node" else "graph_edges"
        if operation.action == "delete":
            conn.execute(
                sql.SQL("DELETE FROM {} WHERE id = %s").format(self._table(table)),
                (operation.entity_id,),
            )
            return
        conn.execute(
            sql.SQL(
                "INSERT INTO {} (id, doc) VALUES (%s, %s)"
                " ON CONFLICT (id) DO UPDATE SET doc = EXCLUDED.doc"
            ).format(self._table(table)),
            (operation.entity_id, psycopg.types.json.Jsonb(operation.payload)),
        )

    def checkpoint(self) -> None:
        """Nothing to fold in: every write above is already committed.

        The file backend needs this because it appends to a journal and only
        periodically rewrites the graph; a database has no such deferred
        state, so the canonical form is what is already there. Implemented
        rather than omitted because `capabilities_of` refuses a backend that
        declares `incremental_writes` without all six methods.
        """

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
