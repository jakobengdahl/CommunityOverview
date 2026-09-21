"""graph_file_to_postgres against a real server, as a role the policy binds.

Row-level security is inert for a superuser, so the suite's own DSN cannot
ask whether anything is refused: every conversion here runs as a role that
owns its schema and is neither superuser nor exempt, which is what a
self-provisioning deployment is. The suite's DSN is used only to set that up
and to look underneath the policy afterwards.

Skips without a server, like the backend's own PostgreSQL suite; with
CO_REQUIRE_POSTGRES set, a missing driver or server is an error instead.
"""

import json
import os
import secrets
import uuid

import pytest

REQUIRE = os.environ.get("CO_REQUIRE_POSTGRES", "").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
if REQUIRE:
    import psycopg  # noqa: F401  (a skip here would be the failure, not a pass)
else:
    psycopg = pytest.importorskip("psycopg", reason="psycopg is optional")

from backend.core.postgres_backend import (  # noqa: E402  (after importorskip)
    PostgresGraphPersistenceBackend,
)
from backend.core.storage import GraphStorage  # noqa: E402  (after importorskip)
from scripts.graph_file_to_postgres import (  # noqa: E402  (after importorskip)
    GraphCounts,
    PostgresTargetInspector,
    main,
)

DSN = os.environ.get("CO_TEST_POSTGRES_DSN", "")


def _server_reachable() -> bool:
    if not DSN:
        if REQUIRE:
            raise RuntimeError(
                "CO_REQUIRE_POSTGRES=1 but CO_TEST_POSTGRES_DSN is unset"
            )
        return False
    try:
        with psycopg.connect(DSN, connect_timeout=3):
            return True
    except Exception as exc:
        if REQUIRE:
            raise RuntimeError(
                f"CO_REQUIRE_POSTGRES=1 but the server is unreachable: {type(exc).__name__}"
            ) from exc
        return False


pytestmark = pytest.mark.skipif(
    not _server_reachable(),
    reason="set CO_TEST_POSTGRES_DSN to a PostgreSQL server to run these",
)


@pytest.fixture(params=["plain", "MixedCase"], ids=["plain", "needs-quoting"])
def store(request):
    """A schema owned by a non-superuser role, and that role's DSN.

    Parametrised over the schema's name because the inspector reaches the
    catalog by `nspname` and the tables by quoted identifier: a name that only
    survives quoted is what tells a correct lookup from one that case-folds.
    """
    suffix = uuid.uuid4().hex[:12]
    role = f"co_conv_{suffix}"
    schema = f"CoConv_{suffix}" if request.param == "MixedCase" else f"co_conv_{suffix}"
    password = secrets.token_hex(16)
    ident = psycopg.sql.Identifier
    created = False
    try:
        with psycopg.connect(DSN, autocommit=True) as conn:
            try:
                conn.execute(
                    psycopg.sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                        ident(role), psycopg.sql.Literal(password)
                    )
                )
            except psycopg.errors.InsufficientPrivilege:
                pytest.skip("the test role may not create roles")
            created = True
            conn.execute(
                psycopg.sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(
                    ident(schema), ident(role)
                )
            )
        params = psycopg.conninfo.conninfo_to_dict(DSN)
        params.update(user=role, password=password)
        yield psycopg.conninfo.make_conninfo(**params), schema
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    ident(schema)
                )
            )
            if created:
                conn.execute(psycopg.sql.SQL("DROP OWNED BY {}").format(ident(role)))
                conn.execute(
                    psycopg.sql.SQL("DROP ROLE IF EXISTS {}").format(ident(role))
                )


def _graph_file(tmp_path, name, ids):
    path = tmp_path / f"{name}.json"
    path.write_text(
        json.dumps(
            {
                "nodes": [{"id": i, "type": "Thing", "name": i} for i in ids],
                "edges": [
                    {"id": f"{a}-{b}", "type": "RELATED_TO", "source": a, "target": b}
                    for a, b in zip(ids, ids[1:])
                ],
                "metadata": {"graph_name": name},
            }
        ),
        encoding="utf-8",
    )
    return path


def _convert(path, dsn, schema, *extra):
    return main(
        [str(path), "--dsn", dsn, "--schema", schema, "--pool-size", "1", *extra]
    )


def _nodes_by_scope(schema):
    """Underneath the policy, as the suite's superuser."""
    with psycopg.connect(DSN) as conn:
        return dict(
            conn.execute(
                psycopg.sql.SQL(
                    "SELECT coalesce(scope_id, '<none>'), count(*) FROM {}.graph_nodes GROUP BY 1"
                ).format(psycopg.sql.Identifier(schema))
            ).fetchall()
        )


def _graph_name(schema):
    with psycopg.connect(DSN) as conn:
        return conn.execute(
            psycopg.sql.SQL("SELECT doc->>'graph_name' FROM {}.graph_metadata").format(
                psycopg.sql.Identifier(schema)
            )
        ).fetchone()[0]


def _ids_seen_by_scope(dsn, schema, scope):
    with psycopg.connect(dsn) as conn:
        with conn.transaction():
            conn.execute("SELECT set_config('app.graph_scope', %s, true)", (scope,))
            return sorted(
                row[0]
                for row in conn.execute(
                    psycopg.sql.SQL("SELECT id FROM {}.graph_nodes").format(
                        psycopg.sql.Identifier(schema)
                    )
                )
            )


def _drop_server_isolation(schema):
    """Leave the scoped rows and take away everything the server enforces:
    scopes kept apart by the application alone."""
    with psycopg.connect(DSN, autocommit=True) as conn:
        for table in ("graph_nodes", "graph_edges"):
            name = psycopg.sql.Identifier(schema, table)
            conn.execute(
                psycopg.sql.SQL("DROP POLICY IF EXISTS {} ON {}").format(
                    psycopg.sql.Identifier(f"{table}_scope_policy"), name
                )
            )
            conn.execute(
                psycopg.sql.SQL(
                    "ALTER TABLE {} NO FORCE ROW LEVEL SECURITY, DISABLE ROW LEVEL SECURITY"
                ).format(name)
            )


def test_an_unscoped_conversion_cannot_publish_a_graph_to_every_scope(
    store, tmp_path, capsys
):
    dsn, schema = store
    assert (
        _convert(_graph_file(tmp_path, "A", ["a1", "a2"]), dsn, schema, "--scope", "A")
        == 0
    )

    unscoped = _graph_file(tmp_path, "U", ["u1", "u2", "u3"])
    # With and without the flag the refusal used to suggest: an unscoped
    # session sees only the shared metadata row here, which is exactly what
    # --allow-non-empty-target was offered to replace.
    assert _convert(unscoped, dsn, schema) == 1
    first = capsys.readouterr().out
    assert "keeps scopes apart" in first
    assert "--allow-non-empty-target" not in first
    assert _convert(unscoped, dsn, schema, "--allow-non-empty-target") == 1
    assert "keeps scopes apart" in capsys.readouterr().out

    assert _nodes_by_scope(schema) == {"A": 2}
    assert _ids_seen_by_scope(dsn, schema, "B") == []
    assert _graph_name(schema) == "A"


@pytest.mark.parametrize("scoped_in", ["graph_nodes", "graph_edges"])
def test_a_store_keeping_scopes_apart_without_a_policy_is_refused_too(
    store, tmp_path, capsys, scoped_in
):
    """Scopes kept apart by the application alone: nothing hides a scoped row
    from an unscoped session, so the rows themselves are the evidence - in
    either table. The other table's scope is cleared so only one carries it."""
    dsn, schema = store
    graph = _graph_file(tmp_path, "A", ["a1", "a2"])
    assert _convert(graph, dsn, schema, "--scope", "A") == 0
    _drop_server_isolation(schema)
    other = "graph_edges" if scoped_in == "graph_nodes" else "graph_nodes"
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            psycopg.sql.SQL("UPDATE {} SET scope_id = NULL").format(
                psycopg.sql.Identifier(schema, other)
            )
        )
    before = _nodes_by_scope(schema)

    unscoped = _graph_file(tmp_path, "U", ["u1"])
    assert _convert(unscoped, dsn, schema, "--allow-non-empty-target") == 1
    assert f"row(s) in {scoped_in} carry a scope" in capsys.readouterr().out
    assert _nodes_by_scope(schema) == before


@pytest.mark.parametrize("table", ["graph_nodes", "graph_edges"])
@pytest.mark.parametrize(
    "sign",
    [
        ("ALTER TABLE {table} ENABLE ROW LEVEL SECURITY", "row-level security enabled"),
        # A name of the operator's choosing: the backend's is not the only one.
        ("CREATE POLICY operator_named ON {table} USING (true)", "a policy"),
        (
            "CREATE POLICY operator_named ON {table} AS RESTRICTIVE USING (true)",
            "a policy",
        ),
        # A single-command policy, not FOR ALL: still a sign the server is
        # asked to keep rows apart, whichever command it governs.
        (
            "CREATE POLICY operator_named ON {table} FOR SELECT USING (true)",
            "a policy",
        ),
    ],
    ids=[
        "rls-without-a-policy",
        "a-policy-without-rls",
        "a-restrictive-policy",
        "a-select-only-policy",
    ],
)
def test_each_sign_of_isolation_is_refused_alone_on_either_table(
    store, tmp_path, capsys, table, sign
):
    sign, named = sign
    """Either sign is a store asking the server to keep rows apart, and either
    table carries it. Added to an ordinary store, so nothing else is a sign."""
    dsn, schema = store
    assert _convert(_graph_file(tmp_path, "G", ["g1"]), dsn, schema) == 0
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            psycopg.sql.SQL(sign.format(table="{}")).format(
                psycopg.sql.Identifier(schema, table)
            )
        )

    assert (
        _convert(
            _graph_file(tmp_path, "U", ["u1"]), dsn, schema, "--allow-non-empty-target"
        )
        == 1
    )
    assert f"keeps scopes apart ({table} has {named})" in capsys.readouterr().out
    assert _nodes_by_scope(schema) == {"<none>": 1}


SHARED = (
    "rows carrying no scope and the metadata row are shared by every scope in "
    "the schema, and --allow-non-empty-target replaces them for all of them"
)
# Last, so nothing after it can say which case this is.
ONLY_IF = "pass it only if this schema holds no graph but this scope's\n"


def test_a_second_scope_is_told_what_the_flag_would_replace(store, tmp_path, capsys):
    """One schema, two scopes: the second sees only the first's metadata row,
    which every scope in the schema reads."""
    dsn, schema = store
    assert (
        _convert(_graph_file(tmp_path, "A", ["a1"]), dsn, schema, "--scope", "A") == 0
    )

    assert (
        _convert(_graph_file(tmp_path, "B", ["b1"]), dsn, schema, "--scope", "B") == 1
    )
    refusal = capsys.readouterr().out
    assert SHARED in refusal and ONLY_IF in refusal
    assert _graph_name(schema) == "A"
    assert _nodes_by_scope(schema) == {"A": 1}


def test_a_scope_in_its_own_started_schema_is_pointed_at_the_flag(
    store, tmp_path, capsys
):
    """The commonest way to find metadata and nothing else: the application
    started against the empty schema and saved an empty graph. That schema is
    the scope's own, so the refusal must leave the flag open, and it works."""
    dsn, schema = store
    backend = PostgresGraphPersistenceBackend(dsn, schema=schema, scope="A")
    try:
        storage = GraphStorage(persistence_backend=backend)
        storage.shutdown_events()
    finally:
        backend.close()

    graph = _graph_file(tmp_path, "A", ["a1", "a2"])
    assert _convert(graph, dsn, schema, "--scope", "A") == 1
    assert ONLY_IF in capsys.readouterr().out
    assert _convert(graph, dsn, schema, "--scope", "A", "--allow-non-empty-target") == 0
    assert _nodes_by_scope(schema) == {"A": 2}


def test_rows_left_unscoped_are_named_as_shared_to_a_scoped_conversion(
    store, tmp_path, capsys
):
    """An older graph written before any scope: its rows carry none, so every
    scope reads them, and a scoped replace would delete them for all."""
    dsn, schema = store
    assert _convert(_graph_file(tmp_path, "old", ["o1", "o2"]), dsn, schema) == 0

    assert (
        _convert(_graph_file(tmp_path, "B", ["b1"]), dsn, schema, "--scope", "B") == 1
    )
    refusal = capsys.readouterr().out
    assert "this scope sees 2 node(s), 1 edge(s)" in refusal
    assert SHARED in refusal and ONLY_IF in refusal
    assert _nodes_by_scope(schema) == {"<none>": 2}


def test_an_ordinary_unscoped_store_is_not_refused(store, tmp_path):
    """The common case, and the one a false positive would block: a store that
    has only ever held one graph. The second run finds the column the first
    boot added, with every row unscoped and no policy - still not isolation."""
    dsn, schema = store
    assert _convert(_graph_file(tmp_path, "G", ["g1", "g2"]), dsn, schema) == 0
    replacement = _graph_file(tmp_path, "H", ["h1"])
    assert _convert(replacement, dsn, schema, "--allow-non-empty-target") == 0

    assert _nodes_by_scope(schema) == {"<none>": 1}
    assert _graph_name(schema) == "H"


@pytest.mark.parametrize("server_isolation", [True, False], ids=["forced", "none"])
def test_rows_in_scope_counts_only_rows_carrying_that_scope(
    store, tmp_path, server_isolation
):
    """Asked as the owner. Under the forced policy, without the session
    setting the policy would hide every scoped row; with no policy at all,
    only the count's own predicate keeps the other scope's row out. Either
    way a predicate admitting scopeless rows would count the stray one."""
    dsn, schema = store
    assert (
        _convert(
            _graph_file(tmp_path, "A", ["a1", "a2", "a3"]), dsn, schema, "--scope", "A"
        )
        == 0
    )
    with psycopg.connect(DSN, autocommit=True) as conn:
        for row_id, scope in (("stray", None), ("elsewhere", "B")):
            conn.execute(
                psycopg.sql.SQL(
                    "INSERT INTO {}.graph_nodes (id, doc, scope_id) VALUES (%s, %s, %s)"
                ).format(psycopg.sql.Identifier(schema)),
                (row_id, json.dumps({"id": row_id, "type": "Thing"}), scope),
            )

    if not server_isolation:
        _drop_server_isolation(schema)

    counted = PostgresTargetInspector(dsn, schema=schema).rows_in_scope("A")

    assert counted == GraphCounts(nodes=3, edges=2)


def _insert_node(schema, row_id, scope):
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            psycopg.sql.SQL(
                "INSERT INTO {}.graph_nodes (id, doc, scope_id) VALUES (%s, %s, %s)"
            ).format(psycopg.sql.Identifier(schema)),
            (row_id, json.dumps({"id": row_id, "type": "Thing"}), scope),
        )


def test_rows_in_scope_uses_exact_match_not_a_loosened_comparison(store, tmp_path):
    """Server-side RLS is dropped first: it enforces its own exact match, so
    with it in place a stray row is hidden before `rows_in_scope`'s own
    comparison ever gets a say (see the "none" case of
    `test_rows_in_scope_counts_only_rows_carrying_that_scope`). With nothing
    else in the way, `=` must not be treated as `LIKE` (`_` and `%` are
    wildcards), as a case-folded compare, or as a prefix check - each stray
    row below matches the scope asked for under exactly one of those
    loosened comparisons and must not be counted."""
    dsn, schema = store
    # Establishes the scope column that rows_in_scope's exact match then has
    # to enforce alone, once the policy below is dropped.
    assert (
        _convert(_graph_file(tmp_path, "T", ["t1"]), dsn, schema, "--scope", "t_1") == 0
    )
    _drop_server_isolation(schema)
    _insert_node(schema, "a-real", "A")
    _insert_node(schema, "a-lower", "a")  # matches "A" under lower()=lower()
    _insert_node(schema, "ab-prefix", "AB")  # matches "A" under starts_with()
    _insert_node(schema, "tx1-like", "tx1")  # matches "t_1" under LIKE ('_' wildcard)

    inspector = PostgresTargetInspector(dsn, schema=schema)
    assert inspector.rows_in_scope("A") == GraphCounts(nodes=1, edges=0)
    assert inspector.rows_in_scope("t_1") == GraphCounts(nodes=1, edges=0)


def test_isolation_evidence_reaches_a_later_table_past_a_missing_one(store):
    """SCOPED_TABLES is walked table by table; a table that does not exist
    yet must not stop the walk before a later table's own RLS is reached -
    continue past the missing one, not break out of the loop."""
    dsn, schema = store
    with psycopg.connect(DSN, autocommit=True) as conn:
        # graph_nodes (first in SCOPED_TABLES) is deliberately never created.
        conn.execute(
            psycopg.sql.SQL("CREATE TABLE {} (id text primary key)").format(
                psycopg.sql.Identifier(schema, "graph_edges")
            )
        )
        conn.execute(
            psycopg.sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(
                psycopg.sql.Identifier(schema, "graph_edges")
            )
        )

    evidence = PostgresTargetInspector(dsn, schema=schema).isolation_evidence()
    assert evidence == ["graph_edges has row-level security enabled"]


def test_isolation_evidence_is_raised_without_a_scope_column(store):
    """RLS or a policy is itself the evidence, whether or not the scope
    column exists yet - a table an operator locked down before any
    conversion ran the migration that adds it."""
    dsn, schema = store
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(
            psycopg.sql.SQL("CREATE TABLE {} (id text primary key)").format(
                psycopg.sql.Identifier(schema, "graph_nodes")
            )
        )
        conn.execute(
            psycopg.sql.SQL("ALTER TABLE {} ENABLE ROW LEVEL SECURITY").format(
                psycopg.sql.Identifier(schema, "graph_nodes")
            )
        )

    evidence = PostgresTargetInspector(dsn, schema=schema).isolation_evidence()
    assert evidence == ["graph_nodes has row-level security enabled"]


def test_rows_in_scope_reports_zero_when_nothing_matches(store, tmp_path):
    dsn, schema = store
    assert (
        _convert(_graph_file(tmp_path, "A", ["a1", "a2"]), dsn, schema, "--scope", "A")
        == 0
    )

    counted = PostgresTargetInspector(dsn, schema=schema).rows_in_scope("no-such-scope")
    assert counted == GraphCounts(nodes=0, edges=0)
