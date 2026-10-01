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

`store_traversal` is the fourth, and the one that reaches into reads rather
than writes: the backend answers a bounded-depth neighbourhood query itself,
a level at a time, instead of the application walking the copy of the
topology it holds in memory. The in-memory walk stays the reference - the
answers are held identical by a differential test - and answers whenever the
store is not current or cannot be reached.

One optional seam runs across all of that. Each entity row can carry an
OPAQUE SCOPE IDENTIFIER, and the tables carry a row-level security policy
binding on it, so a deployment that keeps more than one graph's rows behind
one connection can have the server - not only this code - refuse the rows
that are not its own. It is off unless a host supplies a value: the column
is then NULL on every row, which the policy admits, and the store behaves
exactly as it did before the column existed. What the value MEANS is the
host's business and never this module's; here it is a string to store,
compare and pass through.

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
import logging
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

logger = logging.getLogger(__name__)

# Every instance runs the same migration on boot, so they race. The lock is
# taken for the duration of the migrating transaction and released with it -
# no unlock to leak on an exception. The key is an arbitrary fixed constant,
# not derived from anything; it only has to be one this application uses
# nowhere else.
MIGRATION_LOCK_KEY = 4_872_015_733_882_119_001

# The optional scope seam, in three names. The column holds the host's
# opaque identifier, the setting carries the current session's value to the
# server, and the policy is what the server refuses with. Deliberately
# generic: this module stores and compares the value and never asks what it
# distinguishes, which is the property that keeps the seam usable by any host
# and this backend free of anyone's domain model.
#
# `text`, not a narrower type, for the same reason. A host that uses a UUID
# loses nothing by storing it as text - the comparison is equality either way
# - while a `uuid` column would refuse every host whose identifiers are not
# UUIDs, and a malformed session value would fail the cast rather than match
# nothing.
SCOPE_COLUMN = "scope_id"
SCOPE_SETTING = "app.graph_scope"
SCOPE_POLICY_SUFFIX = "_scope_policy"

# The two tables the seam covers. `graph_metadata` is deliberately not one of
# them: its primary key is a column that can only hold true, so it holds one
# row for the whole store and has nowhere to put a second scope's row. A
# deployment separating scopes therefore separates their metadata the way the
# backend already separates whole graphs - a schema each - and this column is
# the row-level layer underneath that, not a replacement for it.
SCOPED_TABLES = ("graph_nodes", "graph_edges")

# Stored inside graph_metadata.doc, and stripped back out on load. The visible
# metadata keeps its file-backend shape, while the PostgreSQL schema still has
# a single durable claim tying it to the graph identity that opened it.
GRAPH_IDENTITY_KEY = "_postgres_graph_identity"

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
# fast as it appeared reconnected 76 times a second, and every one of those
# reconnects reports unknown(), so this instance reloads the whole graph 76
# times a second. The cost falls on the instance that is already struggling,
# not on the others - a reconnect announces nothing and their listening
# connections are untouched.
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


class ScopeIsolationUnavailable(RuntimeError):
    """A scope was configured and the store cannot keep rows apart by it.

    Raised at boot rather than absorbed, because the alternative is worse
    than not starting: a backend that was handed a scope and has no column to
    put it in writes rows that carry no scope at all, and every other session
    on that store can read them. A store that cannot be provisioned is an
    operator's problem with a remedy; rows written unscoped are already
    everyone's.
    """


class CrossScopeWriteRefused(RuntimeError):
    """An entity write named an id that belongs to another scope.

    `id` is the primary key of the table rather than of the table per scope,
    so two scopes cannot both hold one id and a write that names another
    scope's is not a write this instance may make. Raised rather than applied,
    and rather than quietly skipped: applying it would destroy the other
    scope's row - measured, with the row's content replaced and its scope
    restamped - and skipping it would tell the caller a write happened that
    did not. Where the policy is in force the server refuses the same write,
    so this is the application layer agreeing with it rather than a second
    rule.
    """


class GraphIdentityCollision(RuntimeError):
    """A PostgreSQL schema is already claimed by another graph identity."""


class PostgresGraphPersistenceBackend:
    """The graph in PostgreSQL: nodes, edges and metadata as JSONB rows.

    JSONB rather than columns per field, because the graph's schema is
    configuration (`config/*/schema_config.json`), not something this table
    should have an opinion about. It is the same payload the file backend
    writes, so the two stores hold the same shape.

    A `schema` other than the default keeps one database serving several
    independent graphs, which is also how the tests give each case a store of
    its own without a database each.

    A `scope` tags every row this instance writes with the host's opaque
    identifier and narrows every row it reads to that value or to no value at
    all. Leave it None - the default, and what every deployment before this
    argument existed runs - and nothing about the store changes.
    """

    def __init__(
        self,
        conninfo: str,
        *,
        schema: str = "public",
        pool_size: int = DEFAULT_POOL_SIZE,
        graph_name: str = "graph",
        scope: Optional[str] = None,
    ):
        if pool_size < 1:
            raise ValueError("pool_size must be at least 1")
        # An empty scope is not an unscoped one. A host that templated the
        # value out of its environment sends "" where it meant to send an
        # identifier, and reading that as "no isolation wanted" is how a
        # deployment that asked to be separated silently is not.
        if scope is not None and not scope.strip():
            raise ValueError(
                "scope must be a non-empty identifier, or None for a store "
                "that keeps no scopes apart"
            )
        self.conninfo = conninfo
        self.schema = schema
        self._graph_name = graph_name
        self._scope = scope
        self._graph_identity_checked = False
        # Settled by the migration, before any statement that would name the
        # column is built. False means the store has no scope column - an
        # older store, or one an operator provisioned without it - and every
        # statement below is then the one this backend issued before the
        # column existed.
        self._scope_column = False
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
            # Outside the migration transaction, on purpose, and one
            # connection each. Every level of the traversal filters on
            # doc->>'source' and doc->>'target', which no index covers by
            # default. Measured at depth 3 on 20k nodes: 3.7 ms against
            # 61 ms unindexed at 60k edges, and 89 ms against 258 ms at 300k.
            #
            # Best-effort, and deliberately not fatal: the same least-privilege
            # role the guards above exist for may hold DML and no DDL, and a
            # store that cannot take an index should still boot and still
            # answer - slowly, which is a performance problem, where failing
            # here is an outage. It has to be its own transaction for that to
            # be true at all: a failed statement inside the migration's
            # transaction aborts the whole thing, so catching the error there
            # would have recovered nothing.
            #
            # The catalog is asked first, for the same reason `_create_missing`
            # asks it: `CREATE INDEX IF NOT EXISTS` checks ownership BEFORE it
            # checks existence, so the role provisioned exactly as
            # docs/PERSISTENCE_BACKENDS.md prescribes - indexes and all - got
            # "must be owner of table graph_edges" on every boot, and a warning
            # saying its traversals would scan when they were seeking. A
            # warning that fires when nothing is wrong is worse than none.
            for name, expression in (
                ("graph_edges_source_idx", "((doc->>'source'))"),
                ("graph_edges_target_idx", "((doc->>'target'))"),
            ):
                try:
                    with self._pool.connection() as conn:
                        state = self._index_state(conn, name)
                        if state == "valid":
                            continue
                        if state == "invalid":
                            # indisvalid = false. The planner will not use it,
                            # and `IF NOT EXISTS` matches on the name, so
                            # re-issuing it here is a silent no-op: without
                            # this branch the store scans every edge at every
                            # level and says nothing.
                            #
                            # The cause is deliberately not named. A concurrent
                            # build that failed looks exactly like one that is
                            # still running - verified: indisvalid reads false
                            # throughout a healthy CREATE INDEX CONCURRENTLY -
                            # and the docs tell an operator to use that on a
                            # live store. Telling them their own build had
                            # failed is how they abort it and take an
                            # ACCESS EXCLUSIVE lock they were avoiding.
                            #
                            # Reported rather than repaired because
                            # REINDEX ... CONCURRENTLY cannot run inside a
                            # transaction block and these connections are not
                            # autocommit - not because the index must be
                            # dropped. It need not: REINDEX repairs it in
                            # place, and even removing it has a concurrent
                            # form.
                            # Quoted, because the operator is meant to paste
                            # it. `self.schema` reaches an identifier position
                            # nowhere else in this file without going through
                            # sql.Identifier, and a mixed-case schema is a
                            # supported shape the contract parametrises over:
                            # unquoted, the remedy fails with `schema
                            # "colow_demo" does not exist`.
                            qualified = sql.Identifier(self.schema, name).as_string(
                                conn
                            )
                            logger.warning(
                                f"{name} exists but is not valid - a "
                                f"concurrent build that failed, or one still "
                                f"running. If no build is in progress, "
                                f"REINDEX INDEX CONCURRENTLY {qualified} "
                                f"repairs it; until it is valid the traversal "
                                f"will scan instead of seek"
                            )
                            continue
                        conn.execute(
                            sql.SQL("CREATE INDEX IF NOT EXISTS {} ON {} {}").format(
                                sql.Identifier(name),
                                self._table("graph_edges"),
                                sql.SQL(expression),
                            )
                        )
                except Exception as exc:
                    # Ask again before reporting. This loop runs outside the
                    # migrating transaction, and the advisory lock is
                    # transaction-scoped, so it was released before we got
                    # here: N instances booting together all pass the check
                    # above and all issue the statement, and the losers get a
                    # duplicate-key error from the catalog insert. Measured:
                    # 8 concurrent `CREATE INDEX IF NOT EXISTS` of one name,
                    # 4 of them raised UniqueViolation and the index exists.
                    # The window is the index build, so it widens with the
                    # table - it is the upgrade of a large existing store
                    # that hits this, not a toy one. The migration's own
                    # docstring names the same mechanism for tables, where
                    # the lock closes it.
                    #
                    # Nothing is lost when that happens: the index is there,
                    # put there by whoever won. Reporting it would be the
                    # defect this pre-check was added to fix - a warning that
                    # fires when nothing is wrong - so the warning is for the
                    # case where the index really is missing.
                    if self._index_state(None, name) != "valid":
                        logger.warning(
                            f"could not create {name}; traversal will "
                            f"scan instead of seek: {exc}"
                        )
            # Last, and outside the migrating transaction for the same reason
            # the indexes are: it may legitimately fail, and a failure inside
            # that transaction would take the tables down with it.
            self._ensure_scope_isolation()
            with self._pool.connection() as conn:
                with conn.transaction():
                    self._claim_or_check_graph_identity(conn)
            self._migrated = True

    def _metadata_without_graph_identity(
        self, metadata: Dict[str, Any]
    ) -> Dict[str, Any]:
        metadata = dict(metadata)
        metadata.pop(GRAPH_IDENTITY_KEY, None)
        return metadata

    def _claim_or_check_graph_identity(self, conn, *, force: bool = False) -> None:
        if self._graph_identity_checked and not force:
            return
        row = conn.execute(
            sql.SQL("SELECT doc FROM {} LIMIT 1").format(self._table("graph_metadata"))
        ).fetchone()
        if row is None:
            return
        metadata = dict(row[0] or {})
        claimed = metadata.get(GRAPH_IDENTITY_KEY)
        if claimed is None:
            # Conditional, because the read above took no lock: another
            # instance may have claimed since. Under READ COMMITTED an UPDATE
            # that waited on its row lock re-checks the WHERE against the
            # committed row, so the loser matches nothing and reads the
            # winner's claim below rather than overwriting it. Under
            # REPEATABLE READ it fails on serialization instead, which
            # overwrites nothing either.
            claimed = conn.execute(
                sql.SQL(
                    "UPDATE {} SET doc = doc || jsonb_build_object(%s::text, %s::text)"
                    " WHERE doc ->> %s IS NULL RETURNING doc ->> %s"
                ).format(self._table("graph_metadata")),
                (
                    GRAPH_IDENTITY_KEY,
                    self._graph_name,
                    GRAPH_IDENTITY_KEY,
                    GRAPH_IDENTITY_KEY,
                ),
            ).fetchone()
            if claimed is None:
                claimed = conn.execute(
                    sql.SQL("SELECT doc ->> %s FROM {} LIMIT 1").format(
                        self._table("graph_metadata")
                    ),
                    (GRAPH_IDENTITY_KEY,),
                ).fetchone()
                if claimed is None:
                    return
            claimed = claimed[0]
        if claimed != self._graph_name:
            raise GraphIdentityCollision(
                f"PostgreSQL schema {self.schema!r} is already claimed by "
                f"graph {claimed!r}, not {self._graph_name!r}"
            )
        self._graph_identity_checked = True

    _INDEX_STATE = (
        "SELECT i.indisvalid FROM pg_class c"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " LEFT JOIN pg_index i ON i.indexrelid = c.oid"
        " WHERE n.nspname = %s AND c.relname = %s"
    )

    def _index_state(self, conn, name: str) -> str:
        """ "valid", "invalid" or "missing" for `name` in this schema.

        Presence by name is not enough: an index left invalid by a failed
        concurrent build is present, unusable by the planner, and matched by
        `IF NOT EXISTS` - so treating it as there is how a store ends up
        scanning silently.

        `conn` may be None, which takes a fresh one from the pool. That is
        what the failure path uses - not because the failed connection is
        unusable (the pool's context manager rolls it back as the exception
        propagates, before returning it), but because by then it has gone back
        to the pool and another thread may hold it. Asking through the pool is
        the only way to be sure what is being asked.
        """
        try:
            if conn is not None:
                row = conn.execute(self._INDEX_STATE, (self.schema, name)).fetchone()
            else:
                with self._pool.connection() as fresh:
                    row = fresh.execute(
                        self._INDEX_STATE, (self.schema, name)
                    ).fetchone()
        except Exception:
            # Cannot tell. Say so, so the caller reports rather than hides.
            return "missing"
        if row is None:
            return "missing"
        return "valid" if row[0] else "invalid"

    # -- the scope seam ------------------------------------------------------

    _COLUMN_PRESENT = (
        "SELECT 1 FROM pg_attribute a"
        " JOIN pg_class c ON c.oid = a.attrelid"
        " JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = %s AND c.relname = %s AND a.attname = %s"
        " AND a.attnum > 0 AND NOT a.attisdropped"
    )

    _POLICY_STATE = (
        "SELECT c.relrowsecurity, c.relforcerowsecurity,"
        " EXISTS (SELECT 1 FROM pg_policy p"
        "         WHERE p.polrelid = c.oid AND p.polname = %s)"
        " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
        " WHERE n.nspname = %s AND c.relname = %s"
    )

    def _policy_name(self, table: str) -> str:
        return f"{table}{SCOPE_POLICY_SUFFIX}"

    def _column_present(self, conn, table: str) -> bool:
        return (
            conn.execute(
                self._COLUMN_PRESENT, (self.schema, table, SCOPE_COLUMN)
            ).fetchone()
            is not None
        )

    def _policy_state(self, conn, table: str) -> Tuple[bool, bool, bool]:
        """(rls enabled, rls forced, policy present) for `table` in this schema."""
        row = conn.execute(
            self._POLICY_STATE, (self._policy_name(table), self.schema, table)
        ).fetchone()
        if row is None:
            return (False, False, False)
        return (bool(row[0]), bool(row[1]), bool(row[2]))

    def _try_provision_scope_isolation(self, table: str) -> None:
        with self._pool.connection() as conn:
            with conn.transaction():
                if not self._column_present(conn, table):
                    conn.execute(
                        sql.SQL(
                            "ALTER TABLE {} ADD COLUMN IF NOT EXISTS {} text"
                        ).format(self._table(table), sql.Identifier(SCOPE_COLUMN))
                    )
                enabled, forced, policy = self._policy_state(conn, table)
                if self._scope is None:
                    # The column, and nothing that would cost an unscoped
                    # store its traversal plan.
                    return
                if not policy:
                    # `FOR ALL`, so the same expression governs what a
                    # session may read and what it may write: without a
                    # WITH CHECK the server would refuse to hand a row
                    # over and accept one written in its place.
                    #
                    # The NULL arm is what makes the seam optional. A
                    # row that carries no scope is admitted whatever
                    # the session is set to, which is every row of
                    # every store that existed before this column did.
                    # The other arm is unsatisfiable when the setting
                    # is unset - `= NULL` is NULL, not true - so a
                    # scoped row is invisible to a session that did not
                    # say which scope it is, which is the direction
                    # that has to fail closed.
                    conn.execute(
                        sql.SQL(
                            "CREATE POLICY {} ON {} FOR ALL"
                            " USING ({col} IS NULL"
                            "        OR {col} = current_setting({setting}, true))"
                        ).format(
                            sql.Identifier(self._policy_name(table)),
                            self._table(table),
                            col=sql.Identifier(SCOPE_COLUMN),
                            setting=sql.Literal(SCOPE_SETTING),
                        )
                    )
                if not enabled:
                    conn.execute(
                        sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(
                            self._table(table)
                        )
                    )
                if not forced:
                    # Without FORCE the policy is decoration in the
                    # commonest deployment there is: row-level security
                    # does not apply to a table's OWNER, and the owner
                    # is whoever ran the migration - this application.
                    conn.execute(
                        sql.SQL("ALTER TABLE {} FORCE ROW LEVEL SECURITY").format(
                            self._table(table)
                        )
                    )

    def _scope_isolation_state(self, table: str) -> Tuple[bool, bool]:
        """(column missing, server policy incomplete) after provisioning tried."""
        with self._pool.connection() as conn:
            if not self._column_present(conn, table):
                return (True, False)
            if self._scope is None:
                return (False, False)
            enabled, forced, policy = self._policy_state(conn, table)
            return (False, not (enabled and forced and policy))

    def _ensure_scope_isolation(self) -> None:
        """Provision the scope column and its policy, or establish we cannot.

        Best-effort and outside the migrating transaction, like the traversal's
        indexes and for the same reason: every statement here needs OWNERSHIP,
        and the least-privilege role the migration's guards exist for holds DML
        and no DDL. A store that cannot take the column still boots and still
        answers - as exactly the store it was before this seam existed, since
        no statement this backend issues names a column it has established is
        not there.

        One case does not degrade, and it is the whole point of the seam: a
        scope was CONFIGURED and the column is missing. Writing rows that carry
        no scope would hand them to every other session on that store, so this
        raises instead - see ScopeIsolationUnavailable.

        Order inside the transaction is load-bearing. The policy is created
        BEFORE row-level security is switched on, because a table with RLS
        enabled and no policy admits nothing at all: reversing the two and
        failing in between would leave a store whose every row had vanished.
        One transaction per table makes that unreachable rather than unlikely -
        a failure rolls the whole step back and the catalog is re-read.

        THE POLICY IS CREATED ONLY FOR A STORE THAT ASKED FOR ONE, and that is
        a measurement rather than a preference. A policy applies to a query as
        a security qual, and PostgreSQL will not let a qual that is not
        leakproof be evaluated before one - so the traversal's index condition,
        `doc->>'source' = ...`, stops being usable as one the moment a policy
        applies to the reading role. `jsonb_object_field_text` is not marked
        leakproof (`pg_proc.proleakproof` is false; `texteq`, which the primary
        key uses, is true), so the expression indexes go unused and every level
        of every walk reads the edge table instead - the 3.7 ms against 61 ms
        this file measures elsewhere, in the wrong direction. Charging that to
        a store that keeps no scopes apart would be charging it for a guarantee
        it did not ask for and does not get; charging it to one that did is the
        price of the second layer, and it is written down in
        docs/PERSISTENCE_BACKENDS.md rather than discovered.

        The COLUMN lands either way. It is inert without a policy - a nullable
        text column nothing reads unless this instance names it - and having it
        already there is what lets a host turn the seam on later without
        rewriting a table full of rows.
        """
        # No isolation level is pinned here, where the save and the entity
        # write both state theirs. Nothing in this step depends on one: the
        # catalog re-read runs after every attempt, in a transaction of its
        # own, so it sees what the DDL transaction left whatever level the
        # role defaults to - confirmed against a role defaulting to REPEATABLE
        # READ.
        missing_column: List[str] = []
        unprotected: List[str] = []
        for table in SCOPED_TABLES:
            try:
                self._try_provision_scope_isolation(table)
            except Exception:
                # Swallowed on purpose: what matters is what the store ended up
                # with, which the catalog answers below, not which statement
                # could not be issued.
                pass
            column_missing, policy_incomplete = self._scope_isolation_state(table)
            if column_missing:
                missing_column.append(table)
            if policy_incomplete:
                unprotected.append(table)

        self._scope_column = not missing_column
        if self._scope is not None and missing_column:
            raise ScopeIsolationUnavailable(
                f"a scope was configured, and {', '.join(missing_column)} in "
                f"schema {self.schema!r} has no {SCOPE_COLUMN} column to put it "
                f"in. Rows written now would carry no scope and be readable by "
                f"every other session on this store. Add the column and its "
                f"policy - docs/PERSISTENCE_BACKENDS.md has the statements - or "
                f"run this instance without a scope."
            )
        if self._scope is not None and unprotected:
            # Only when a scope is configured: a store that keeps no scopes
            # apart is not missing anything by not having the policy, and a
            # warning that fires when nothing is wrong teaches an operator to
            # ignore warnings.
            logger.warning(
                f"{', '.join(unprotected)} in schema {self.schema!r} "
                f"carries the {SCOPE_COLUMN} column but not its row-level "
                f"security policy, so the scope is enforced by this "
                f"application alone and not by the server"
            )

    def _scope_clause(self) -> Tuple[sql.Composed, Tuple]:
        """The visibility predicate and its parameter, or nothing at all.

        The same expression the policy carries, issued by the application too.
        Two layers rather than one, and deliberately not a duplicate that could
        drift: where the server cannot enforce the policy - a role that owns
        nothing, a store an operator provisioned without it - this is what
        still holds, and where it can, the two agree so a row is never visible
        to one and not the other.

        An unscoped instance passes NULL, which reduces the predicate to
        `scope_id IS NULL` - the same set the policy shows a session that never
        said which scope it is.
        """
        if not self._scope_column:
            return sql.SQL(""), ()
        return (
            sql.SQL("({col} IS NULL OR {col} = %s)").format(
                col=sql.Identifier(SCOPE_COLUMN)
            ),
            (self._scope,),
        )

    def _where_scope(self) -> Tuple[sql.Composed, Tuple]:
        """` WHERE <predicate>`, or nothing when the store has no column."""
        clause, params = self._scope_clause()
        if not params:
            return sql.SQL(""), ()
        return sql.SQL(" WHERE ") + clause, params

    def _and_scope(self) -> Tuple[sql.Composed, Tuple]:
        """` AND <predicate>`, for a statement that already has a WHERE."""
        clause, params = self._scope_clause()
        if not params:
            return sql.SQL(""), ()
        return sql.SQL(" AND ") + clause, params

    def _insert_shape(self) -> Tuple[sql.Composed, sql.Composed]:
        """The column list and the placeholders an insert carries.

        Both halves from one place, so a statement cannot name three columns
        and pass two values.
        """
        if not self._scope_column:
            return sql.SQL("id, doc"), sql.SQL("%s, %s")
        return (
            sql.SQL("id, doc, ") + sql.Identifier(SCOPE_COLUMN),
            sql.SQL("%s, %s, %s"),
        )

    def _insert_params(self, entity_id: str, payload: Dict[str, Any]) -> Tuple:
        row = (entity_id, psycopg.types.json.Jsonb(payload))
        return row + (self._scope,) if self._scope_column else row

    def _bind_scope(self, conn) -> None:
        """Carry this instance's scope into the transaction, for the policy.

        `set_config(..., is_local => true)` rather than `SET LOCAL`: the value
        is a parameter, so a host's identifier reaches the server as data and
        not as SQL text, and transaction-local means the connection goes back
        to the pool carrying nothing - the next transaction on it starts unset,
        which is the safe direction to be wrong in.

        Nothing is issued when no scope is configured. The setting is then
        never set, `current_setting(..., true)` answers NULL, and the policy
        admits exactly the rows that carry no scope.
        """
        if self._scope is None:
            return
        conn.execute("SELECT set_config(%s, %s, true)", (SCOPE_SETTING, self._scope))

    # -- snapshot contract ---------------------------------------------------

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            incremental_writes=True,
            transactions=True,
            change_notification=True,
            store_traversal=True,
        )

    # -- traversal contract --------------------------------------------------

    # One level of the walk, not the whole traversal. The recursion this
    # replaced was a single depth-limited `WITH RECURSIVE`, which is the
    # obvious way to write it and cannot be made to stop early: its working
    # table is keyed on (id, depth), so a level that reaches nothing new still
    # emits rows at a depth never seen before, and PostgreSQL has no way to
    # prune ids already reached - a recursive term may not reference its own
    # accumulated result in a subquery. So it ran the caller's number of
    # levels whatever the graph looked like. Measured on 500 nodes / 5000
    # edges, where the answer is complete at depth 5 (0.06 s): depth 64 cost
    # 1.05 s, depth 200 cost 3.3 s and depth 1000 cost 16.3 s, all returning
    # the identical set. Clamping the depth to the graph's size does not fix
    # that - the clamp only binds when the graph has FEWER edges than the
    # requested depth, which is never the case on a store worth putting behind
    # this - and `mcp_tools.get_related_nodes` takes a depth with no cap at
    # all.
    #
    # Driving the levels from here converges instead: the loop stops the
    # moment a level reaches nothing new, which is what the in-memory walk
    # does and therefore what the equivalence contract already describes. It
    # costs one round trip per level of the graph actually crossed, rather
    # than one recursion step per level the caller asked for.
    # No DISTINCT: the LATERAL emits one row per edge and `graph_nodes.id` is
    # the primary key, so the LEFT JOIN matches at most once. There is nothing
    # for it to remove, and asking for it buys a unique step per level.
    _LEVEL = """
    SELECT e.id AS edge_id, far.id AS far_id, (fn.id IS NOT NULL) AS is_node
    FROM {edges} e
    CROSS JOIN LATERAL (
      SELECT CASE WHEN e.doc->>'source' = ANY(%(frontier)s)
                  THEN e.doc->>'target' ELSE e.doc->>'source' END AS id
    ) far
    LEFT JOIN {nodes} fn ON fn.id = far.id {node_scope}
    WHERE (e.doc->>'source' = ANY(%(frontier)s)
           OR e.doc->>'target' = ANY(%(frontier)s))
      AND (%(archived_ok)s OR NOT COALESCE((e.doc->>'archived')::boolean, false))
      AND (%(any_type)s OR e.doc->>'type' = ANY(%(types)s))
      AND (%(archived_ok)s OR far.id = %(anchor)s OR fn.id IS NULL
           OR NOT COALESCE((fn.doc->>'archived')::boolean, false))
      {edge_scope}
    """

    # The node half of the predicate goes in the JOIN condition, not the WHERE.
    # A node this instance may not see has to read as an endpoint that is not a
    # node - traversed THROUGH, not returned, which is the dangling-endpoint
    # rule the walk already has - and a WHERE clause naming `fn` would instead
    # turn the outer join inner and drop the edge itself.
    _LEVEL_NODE_SCOPE = "AND (fn.{col} IS NULL OR fn.{col} = %(scope)s)"
    _LEVEL_EDGE_SCOPE = "AND (e.{col} IS NULL OR e.{col} = %(scope)s)"

    def traverse(
        self,
        anchor_id: str,
        depth: int,
        relationship_types: Optional[Sequence[str]] = None,
        include_archived: bool = False,
    ) -> Dict[str, List[str]]:
        """Bounded-depth traversal in the store. See `TraversingBackend`.

        A level at a time, from here, exactly as the in-memory walk does it:
        the ids a level reaches become the next level's frontier, and the loop
        ends when a level reaches nothing new. That is what bounds the cost by
        the graph rather than by the caller's `depth` - see `_LEVEL` for what
        the single-query version cost instead.

        The edge set is everything incident to a node that was EXPANDED, which
        is strictly smaller than everything incident to a node that was
        reached: a node exactly `depth` away is reached but never expanded, so
        an edge between two such nodes belongs to neither endpoint's expansion
        and is not returned. Collecting each level's edges from the frontier
        being expanded gives that for free; the single-query version needed a
        second pass over the recursion to get it.

        Ids that are not nodes are traversed THROUGH but not returned - the
        dangling-endpoint rule - so a path can be longer than the graph has
        nodes, and the frontier carries them while `node_ids` does not.
        """
        self._ensure_schema()
        depth = max(0, depth)
        # `.value`, never `str()`. RelationshipType is a str-Enum, and str()
        # of one is "RelationshipType.RELATES_TO" while the stored document
        # holds "RELATES_TO" - so a filter built with str() silently matches
        # nothing and the traversal returns the anchor alone. The protocol asks
        # for strings; this coerces an enum that arrives anyway, correctly.
        types = [getattr(t, "value", t) for t in (relationship_types or [])]
        scoped = {
            "node_scope": self._LEVEL_NODE_SCOPE,
            "edge_scope": self._LEVEL_EDGE_SCOPE,
        }
        query = sql.SQL(self._LEVEL).format(
            edges=self._table("graph_edges"),
            nodes=self._table("graph_nodes"),
            **{
                name: (
                    sql.SQL(text).format(col=sql.Identifier(SCOPE_COLUMN))
                    if self._scope_column
                    else sql.SQL("")
                )
                for name, text in scoped.items()
            },
        )
        anchor_scope, anchor_params = self._and_scope()
        with self._pool.connection() as conn, conn.transaction():
            # One moment, like the load, and for the same reason. A traversal
            # is N+1 statements on one connection, and PostgreSQL's default
            # isolation takes its snapshot per STATEMENT: without this, level 2
            # reads a graph level 1 never saw. Measured against a live server -
            # a --ab--> b, with another connection committing `DELETE ab` and
            # `INSERT bc` between the two levels - the store returned nodes
            # a,b,c and edges ab,bc: the deleted edge AND the new one, an
            # answer for neither the graph before the write nor the one after.
            # The in-memory walk cannot produce that; it reads dictionaries it
            # holds. On a shared store several writers is the case this backend
            # exists for, so that interleaving is the normal case rather than a
            # race to engineer - which is the argument load_graph_data already
            # makes for itself, in this file. Stated rather than inherited,
            # like the load and the save: leaving it to the environment was the
            # asymmetry.
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            # After the isolation level, which must be the transaction's first
            # statement, and before anything that reads a scoped table.
            self._bind_scope(conn)
            self._claim_or_check_graph_identity(conn)
            # The anchor has to exist, and an anchor that does not is not an
            # empty traversal but no traversal: the in-memory walk returns
            # nothing at all rather than a lone anchor. An anchor this instance
            # may not see does not exist as far as it is concerned, which is
            # the same answer for the same reason.
            present = conn.execute(
                sql.SQL("SELECT 1 FROM {} WHERE id = %s{}").format(
                    self._table("graph_nodes"), anchor_scope
                ),
                (anchor_id, *anchor_params),
            ).fetchone()
            if not present:
                return {"node_ids": [], "edge_ids": []}

            seen = {anchor_id}
            node_ids = [anchor_id]
            edge_ids: List[str] = []
            edges_seen: set = set()
            frontier = [anchor_id]
            for _ in range(depth):
                if not frontier:
                    break
                rows = conn.execute(
                    query,
                    {
                        "frontier": frontier,
                        "anchor": anchor_id,
                        "archived_ok": bool(include_archived),
                        "any_type": not types,
                        "types": types,
                        "scope": self._scope,
                    },
                    # Never prepared, and this is the one query in this backend
                    # that must not be. Its selectivity is the frontier's size:
                    # one id on the first level, thousands by the third. psycopg
                    # prepares a statement after a few executions and PostgreSQL
                    # then plans a prepared statement GENERICALLY - without the
                    # array in hand - so it plans for the small case and meets
                    # the large one. Measured at 50k nodes, depth 3 from the
                    # biggest hub: 329, 209, 201, then 4358 ms and never fast
                    # again, because the plan is cached for the connection's
                    # life. A traversal issues one of these per level, so a
                    # handful of requests is enough to fall off that cliff.
                    prepare=False,
                ).fetchall()
                reached = []
                for edge_id, far_id, is_node in rows:
                    if edge_id not in edges_seen:
                        edges_seen.add(edge_id)
                        edge_ids.append(edge_id)
                    if far_id not in seen:
                        seen.add(far_id)
                        reached.append(far_id)
                        if is_node:
                            node_ids.append(far_id)
                frontier = reached
        return {"node_ids": node_ids, "edge_ids": edge_ids}

    def exists(self) -> bool:
        self._ensure_schema()
        with self._pool.connection() as conn:
            with conn.transaction():
                self._claim_or_check_graph_identity(conn)
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
                self._claim_or_check_graph_identity(conn)
                self._bind_scope(conn)
                where, scope_params = self._where_scope()
                nodes = [
                    row[0]
                    for row in conn.execute(
                        sql.SQL("SELECT doc FROM {}{} ORDER BY id").format(
                            self._table("graph_nodes"), where
                        ),
                        scope_params or None,
                    )
                ]
                edges = [
                    row[0]
                    for row in conn.execute(
                        sql.SQL("SELECT doc FROM {}{} ORDER BY id").format(
                            self._table("graph_edges"), where
                        ),
                        scope_params or None,
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
            "metadata": self._metadata_without_graph_identity(row[0]) if row else {},
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
        metadata = self._metadata_without_graph_identity(data.get("metadata") or {})
        metadata[GRAPH_IDENTITY_KEY] = self._graph_name
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
                self._bind_scope(conn)
                # Before the first statement that READS the store takes its
                # snapshot, so a writer that waited here re-reads what the
                # other one left. The scope binding above takes no snapshot
                # this depends on: under READ COMMITTED the DELETE below takes
                # its own when it starts, which is after the lock.
                conn.execute(
                    "SELECT pg_advisory_xact_lock(%s, hashtext(%s))",
                    (SAVE_LOCK_KEY, self.schema),
                )
                self._claim_or_check_graph_identity(conn, force=True)
                # Exactly the rows the load would have returned, which is what
                # makes "replace the whole graph" mean the same thing here as
                # it does everywhere else. Deleting less than that is the
                # interesting failure: the load hands GraphStorage a row this
                # statement then leaves behind, and the insert below dies on
                # its primary key.
                where, scope_params = self._where_scope()
                for table in ("graph_nodes", "graph_edges"):
                    conn.execute(
                        sql.SQL("DELETE FROM {}{}").format(self._table(table), where),
                        scope_params or None,
                    )
                columns, placeholders = self._insert_shape()
                conn.cursor().executemany(
                    sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                        self._table("graph_nodes"), columns, placeholders
                    ),
                    [self._insert_params(node["id"], node) for node in nodes],
                )
                conn.cursor().executemany(
                    sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
                        self._table("graph_edges"), columns, placeholders
                    ),
                    [self._insert_params(edge["id"], edge) for edge in edges],
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

        # A whole-graph save replaces every row and leaves the planner's
        # statistics describing a table that no longer exists - an empty one,
        # for a store being written for the first time. The traversal's
        # expression indexes are then present and unused: measured on 300k
        # edges, a depth-2 traversal took 185 ms planned against stale
        # statistics and 2.2 ms once they were current, an 85x difference the
        # indexes alone do not deliver. Autovacuum gets there on its own, but
        # not before an instance that has just loaded starts serving.
        # ~150 ms on that 300k table, against 10.4 s for the save it follows.
        #
        # Outside the transaction so it cannot fail the save. That is the whole
        # reason: ANALYZE is perfectly legal inside a transaction block and its
        # result is visible there - unlike VACUUM - so a comment claiming
        # otherwise would be wrong.
        #
        # The notice handler is not decoration. A role with the DML grants
        # docs/PERSISTENCE_BACKENDS.md tells an operator to hand out, and no
        # ownership, does not get an error from ANALYZE: the server emits
        # `WARNING: permission denied to analyze "graph_edges", skipping it`
        # and reports success. Without this, the one deployment most likely to
        # hit it is the one that would never be told - and it would keep the
        # stale-statistics regression this exists to close. CREATE INDEX in the
        # same situation DOES raise, which is why the two are handled
        # differently rather than alike.
        # Removed again in the `finally` below, not left on the connection.
        # It goes back to the pool when this block exits, and a handler left
        # behind attaches to whatever runs on it next: four saves leave four
        # handlers, and an unrelated DROP's notices then print four times,
        # each labelled as coming from ANALYZE.
        def _report(diag: Any) -> None:
            logger.warning(
                f"ANALYZE after save: {diag.severity}: {diag.message_primary}"
            )

        try:
            with self._pool.connection() as conn:
                conn.add_notice_handler(_report)
                try:
                    for table in ("graph_nodes", "graph_edges"):
                        conn.execute(sql.SQL("ANALYZE {}").format(self._table(table)))
                finally:
                    conn.remove_notice_handler(_report)
        except Exception as exc:
            logger.warning(
                f"could not ANALYZE after save; the traversal's "
                f"indexes may go unused: {exc}"
            )

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
                self._bind_scope(conn)
                conn.execute(
                    "SELECT pg_advisory_xact_lock_shared(%s, hashtext(%s))",
                    (SAVE_LOCK_KEY, self.schema),
                )
                self._claim_or_check_graph_identity(conn)
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
            # The predicate is not redundant with the policy: where the server
            # cannot enforce one, this is what stops an instance deleting a row
            # that is not its to delete.
            scope, scope_params = self._and_scope()
            conn.execute(
                sql.SQL("DELETE FROM {} WHERE id = %s{}").format(
                    self._table(table), scope
                ),
                (operation.entity_id, *scope_params),
            )
            return
        columns, placeholders = self._insert_shape()
        if not self._scope_column:
            conn.execute(
                sql.SQL(
                    "INSERT INTO {} ({}) VALUES ({})"
                    " ON CONFLICT (id) DO UPDATE SET doc = EXCLUDED.doc"
                ).format(self._table(table), columns, placeholders),
                self._insert_params(operation.entity_id, operation.payload),
            )
            return
        # The scope travels with the row. A row written before this instance
        # had a scope carries none, and adopting it here is what stops the
        # store drifting into two kinds of row that the whole-graph save would
        # then have to reconcile.
        #
        # The predicate on the conflicting row is the same one every read
        # carries, and it is what makes an upsert unable to reach a row that is
        # not this instance's. `id` is the primary key of the TABLE rather than
        # of the table per scope, so the conflicting row may belong to another
        # scope - and without this the statement replaced its content and
        # restamped its scope, which is a cross-scope write in the one path
        # that had none. Measured before this predicate existed: the other
        # scope's node was gone and its next load returned nothing.
        #
        # The row count is read rather than assumed, because a DO UPDATE whose
        # WHERE does not match is not an error - the statement affects no rows
        # and raises nothing, which would tell the caller a write happened that
        # did not. Measured on PostgreSQL 16: 0 for exactly that case, and 1
        # for a fresh insert, an own-scope update and a row carrying no scope.
        cursor = conn.execute(
            sql.SQL(
                "INSERT INTO {} ({}) VALUES ({})"
                " ON CONFLICT (id) DO UPDATE"
                " SET doc = EXCLUDED.doc, {col} = EXCLUDED.{col}"
                " WHERE ({bare}.{col} IS NULL OR {bare}.{col} = %s)"
            ).format(
                self._table(table),
                columns,
                placeholders,
                col=sql.Identifier(SCOPE_COLUMN),
                bare=sql.Identifier(table),
            ),
            self._insert_params(operation.entity_id, operation.payload)
            + (self._scope,),
        )
        if cursor.rowcount == 0:
            raise CrossScopeWriteRefused(
                f"{operation.kind} {operation.entity_id!r} in schema "
                f"{self.schema!r} carries a scope this instance may not write. "
                f"An id is unique across the whole table, so two scopes cannot "
                f"both hold one - see docs/PERSISTENCE_BACKENDS.md, "
                f"'Keeping scopes apart'."
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
            with self._pool.connection() as conn:
                with conn.transaction():
                    self._claim_or_check_graph_identity(conn)
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
                    logger.warning(
                        f"the listener thread for {self._channel} "
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
                logger.warning(
                    f"reconnecting to {self._channel} after {type(exc).__name__}: {exc}"
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
                    logger.warning(
                        f"lost the listening connection on "
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

        The shape is checked *here* because this is where the announcement is
        already being parsed: the origin decides whether to deliver at all, so
        `json.loads` and the origin lookup happen regardless, and reading the
        entries is one comprehension inside the same `try`. It is not bought
        by what it saves. The read-back is deferred, so a malformed entry left
        to reach it raises only when the application asks for the content, and
        what follows is the same whole-graph reload this road takes - measured
        both ways, one drain and one reload each, indistinguishable on the
        clock. What differs is the diagnosis, and not in this road's favour:
        from there it is reported as a content read that failed, which is a
        different thing to look for than an announcement this build cannot
        parse, and from here it is reported as nothing at all - the weaker
        half of that trade, and worth fixing on its own.
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
        # Read ON DEMAND, not here. The application asks for the content once
        # it has settled its own writes, so what gets read is the store's
        # answer with that instance's work already in it - where reading now
        # would hand over content that predates it, and leave the application
        # arbitrating with a wall clock that does not order the commits.
        #
        # "Later", not "elsewhere": the application applies the report inline,
        # so the read normally happens on THIS thread, further down this call
        # stack, inside _deliver. A report that arrives before the
        # application's first load has returned is the exception - it is held
        # and replayed on the thread that finished the load, with this one
        # long gone. Either way what makes it safe is not which thread it is
        # on but what the application has done by then - it has drained its
        # write queue and holds the lock every mutation needs in order to be
        # queued, so nothing of its own is competing for the pool - and
        # _resolve takes a pooled connection, which does not care.
        #
        # A read that fails there is not this thread's to absorb, though: the
        # application turns it into a reload, which is what this used to do
        # here, and the listening connection is not in its path.
        self._deliver(
            ExternalChange.entities_read_on_demand(lambda: self._resolve(pairs))
        )

    def _resolve(self, pairs: Sequence[Tuple[str, str]]) -> List[EntityOperation]:
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
        # The scope predicate below is built from what the migration found, and
        # this method does not migrate - it does not need to, because the only
        # caller is the listening thread and that thread exists only once
        # `start_change_notification` has run, which does. Stated because it is
        # the one read path here whose correctness rests on an ordering rather
        # than on its own first line.
        scope, scope_params = self._and_scope()
        with self._pool.connection() as conn, conn.transaction():
            # In a transaction of its own, because the scope is bound to one
            # and this read is otherwise the only path here that has none.
            self._bind_scope(conn)
            for kind, table in (("node", "graph_nodes"), ("edge", "graph_edges")):
                ids = [entity_id for k, entity_id in pairs if k == kind]
                if not ids:
                    continue
                rows = conn.execute(
                    sql.SQL("SELECT id, doc FROM {} WHERE id = ANY(%s){}").format(
                        self._table(table), scope
                    ),
                    (ids, *scope_params),
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
        return operations

    def _deliver(self, change: ExternalChange) -> None:
        """Hand one change to the listener, holding nothing of ours while it runs.

        Nothing of ours is held here on purpose. The listener refreshes an
        application that waits for its own write queue first, and that queue's
        writes need this pool - so a pooled connection held across this call
        would have the refresh waiting for a writer that is waiting for the
        connection the refresh has.

        The read-back is inside the listener now rather than before it, which
        is not the same hazard turned back on: it runs after that wait, with
        the queue drained and the application's lock held, so there is no
        writer of its own left to wait for. What must not happen is a
        connection taken HERE and held across the call, which is why there is
        none.
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
            logger.warning(f"change notification refused: {exc}")
        except Exception as exc:
            logger.warning(
                f"applying an external change failed: {type(exc).__name__}: {exc}"
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
    # The two a host must be able to catch, so they belong in a curated list
    # as much as the tuning constants do.
    "CrossScopeWriteRefused",
    "ScopeIsolationUnavailable",
    "SAVE_LOCK_KEY",
    "DEFAULT_POOL_SIZE",
    "MIGRATION_LOCK_KEY",
    "NOTIFY_PAYLOAD_LIMIT",
    "NOTIFY_POLL_SECONDS",
    "NOTIFY_RECONNECT_MAX_SECONDS",
    "SCOPE_COLUMN",
    "SCOPE_POLICY_SUFFIX",
    "SCOPE_SETTING",
    "SCOPED_TABLES",
]
