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

The other direction is `change_notification`, over the server's own
LISTEN/NOTIFY. A write announces itself on a channel derived from the schema,
inside the writing transaction, so the announcement is atomic with the write
and arrives only if it commits. Every other instance holds one listening
connection and refreshes what the announcement names. Without it an instance
would serve what it last loaded until it restarted - correct, but stale, and
a shared store that only one instance can read currently is not one.

Two payload restrictions come from JSONB and are shared with neither the
file backend nor `graph.json`. A whole-graph save carrying one fails
entirely; an entity write fails only the write that carries it - one
operation, or the whole batch it is in, since a batch is one transaction -
which GraphStorage answers by re-issuing the whole graph, so the value has
to go either way:

- Non-finite floats. Python's `json` writes bare `NaN` and `Infinity` and
  reads them back; `jsonb` rejects them. This is the likelier of the two,
  because a backend with no vector sidecar receives every node's embedding
  inline, so one degenerate vector is enough.
- A NUL in a string. `\u0000` is valid JSON and round-trips through
  `graph.json`; `jsonb` cannot store it.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import psycopg
from psycopg import sql
from psycopg_pool import ConnectionPool

from backend.core.storage_backends import (
    BackendCapabilities,
    EntityOperation,
    ExternalChange,
    ExternalChangeRefused,
)

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
# deployment that raises it should check that the server's max_connections
# covers instance_count * (pool_size + 1), which is what this backend
# actually asks for at full scale.
#
# The + 1 is the listening connection, and it cannot come from the pool.
# LISTEN registers on the session, so a pooled connection would stop
# listening the moment it was returned and start again somewhere unrelated;
# and the thread reading it blocks for as long as notification runs, which
# would hold a pooled connection out of circulation for just as long. At the
# default that is a quarter of the instance's write capacity; at pool_size=1
# it is all of it.
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

# A NOTIFY payload is capped at 8000 bytes by the server, and exceeding it
# raises InvalidParameterValue - inside the writing transaction, which would
# abort the write itself. So the cap is enforced here, before the statement
# is issued, and an announcement that will not fit degrades to "something
# changed" rather than failing the mutation it describes. Measured on
# PostgreSQL 16: 7999 bytes is accepted and 8000 is not.
NOTIFY_PAYLOAD_LIMIT = 8000

# How long the listening thread blocks in one read before looking at the
# stop flag. It bounds shutdown latency and nothing else: a notification
# arriving mid-wait wakes the read immediately.
NOTIFY_POLL_SECONDS = 0.5

# A dropped listening connection is the quiet failure this capability exists
# to avoid: the instance goes on serving reads and never hears another write
# again. So it reconnects, backing off to this ceiling so a server that is
# down does not get hammered by every instance at once.
NOTIFY_RECONNECT_MAX_SECONDS = 30.0

# Not published: the first backoff step, how long one connect attempt may
# take, and how long start and stop wait for the listening thread. The
# connect timeout is what makes start's bound mean anything - without it a
# server that accepts TCP and never answers holds the thread indefinitely,
# and start's own timeout then abandons a thread that is still alive. Stop's
# bound has to outlast a refresh already inside the listener, which waits for
# the application's write queue.
_NOTIFY_RECONNECT_MIN_SECONDS = 0.25
_LISTEN_CONNECT_TIMEOUT = 10.0
_LISTEN_START_TIMEOUT = 30.0
_LISTEN_STOP_TIMEOUT = 30.0

# How long a listening connection must survive before the next drop is
# treated as an isolated one rather than a flap. Without this the backoff is
# reset by the mere fact of connecting, which is the one thing a flapping
# server does reliably - and the backoff then never engages on the failure it
# was written for. Measured before it existed: a connection terminated as
# fast as it appeared reconnected 76 times a second, each reconnect costing
# every other instance a whole-graph reload.
_NOTIFY_STABLE_SECONDS = 60.0


# The two kinds an announcement can name, abbreviated because the payload has
# 8000 bytes for the whole batch. Anything else is an announcement this build
# does not understand, and `_pair` raises rather than guessing - a kind read
# as "edge" because it was not "node" would look up an id in the wrong table
# and report content that belongs to something else.
_KINDS = {"n": "node", "e": "edge"}


def _pair(entry: Any) -> Tuple[str, str]:
    """One announced entity as (kind, id), or raise trying.

    Raising is the point: `_handle` turns any failure here into a reload,
    which is the only honest answer to an announcement whose shape this build
    does not recognise.

    The type is checked before the unpacking, not after. A string of two
    characters unpacks as happily as a pair does, so `["ne"]` used to arrive
    as ("node", "e") - a confident delete of an entity nothing announced,
    which is exactly the failure _KINDS above exists to prevent, reached
    through the door beside the one it guards.
    """
    if not isinstance(entry, (list, tuple)) or len(entry) != 2:
        raise TypeError(f"announced entity is not a pair: {entry!r}")
    kind, entity_id = entry
    if not isinstance(entity_id, str):
        raise TypeError(f"announced id is not a string: {entity_id!r}")
    return _KINDS[kind], entity_id


def _channel_for(schema: str) -> str:
    """The notification channel two instances of one graph share.

    Derived from the schema so two graphs in one database do not hear each
    other, and hashed rather than interpolated because a channel name is an
    SQL identifier: it is capped at 63 bytes, where a schema name may itself
    be 63, and an unquoted one is case-folded, so `CoGraph` and `cograph`
    would be the same channel while `LISTEN` and `pg_notify` disagreed about
    which. The digest is lower-case hex, which is fixed-length, well under
    the cap and identical however it is quoted.
    """
    return "co_graph_" + hashlib.sha256(schema.encode("utf-8")).hexdigest()[:32]


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
        # Who wrote it. Every instance on this store listens on one channel
        # and the server delivers a notification to the connection that sent
        # it as readily as to any other - measured, not assumed - so without
        # this an instance would re-apply its own writes and emit a second
        # event for each. Per object rather than per process: two backends in
        # one process are two instances as far as the store is concerned, and
        # the tests rely on exactly that.
        self._origin = uuid.uuid4().hex
        self._channel = _channel_for(schema)
        self._listener: Optional[Callable[[ExternalChange], None]] = None
        self._listen_conn: Optional[psycopg.Connection] = None
        self._listen_thread: Optional[threading.Thread] = None
        self._listen_stop = threading.Event()
        self._listen_lock = threading.Lock()
        self._listen_error: Optional[BaseException] = None
        # Stop is serialised on a lock of its own. Without it a second caller
        # - an application shutting down while a script or a test closes the
        # backend - finds the thread already taken, skips the join, and
        # returns while a listener call is still running, which is exactly
        # the promise stop makes.
        self._stop_lock = threading.Lock()

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
        return BackendCapabilities(
            incremental_writes=True,
            transactions=True,
            change_notification=True,
        )

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
                # A whole-graph save replaced everything, including rows it
                # never named. Naming what changed would mean diffing the
                # store against itself, which is the case unknown() exists
                # for: the other instances reload.
                self._announce(conn, None)

    def default_graph_name(self) -> str:
        return self._graph_name

    # -- entity contract -----------------------------------------------------

    # A deadlock is transient by nature and the batch is atomic, so a retry
    # starts from the state the aborted one left - which is the state it
    # found. Bounded rather than unbounded because a cycle that keeps
    # re-forming is a signal, not something to absorb silently. Under
    # sustained contention from many instances a batch can still exhaust
    # the bound - the server forms cycles of three processes and more, not
    # only pairs - and what happens then is written down at apply_batch.
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
        editing different entities do not wait for each other, because SHARE
        conflicts only with the save's EXCLUSIVE mode. One exception, and it
        is a feature rather than a leak: PostgreSQL makes a new request queue
        behind a conflicting waiter, so once a save is queued for the
        exclusive lock the entity writes behind it wait too. That is what
        stops a stream of them starving the save indefinitely.

        A batch takes one row lock per operation and holds them to commit,
        so two batches touching the same entities in opposite orders
        deadlock. The order is the caller's and cannot be sorted - a delete
        followed by an upsert of one id is not the same batch reordered - so
        the deadlock is retried instead. That is safe precisely because the
        batch is atomic: the aborted transaction left nothing behind. Left
        to propagate on the first cycle it would be worse than one lost
        mutation, because GraphStorage answers a failed entity write by
        re-issuing the whole graph, which is the overwrite this slice exists
        to eliminate. Exhausting the bound reaches that same path: the error
        propagates and the next write is a whole-graph one. That is the
        behaviour this backend had before entity writes existed, so the
        worst case degrades rather than corrupts.
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
                # Inside the transaction, so the announcement is atomic with
                # what it announces: the server holds it until commit and
                # discards it on rollback. A batch that deadlocks and retries
                # therefore announces once, from the attempt that committed,
                # and a batch that fails announces nothing at all.
                self._announce(conn, operations)

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

    # -- change notification -------------------------------------------------

    def _announce(self, conn, operations: Optional[Sequence[EntityOperation]]) -> None:
        """Tell the other instances what this transaction did.

        Issued on the writing connection inside the writing transaction, so
        the server holds the notification until commit and drops it on
        rollback. That is what makes the announcement honest without any
        bookkeeping here: a listener is never told about a change that did
        not happen, and never told twice about one that did - a batch that
        deadlocks and retries announces only from the attempt that committed.

        `operations` is None for a whole-graph save, which replaced rows it
        never named and so cannot describe itself.
        """
        conn.execute(
            "SELECT pg_notify(%s, %s)", (self._channel, self._encode(operations))
        )

    def _encode(self, operations: Optional[Sequence[EntityOperation]]) -> str:
        """The announcement: who wrote, and which entities, in order.

        Identifiers only, never content. A node payload carries its embedding
        inline when there is no vector sidecar, so one node can exceed the
        server's whole payload allowance - the listener reads content back
        from the store instead, which is also what keeps it from acting on a
        stale copy.

        Order is preserved rather than grouped by kind, because it is load
        bearing on the receiving side: GraphStorage drops an external edge
        whose endpoint is not present yet, so a batch that created a node and
        an edge to it would lose the edge if the two were reordered here.

        An announcement that will not fit degrades to the whole-graph form.
        The alternative is worse than a coarse refresh: the server rejects an
        oversized payload with an error, inside the transaction the write is
        in, so a batch of enough entities would fail on its announcement -
        the mutation lost to the act of describing it.
        """
        if operations is None:
            return json.dumps({"o": self._origin})
        payload = json.dumps(
            {
                "o": self._origin,
                "ops": [
                    ["n" if op.kind == "node" else "e", op.entity_id]
                    for op in operations
                ],
            },
            separators=(",", ":"),
        )
        # The encoded length, because bytes are the unit the server's own
        # check uses. It is not currently distinguishable from the character
        # count - json.dumps escapes non-ASCII by default, so this payload is
        # always pure ASCII and one "a-umlaut" is six of both - and no test
        # can make it so. Written this way because it is the right unit if
        # that default is ever changed, which is tempting: the escaping costs
        # six bytes per character, so a graph whose ids are not ASCII reaches
        # this cap six times sooner and announces reloads where it could have
        # named entities.
        if len(payload.encode("utf-8")) >= NOTIFY_PAYLOAD_LIMIT:
            return json.dumps({"o": self._origin})
        return payload

    def start_change_notification(
        self, listener: Callable[[ExternalChange], None]
    ) -> None:
        """Begin reporting other instances' writes, on a thread of our own.

        Returns only once the connection is listening. Returning earlier
        would lose every write made in the gap - and the caller's next act is
        typically to let the application serve traffic, so the gap is exactly
        when the first cross-instance write arrives.

        A failure to establish that first connection is raised rather than
        retried in the background. An instance that boots without listening
        serves what it loaded and never hears another write again; it looks
        healthy and is silently wrong, which is the failure this whole
        capability exists to remove. Once listening has been established, a
        connection that drops later is a different matter and is reconnected
        below.
        """
        with self._listen_lock:
            if self._listen_thread is not None:
                raise RuntimeError(
                    "change notification is already running"
                    if self._listen_thread.is_alive()
                    else "change notification was not stopped cleanly"
                )
            self._ensure_schema()
            self._listener = listener
            self._listen_stop.clear()
            self._listen_error = None
            ready = threading.Event()
            self._listen_thread = threading.Thread(
                target=self._listen_loop,
                args=(ready,),
                name=f"pg-notify-{self._channel[-8:]}",
                daemon=True,
            )
            self._listen_thread.start()
        ready.wait(_LISTEN_START_TIMEOUT)
        if self._listen_error is not None:
            error = self._listen_error
            self.stop_change_notification()
            raise error
        if not ready.is_set():
            self.stop_change_notification()
            raise TimeoutError(
                f"listening on {self._channel} did not start within "
                f"{_LISTEN_START_TIMEOUT}s"
            )

    def stop_change_notification(self) -> None:
        """Stop reporting. The listener is not called again after this returns.

        The thread is joined rather than merely signalled, because the
        promise is about calls in flight as much as calls to come: a refresh
        already inside the listener is running against a model the caller is
        about to tear down. Closing the connection is the fallback for a
        thread that does not notice the flag - the blocking read raises on a
        closed socket.

        The promise holds even when the join does not: the stop flag is set
        before anything else and `_deliver` reads it, so no further call is
        made whatever the thread is doing. What a failed join costs is the
        thread, and the handle to it is then kept rather than cleared - a
        cleared handle would let the next start run a second listener beside
        the first.
        """
        with self._stop_lock:
            # Set before the thread is read, so a thread that wakes during
            # the join below sees it and leaves.
            self._listen_stop.set()
            with self._listen_lock:
                thread = self._listen_thread
            mine = thread is not None and thread is not threading.current_thread()
            if mine:
                thread.join(_LISTEN_STOP_TIMEOUT)
                if thread.is_alive():
                    self._close_listen_conn()
                    thread.join(_LISTEN_STOP_TIMEOUT)
            self._close_listen_conn()
            self._listener = None
            if thread is not None and thread.is_alive():
                # Left in place deliberately, and on the same test whether we
                # joined it or not. The listener will not be called again -
                # the flag is set and _deliver reads it - but the thread is
                # still there, and clearing the handle would let the next
                # start believe nothing is running and start a second one.
                # Measured before this: two listening connections, and every
                # change delivered to the application twice.
                #
                # `mine` decides whether it can be JOINED, not whether it is
                # running. Stop called from inside the listener - a listener
                # that closes its own backend - cannot join itself, and
                # reading that as "nothing is running" cleared the handle on
                # the one thread guaranteed to still be alive.
                if mine:
                    print(
                        f"Warning: the listener thread for {self._channel} "
                        f"did not stop within {_LISTEN_STOP_TIMEOUT}s; "
                        f"notification cannot be started again on this "
                        f"backend"
                    )
                return
            with self._listen_lock:
                if self._listen_thread is thread:
                    self._listen_thread = None

    def _close_listen_conn(self) -> None:
        with self._listen_lock:
            conn, self._listen_conn = self._listen_conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass  # closing a connection that is already gone

    def _listen_loop(self, ready: threading.Event) -> None:
        delay = _NOTIFY_RECONNECT_MIN_SECONDS
        while not self._listen_stop.is_set():
            conn = None
            try:
                conn = psycopg.connect(
                    self.conninfo,
                    autocommit=True,
                    connect_timeout=_LISTEN_CONNECT_TIMEOUT,
                )
                conn.execute(sql.SQL("LISTEN {}").format(sql.Identifier(self._channel)))
            except Exception as exc:
                # Closed here rather than left to the interpreter: LISTEN
                # can fail on a connection that opened, and a connection is
                # the resource this backend is most careful with. Measured,
                # so the reason is stated no larger than it is - CPython's
                # refcounting does close the dropped connection promptly, so
                # this is about not depending on that, not about a leak that
                # would exhaust the server.
                if conn is not None:
                    conn.close()
                if not ready.is_set():
                    self._listen_error = exc
                    ready.set()
                    return
                print(
                    f"Warning: reconnecting to {self._channel} after "
                    f"{type(exc).__name__}: {exc}"
                )
                self._listen_stop.wait(delay)
                delay = min(delay * 2, NOTIFY_RECONNECT_MAX_SECONDS)
                continue

            with self._listen_lock:
                self._listen_conn = conn
            reconnected = ready.is_set()
            ready.set()
            if reconnected:
                # Whatever was written while this instance was not listening
                # was announced to a connection that no longer existed. The
                # notifications are gone; the writes are in the store. A
                # reload is the only honest answer - and the reason the
                # backoff below is not optional, because this is the cost
                # every reconnect puts on this instance.
                self._deliver(ExternalChange.unknown())

            listening_since = time.monotonic()
            try:
                self._read_until_stopped(conn)
            except Exception as exc:
                if not self._listen_stop.is_set():
                    print(
                        f"Warning: lost the listening connection on "
                        f"{self._channel}: {type(exc).__name__}: {exc}"
                    )
            finally:
                self._close_listen_conn()

            if self._listen_stop.is_set():
                return
            # The connection dropped. This waits too, and it is the path that
            # needed it most: a connection that establishes and dies is what a
            # flapping server, a failover loop or an idle reaper produces, and
            # resetting the backoff on the strength of having connected made
            # the whole ceiling unreachable on exactly that failure. Reset
            # only on a connection that actually stayed up.
            if time.monotonic() - listening_since >= _NOTIFY_STABLE_SECONDS:
                delay = _NOTIFY_RECONNECT_MIN_SECONDS
            self._listen_stop.wait(delay)
            delay = min(delay * 2, NOTIFY_RECONNECT_MAX_SECONDS)

    def _read_until_stopped(self, conn) -> None:
        """Block on the connection, waking often enough to see the stop flag.

        The timeout bounds shutdown latency only. A notification arriving
        mid-wait wakes the read at once, and notifications the generator did
        not yield before it timed out stay buffered on the connection for the
        next call - measured, because losing them here would be a change
        reported to nobody.
        """
        while not self._listen_stop.is_set():
            for note in conn.notifies(timeout=NOTIFY_POLL_SECONDS):
                self._handle(note.payload)
                if self._listen_stop.is_set():
                    return

    def _handle(self, payload: str) -> None:
        """Read one announcement, or decide it cannot be read.

        Every way of not understanding it ends in the same place: a reload. A
        store is shared with instances that may be running a version this one
        predates - that is what a rolling deploy is - and treating an
        announcement from one of them as noise would leave this instance
        stale for exactly as long as that instance keeps writing. There is no
        catch-up; there is only the next announcement, which this build would
        not understand either.

        Which is why the shape is checked *here*, before any of it is used.
        Letting a malformed entry reach the read-back instead would raise out
        of the reading thread, and the reload would arrive only as a side
        effect of the reconnect that followed: the right outcome by the wrong
        road. It costs the listening connection, it costs the backoff wait
        that a drop now takes, and it is reported as a lost connection naming
        a KeyError - a misdiagnosis in the one line an operator would read.
        """
        try:
            announcement = json.loads(payload)
            origin = announcement["o"]
            named = announcement.get("ops")
            pairs = None if named is None else [_pair(entry) for entry in named]
        except Exception:
            self._deliver(ExternalChange.unknown())
            return
        if origin == self._origin:
            return  # our own write; the application already has it
        if pairs is None:
            self._deliver(ExternalChange.unknown())
            return
        try:
            change = self._resolve(pairs)
        except Exception as exc:
            # The announcement was read; the store would not answer for it -
            # a pool timeout under contention, a connection dropped between
            # the two. Left to propagate it would leave the reading thread,
            # take the listening connection with it and cost a reconnect, so
            # a transient read is amplified into the most expensive recovery
            # this backend has. The change is real either way, so this
            # instance says what it honestly knows: something changed.
            print(
                f"Warning: could not read back an announced change on "
                f"{self._channel}: {type(exc).__name__}: {exc}"
            )
            self._deliver(ExternalChange.unknown())
            return
        self._deliver(change)

    def _resolve(self, pairs: Sequence[Tuple[str, str]]) -> ExternalChange:
        """Turn named identifiers into operations, reading content from the store.

        The store decides what happened, not the announcement. An identifier
        that is there now is an upsert carrying what is there now; one that is
        not is a delete. That is deliberate and stronger than trusting the
        announced action: between the commit and this read the entity may have
        been written again or removed by a third instance, and reporting the
        announced action would then apply something the store contradicts.
        Reading instead can only report content newer than the announcement,
        never older, and the announcement for that newer write follows and
        agrees.
        """
        found: Dict[str, Dict[str, Any]] = {"node": {}, "edge": {}}
        with self._pool.connection() as conn:
            for kind, table in (("node", "graph_nodes"), ("edge", "graph_edges")):
                ids = [entity_id for k, entity_id in pairs if k == kind]
                if not ids:
                    continue
                rows = conn.execute(
                    sql.SQL("SELECT id, doc FROM {} WHERE id = ANY(%s)").format(
                        self._table(table)
                    ),
                    (ids,),
                ).fetchall()
                found[kind] = {row[0]: row[1] for row in rows}

        operations: List[EntityOperation] = []
        for kind, entity_id in pairs:
            doc = found[kind].get(entity_id)
            if doc is None:
                operations.append(
                    EntityOperation(kind, "delete", entity_id)  # type: ignore[arg-type]
                )
            else:
                operations.append(
                    EntityOperation(kind, "upsert", entity_id, dict(doc))  # type: ignore[arg-type]
                )
        return ExternalChange.entities(operations)

    def _deliver(self, change: ExternalChange) -> None:
        """Hand one change to the listener, holding nothing while it runs.

        Nothing of ours is held here on purpose. The listener refreshes an
        application that may wait for its own write queue, and that queue's
        writes need this pool - so a pooled connection still held from the
        read-back above would have the refresh waiting for a writer that is
        waiting for the connection the refresh has. `_resolve` returns its
        connection before this is called, and this is why.
        """
        listener = self._listener
        if listener is None or self._listen_stop.is_set():
            return
        try:
            listener(change)
        except ExternalChangeRefused as exc:
            # Reported from the application's write thread, which this thread
            # is not. Either the application called us back into itself, or
            # this backend grew a path that reports inline. Loud, because the
            # refresh did not happen and the instance is now behind.
            print(f"Warning: change notification refused: {exc}")
        except Exception as exc:
            print(
                f"Warning: applying an external change failed: "
                f"{type(exc).__name__}: {exc}"
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
        """Release this instance's server connections.

        Not part of the persistence contract - no backend is asked to close -
        but a pool holds server connections, and the server's ceiling is the
        scarce resource here. A caller that creates backends repeatedly
        (tests, a script) should close them; a long-lived application
        instance holds one for its lifetime by design.

        Notification is stopped first, and stopped here at all because the
        listening connection and its thread outlive the pool otherwise: an
        application shuts them down through stop_change_notification, but a
        script or a test that only closes the backend would leave a thread
        reading a connection whose pool is gone.
        """
        self.stop_change_notification()
        self._pool.close()


__all__ = [
    "PostgresGraphPersistenceBackend",
    "SAVE_LOCK_KEY",
    "DEFAULT_POOL_SIZE",
    "MIGRATION_LOCK_KEY",
    "NOTIFY_PAYLOAD_LIMIT",
    "NOTIFY_POLL_SECONDS",
    "NOTIFY_RECONNECT_MAX_SECONDS",
]
