"""The PostgreSQL backend against the layer-store contract, and what is its
own: the tables are opt-in and idempotent, a scope keeps layers apart the
way it keeps graph rows apart, and a merge is announced like any write.

Skips with the rest of the PostgreSQL suite when no server is configured;
see test_persistence_contract_postgres.py.
"""

from __future__ import annotations

import secrets
import threading
import uuid
from datetime import timedelta

import pytest

from backend.core.tests.test_persistence_contract_postgres import (
    DSN,
    _dbname,
    _dsn_as_role,
    _provision_revision_sequence,
    _server_reachable,
    psycopg,
)

from backend.core import layer_store as ls  # noqa: E402
from backend.core.postgres_backend import (  # noqa: E402
    LAYER_TABLES,
    SCOPE_POLICY_SUFFIX,
    CrossScopeWriteRefused,
    PostgresGraphPersistenceBackend,
)
from backend.core.storage_backends import EntityOperation, NodeTypeChanged  # noqa: E402
from backend.core.tests.layer_store_contract import (  # noqa: E402
    A,
    B,
    LayerStoreContract,
    entry,
    fresh,
)
from backend.core.tests.persistence_contract import (  # noqa: E402
    by_id,
    edge_payload,
    node_payload,
    snapshot,
)

pytestmark = pytest.mark.skipif(
    not _server_reachable(), reason="no PostgreSQL server configured"
)


@pytest.fixture
def schema():
    name = f"co_layer_{uuid.uuid4().hex[:16]}"
    yield name
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')


@pytest.fixture
def backends():
    made = []
    yield made
    for backend in made:
        backend.close()


@pytest.fixture
def make(schema, backends):
    def build(**kwargs):
        kwargs.setdefault("staged_sessions", True)
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema, **kwargs)
        backends.append(backend)
        return backend

    return build


def tables_in(schema):
    with psycopg.connect(DSN) as conn:
        return {
            row[0]
            for row in conn.execute(
                "SELECT c.relname FROM pg_class c JOIN pg_namespace n"
                " ON n.oid = c.relnamespace WHERE n.nspname = %s AND c.relkind = 'r'",
                (schema,),
            )
        }


class TestPostgresLayerStoreContract(LayerStoreContract):
    @pytest.fixture
    def factory(self, make):
        return make


class TestPostgresScopedLayerStoreContract(LayerStoreContract):
    """The whole contract again with a scope configured: the policy then
    applies to every layer statement, and must change no answer."""

    @pytest.fixture
    def factory(self, make):
        return lambda: make(scope="scope-a")


@pytest.fixture
def owner():
    """A non-superuser role owning a schema of its own.

    The suite's own DSN is a superuser, and a superuser bypasses row-level
    security even when it is FORCED, so a clause run through it says nothing
    about what the policy does to a layer statement. This is the role a
    self-provisioning deployment runs as: it owns its tables, so FORCE is
    what puts it under the policy.
    """
    suffix = uuid.uuid4().hex[:12]
    name, schema = f"co_lown_{suffix}", f"co_lown_{suffix}_sch"
    password = secrets.token_hex(16)
    ident = psycopg.sql.Identifier
    with psycopg.connect(DSN, autocommit=True) as conn:
        try:
            conn.execute(
                psycopg.sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER PASSWORD {}").format(
                    ident(name), psycopg.sql.Literal(password)
                )
            )
        except psycopg.errors.InsufficientPrivilege:
            pytest.skip("the test role may not create roles")
        conn.execute(
            psycopg.sql.SQL("CREATE SCHEMA {} AUTHORIZATION {}").format(
                ident(schema), ident(name)
            )
        )
    try:
        yield _dsn_as_role(name, password), schema
    finally:
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                    ident(schema)
                )
            )
            conn.execute(psycopg.sql.SQL("DROP OWNED BY {}").format(ident(name)))
            conn.execute(psycopg.sql.SQL("DROP ROLE IF EXISTS {}").format(ident(name)))


@pytest.fixture
def make_as_owner(owner, backends):
    dsn, schema = owner

    def build(**kwargs):
        kwargs.setdefault("staged_sessions", True)
        backend = PostgresGraphPersistenceBackend(dsn, schema=schema, **kwargs)
        backends.append(backend)
        return backend

    return build


class TestPostgresScopedLayerStoreContractUnderTheServersPolicy(LayerStoreContract):
    """The contract once more, scoped, as a role the FORCED policy binds."""

    @pytest.fixture
    def factory(self, make_as_owner):
        return lambda: make_as_owner(scope="scope-a")


class TestTheLayerTablesAreOptIn:
    def test_a_backend_without_staged_sessions_creates_no_layer_table(
        self, make, schema
    ):
        backend = make(staged_sessions=False)
        backend.save_graph_data(snapshot([node_payload("a")]))
        assert not backend.capabilities().staged_sessions
        assert tables_in(schema) == {"graph_nodes", "graph_edges", "graph_metadata"}
        with pytest.raises(RuntimeError, match="staged_sessions"):
            backend.load_layer(A)

    def test_enabling_it_creates_the_tables_once_and_keeps_the_graph(
        self, make, schema
    ):
        make(staged_sessions=False).save_graph_data(snapshot([node_payload("a")]))
        enabled = make()
        enabled.record_staged(A)
        assert set(LAYER_TABLES) <= tables_in(schema)
        assert [n["id"] for n in enabled.load_graph_data()["nodes"]] == ["a"]
        again = make()
        assert again.staged_status(A) == ls.STATUS_ACTIVE

    def test_instances_booting_together_create_the_tables_once(self, make, schema):
        errors = []
        start = threading.Barrier(6)

        def boot():
            try:
                backend = make()
                start.wait()
                backend.staged_status(A)
            except Exception as exc:  # pragma: no cover - the failure reported
                errors.append(exc)

        threads = [threading.Thread(target=boot) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        assert errors == []
        assert set(LAYER_TABLES) <= tables_in(schema)

    def test_a_disabled_instance_ignores_existing_layer_tables(self, make):
        make().record_staged(A)
        plain = make(staged_sessions=False)
        plain.save_graph_data(snapshot([node_payload("a")]))
        plain.apply_batch([EntityOperation.upsert_node(node_payload("b"))])
        assert {n["id"] for n in plain.load_graph_data()["nodes"]} == {"a", "b"}


class TestScopesKeepLayersApart:
    """Run as a role the FORCED policy binds, so the server's half of the
    isolation is exercised on every write, not only the application's."""

    @pytest.fixture
    def pair(self, make_as_owner):
        first = make_as_owner(scope="scope-a")
        second = make_as_owner(scope="scope-b")
        first.save_graph_data(snapshot([node_payload("a", revision=1)]))
        return first, second

    def test_the_policy_covers_every_layer_table(self, pair, owner):
        _, schema = owner
        with psycopg.connect(DSN) as conn:
            for table in LAYER_TABLES:
                row = conn.execute(
                    "SELECT c.relrowsecurity, c.relforcerowsecurity,"
                    " EXISTS (SELECT 1 FROM pg_policy p WHERE p.polrelid = c.oid"
                    "         AND p.polname = %s)"
                    " FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
                    " WHERE n.nspname = %s AND c.relname = %s",
                    (table + SCOPE_POLICY_SUFFIX, schema, table),
                ).fetchone()
                assert row == (True, True, True), table

    def test_one_scope_sees_nothing_of_another_scopes_sessions(self, pair):
        first, second = pair
        first.record_staged(A)
        first.stage(A, [entry("node", "a")])
        first.merge(A, "m-a", [], [], [("node", "a")], {"of": "scope-a"})
        first.stage(A, [entry("node", "a", session_seq=2)])
        assert second.staged_status(A) == ls.STATUS_NONE
        assert second.load_layer(A) == []
        assert second.layers_in_use() == []
        assert second.load_merge("m-a") is None
        with pytest.raises(ls.StagedSessionNotActive):
            second.stage(A, [entry("node", "x")])
        with pytest.raises(ls.StagedSessionNotActive):
            second.discard(A)

    def test_one_scope_cannot_take_over_another_scopes_session_id(self, pair):
        first, second = pair
        first.record_staged(A)
        first.stage(A, [entry("node", "a")])
        with pytest.raises(CrossScopeWriteRefused):
            second.record_staged(A)
        with pytest.raises(CrossScopeWriteRefused):
            second.retire(A)
        second.drop_layer(A)
        assert first.staged_status(A) == ls.STATUS_ACTIVE
        assert [e["entity_id"] for e in first.load_layer(A)] == ["a"]

    def test_one_scope_cannot_reuse_another_scopes_merge_id(self, pair):
        first, second = pair
        first.record_staged(A)
        second.record_staged(B)
        first.stage(A, [entry("node", "a")])
        second.stage(B, [entry("node", "b", session_id=B)])
        first.merge(A, "m", [], [], [("node", "a")], {})
        with pytest.raises(ls.MergeIdTaken):
            second.merge(B, "m", [], [], [("node", "b")], {})
        assert [e["entity_id"] for e in second.load_layer(B)] == ["b"]

    @pytest.mark.parametrize(
        "first_scope,second_scope", [(None, "scope-a"), ("scope-a", None)]
    )
    def test_a_session_belongs_to_the_one_scope_that_recorded_it(
        self, make_as_owner, first_scope, second_scope
    ):
        """No scope is a scope of its own here. Shared with every scope the
        way an unscoped graph row is, a session would let a scoped instance
        stage entries its unscoped owner cannot see and `retire` would leave
        behind."""
        owner = make_as_owner(scope=first_scope)
        other = make_as_owner(scope=second_scope)
        owner.save_graph_data(snapshot([node_payload("a", revision=1)]))
        owner.record_staged(A)
        owner.stage(A, [entry("node", "q")])
        assert other.staged_status(A) == ls.STATUS_NONE
        assert other.load_layer(A) == [] and other.layers_in_use() == []
        with pytest.raises(ls.StagedSessionNotActive):
            other.stage(A, [entry("node", "z")])
        with pytest.raises(ls.StagedSessionNotActive):
            other.merge(A, "m", [], [], [("node", "q")], {})
        with pytest.raises(CrossScopeWriteRefused):
            other.record_staged(A)
        with pytest.raises(CrossScopeWriteRefused):
            other.retire(A)
        other.drop_layer(A)
        assert [e["entity_id"] for e in owner.load_layer(A)] == ["q"]
        owner.stage(A, [entry("node", "r")])
        owner.merge(A, "m-owner", [], [], [("node", "r")], {})
        assert other.load_merge("m-owner") is None
        other.record_staged(B)
        other.stage(B, [entry("node", "s", session_id=B)])
        with pytest.raises(ls.MergeIdTaken):
            other.merge(B, "m-owner", [], [], [("node", "s")], {})
        owner.retire(A)
        assert owner.layers_in_use() == [] and other.layers_in_use() == [B]

    def test_the_server_refuses_another_scopes_rows_without_the_predicate(
        self, pair, owner
    ):
        """The policy, not only the application's own predicate."""
        _, schema = owner
        first, _ = pair
        first.record_staged(A)
        first.stage(A, [entry("node", "a")])
        ident = psycopg.sql.Identifier
        role = f"co_layer_reader_{uuid.uuid4().hex[:8]}"
        with psycopg.connect(DSN, autocommit=True) as admin:
            admin.execute(psycopg.sql.SQL("CREATE ROLE {}").format(ident(role)))
            admin.execute(
                psycopg.sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                    ident(schema), ident(role)
                )
            )
            admin.execute(
                psycopg.sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA {} TO {}").format(
                    ident(schema), ident(role)
                )
            )
        try:
            with psycopg.connect(DSN) as conn:
                conn.execute(psycopg.sql.SQL("SET ROLE {}").format(ident(role)))
                conn.execute("SELECT set_config('app.graph_scope', 'scope-b', true)")
                for table in LAYER_TABLES:
                    count = conn.execute(
                        psycopg.sql.SQL("SELECT count(*) FROM {}.{}").format(
                            ident(schema), ident(table)
                        )
                    ).fetchone()[0]
                    assert count == 0, table
                conn.execute("SELECT set_config('app.graph_scope', 'scope-a', true)")
                count = conn.execute(
                    psycopg.sql.SQL("SELECT count(*) FROM {}.{}").format(
                        ident(schema), ident("graph_layer_entries")
                    )
                ).fetchone()[0]
                assert count == 1
        finally:
            with psycopg.connect(DSN, autocommit=True) as admin:
                admin.execute(psycopg.sql.SQL("DROP OWNED BY {}").format(ident(role)))
                admin.execute(psycopg.sql.SQL("DROP ROLE {}").format(ident(role)))


class TestAMergeOnASharedStore:
    def test_the_merge_is_announced_to_other_instances(self, make):
        writer = make()
        writer.save_graph_data(snapshot([node_payload("a", revision=1)]))
        writer.record_staged(A)
        writer.stage(A, [entry("node", "a")])
        reader = make()
        seen = []
        arrived = threading.Event()

        def listener(change):
            seen.append(change.with_content())
            arrived.set()

        reader.start_change_notification(listener)
        try:
            current = by_id(writer.load_graph_data(), "nodes")["a"]
            writer.merge(
                A,
                "m",
                [
                    EntityOperation(
                        "node",
                        "upsert",
                        "a",
                        fresh({**current, "name": "Merged"}),
                        expected_revision=current["revision"],
                    )
                ],
                [],
                [("node", "a")],
                {},
            )
            assert arrived.wait(30)
        finally:
            reader.stop_change_notification()
        ops = seen[0].operations
        assert [(op.kind, op.entity_id) for op in ops] == [("node", "a")]
        assert ops[0].payload["name"] == "Merged"
        assert "_write_token" not in ops[0].payload

    def test_the_revision_the_record_names_is_the_store_s_own(self, make):
        backend = make()
        backend.save_graph_data(snapshot([node_payload("a", revision=1)]))
        backend.record_staged(A)
        backend.stage(A, [entry("node", "a")])
        current = by_id(backend.load_graph_data(), "nodes")["a"]
        op = EntityOperation(
            "node",
            "upsert",
            "a",
            {**current, "name": "x", "revision": 999_999_999},
            expected_revision=current["revision"],
        )
        result = backend.merge(A, "m", [op], [], [("node", "a")], {})
        stored = by_id(backend.load_graph_data(), "nodes")["a"]["revision"]
        assert result.revisions == [stored]
        assert stored != 999_999_999

    def test_concurrent_merges_of_one_id_apply_once(self, make):
        backend = make()
        backend.save_graph_data(snapshot([node_payload("a", revision=1)], []))
        backend.record_staged(A)
        backend.stage(A, [entry("node", "a")])
        current = by_id(backend.load_graph_data(), "nodes")["a"]
        results, errors = [], []
        start = threading.Barrier(4)

        def attempt(n):
            other = make()
            op = EntityOperation(
                "node",
                "upsert",
                "a",
                {**current, "name": f"by {n}"},
                expected_revision=current["revision"],
            )
            start.wait()
            try:
                results.append(other.merge(A, "same", [op], [], [("node", "a")], {}))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=attempt, args=(n,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        assert errors == []
        assert sum(not r.replayed for r in results) == 1
        assert len({r.record["applied"][0]["revision"] for r in results}) == 1

    def test_an_edge_create_and_a_merged_node_delete_are_ordered(self, make):
        """The incident-edge check locks the node row, so an edge create
        elsewhere either lands first - and the merge races - or waits for the
        merge and then finds no endpoint."""
        backend = make()
        backend.save_graph_data(
            snapshot([node_payload("a", revision=1), node_payload("b", revision=2)], [])
        )
        backend.record_staged(A)
        backend.stage(A, [entry("node", "a", action="delete")])
        delete = EntityOperation("node", "delete", "a", expected_revision=1)
        draw_edge = EntityOperation(
            "edge",
            "upsert",
            "e",
            edge_payload("e", "b", "a"),
            endpoint_types=tuple(
                (node["id"], node["type"])
                for node in backend.load_graph_data()["nodes"]
            ),
        )
        outcomes, draw_outcomes, errors = [], [], []
        start = threading.Barrier(2)

        def merge():
            start.wait()
            try:
                make().merge(
                    A,
                    "m",
                    [delete],
                    [ls.MergeCheck.incident_edges("a", [])],
                    [("node", "a")],
                    {},
                )
                outcomes.append("merged")
            except ls.MergeRaced:
                outcomes.append("raced")
            except Exception as exc:
                errors.append(exc)

        def draw():
            start.wait()
            try:
                make().apply_batch([draw_edge])
                draw_outcomes.append("drawn")
            except NodeTypeChanged:
                draw_outcomes.append("refused")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=merge), threading.Thread(target=draw)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        assert not any(thread.is_alive() for thread in threads)
        assert errors == []
        data = backend.load_graph_data()
        nodes, edges = by_id(data, "nodes"), by_id(data, "edges")
        if outcomes == ["merged"]:
            assert "a" not in nodes and "e" not in edges
            assert draw_outcomes == ["refused"]
        else:
            assert outcomes == ["raced"] and "a" in nodes and "e" in edges
            assert draw_outcomes == ["drawn"]


class TestALeastPrivilegeRole:
    @pytest.fixture
    def lowpriv(self):
        suffix = uuid.uuid4().hex[:12]
        name, schema = f"co_lowl_{suffix}", f"co_lowl_{suffix}_sch"
        password = secrets.token_hex(16)
        database = psycopg.sql.Identifier(_dbname())
        with psycopg.connect(DSN, autocommit=True) as conn:
            try:
                conn.execute(
                    psycopg.sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                        psycopg.sql.Identifier(name), psycopg.sql.Literal(password)
                    )
                )
            except psycopg.errors.InsufficientPrivilege:
                pytest.skip("the test role may not create roles")
            public_had_create = conn.execute(
                "SELECT has_database_privilege('public', %s, 'CREATE')", (_dbname(),)
            ).fetchone()[0]
            if public_had_create:
                conn.execute(
                    psycopg.sql.SQL("REVOKE CREATE ON DATABASE {} FROM PUBLIC").format(
                        database
                    )
                )
            conn.execute(
                psycopg.sql.SQL("CREATE SCHEMA {}").format(
                    psycopg.sql.Identifier(schema)
                )
            )
            conn.execute(
                psycopg.sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                    psycopg.sql.Identifier(schema), psycopg.sql.Identifier(name)
                )
            )
            for table, columns in (
                ("graph_nodes", "id text PRIMARY KEY, doc jsonb NOT NULL"),
                ("graph_edges", "id text PRIMARY KEY, doc jsonb NOT NULL"),
                (
                    "graph_metadata",
                    "only_row boolean PRIMARY KEY DEFAULT true CHECK (only_row),"
                    " doc jsonb NOT NULL",
                ),
            ):
                conn.execute(
                    psycopg.sql.SQL("CREATE TABLE {}.{} ({})").format(
                        psycopg.sql.Identifier(schema),
                        psycopg.sql.Identifier(table),
                        psycopg.sql.SQL(columns),
                    )
                )
            _provision_revision_sequence(conn, schema, name)
        try:
            yield name, schema, password
        finally:
            with psycopg.connect(DSN, autocommit=True) as conn:
                conn.execute(
                    psycopg.sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(
                        psycopg.sql.Identifier(schema)
                    )
                )
                if public_had_create:
                    conn.execute(
                        psycopg.sql.SQL("GRANT CREATE ON DATABASE {} TO PUBLIC").format(
                            database
                        )
                    )
                conn.execute(
                    psycopg.sql.SQL("REVOKE ALL ON DATABASE {} FROM {}").format(
                        database, psycopg.sql.Identifier(name)
                    )
                )
                conn.execute(
                    psycopg.sql.SQL("DROP ROLE IF EXISTS {}").format(
                        psycopg.sql.Identifier(name)
                    )
                )

    def _grant(self, schema, name):
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                psycopg.sql.SQL(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {} TO {}"
                ).format(psycopg.sql.Identifier(schema), psycopg.sql.Identifier(name))
            )

    def test_without_staged_sessions_it_boots_as_before(self, lowpriv, backends):
        name, schema, password = lowpriv
        self._grant(schema, name)
        backend = PostgresGraphPersistenceBackend(
            _dsn_as_role(name, password), schema=schema
        )
        backends.append(backend)
        backend.save_graph_data(snapshot([node_payload("a")]))

    def test_with_staged_sessions_and_no_tables_it_says_what_to_provision(
        self, lowpriv, backends
    ):
        name, schema, password = lowpriv
        self._grant(schema, name)
        backend = PostgresGraphPersistenceBackend(
            _dsn_as_role(name, password), schema=schema, staged_sessions=True
        )
        backends.append(backend)
        with pytest.raises(RuntimeError, match="provision the layer tables"):
            backend.load_graph_data()

    def test_a_provisioned_table_without_retired_at_says_what_to_add(
        self, lowpriv, backends
    ):
        name, schema, password = lowpriv
        from backend.core.postgres_backend import _LAYER_TABLE_COLUMNS

        old_shape = {
            **_LAYER_TABLE_COLUMNS,
            "graph_staged_sessions": _LAYER_TABLE_COLUMNS[
                "graph_staged_sessions"
            ].replace(", retired_at timestamptz", ""),
        }
        with psycopg.connect(DSN, autocommit=True) as conn:
            for table in LAYER_TABLES:
                conn.execute(
                    psycopg.sql.SQL("CREATE TABLE {}.{} ({})").format(
                        psycopg.sql.Identifier(schema),
                        psycopg.sql.Identifier(table),
                        psycopg.sql.SQL(old_shape[table]),
                    )
                )
        self._grant(schema, name)
        backend = PostgresGraphPersistenceBackend(
            _dsn_as_role(name, password), schema=schema, staged_sessions=True
        )
        backends.append(backend)
        with pytest.raises(RuntimeError, match="no retired_at"):
            backend.load_graph_data()

    def test_with_the_tables_provisioned_it_boots_and_stages(self, lowpriv, backends):
        name, schema, password = lowpriv
        from backend.core.postgres_backend import _LAYER_TABLE_COLUMNS

        with psycopg.connect(DSN, autocommit=True) as conn:
            for table in LAYER_TABLES:
                conn.execute(
                    psycopg.sql.SQL("CREATE TABLE {}.{} ({})").format(
                        psycopg.sql.Identifier(schema),
                        psycopg.sql.Identifier(table),
                        psycopg.sql.SQL(_LAYER_TABLE_COLUMNS[table]),
                    )
                )
        self._grant(schema, name)
        backend = PostgresGraphPersistenceBackend(
            _dsn_as_role(name, password), schema=schema, staged_sessions=True
        )
        backends.append(backend)
        backend.save_graph_data(snapshot([node_payload("a")]))
        backend.record_staged(A)
        backend.stage(A, [entry("node", "a")])
        assert [e["entity_id"] for e in backend.load_layer(A)] == ["a"]


class TestMergeRecordRetention:
    RETENTION = timedelta(days=90)

    @staticmethod
    def _merged(backend, session_id, merge_id, entity="a"):
        backend.stage(session_id, [entry("node", entity, session_id=session_id)])
        backend.merge(session_id, merge_id, [], [], [("node", entity)], {})

    def test_a_store_from_before_retention_gets_the_column_and_a_window(
        self, make, schema
    ):
        backend = make()
        backend.save_graph_data(snapshot([node_payload("a", revision=1)]))
        backend.record_staged(A)
        self._merged(backend, A, "m")
        backend.retire(A)
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                psycopg.sql.SQL(
                    "ALTER TABLE {}.graph_staged_sessions DROP COLUMN retired_at"
                ).format(psycopg.sql.Identifier(schema))
            )
        upgraded = make()
        first = ls.utc_now() + timedelta(days=3650)
        assert upgraded.purge_merge_records(self.RETENTION, limit=10, now=first) == 0
        assert upgraded.load_merge("m") is not None
        with psycopg.connect(DSN) as conn:
            stamped = conn.execute(
                psycopg.sql.SQL(
                    "SELECT retired_at FROM {}.graph_staged_sessions WHERE session_id = %s"
                ).format(psycopg.sql.Identifier(schema)),
                (A,),
            ).fetchone()[0]
        assert stamped == first
        later = first + self.RETENTION + timedelta(seconds=1)
        assert make().purge_merge_records(self.RETENTION, limit=10, now=later) == 1

    def test_a_record_not_marked_applied_is_never_purged(self, make, schema):
        backend = make()
        backend.save_graph_data(snapshot([node_payload("a", revision=1)]))
        backend.retire(A)
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                psycopg.sql.SQL(
                    "INSERT INTO {}.graph_merge_records (merge_id, session_id, doc)"
                    " VALUES ('m-pending', %s, %s)"
                ).format(psycopg.sql.Identifier(schema)),
                (A, psycopg.types.json.Jsonb({"status": ls.MERGE_PENDING})),
            )
        far = ls.utc_now() + timedelta(days=3650)
        assert backend.purge_merge_records(self.RETENTION, limit=10, now=far) == 0
        assert backend.load_merge("m-pending") is not None

    def test_the_window_is_judged_on_the_database_clock(self, make, schema):
        backend = make()
        backend.save_graph_data(snapshot([node_payload("a", revision=1)]))
        backend.record_staged(A)
        self._merged(backend, A, "m")
        backend.retire(A)
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                psycopg.sql.SQL(
                    "UPDATE {}.graph_staged_sessions"
                    " SET retired_at = clock_timestamp() - %s WHERE session_id = %s"
                ).format(psycopg.sql.Identifier(schema)),
                (self.RETENTION - timedelta(minutes=1), A),
            )
        assert backend.purge_merge_records(self.RETENTION, limit=10) == 0
        with psycopg.connect(DSN, autocommit=True) as conn:
            conn.execute(
                psycopg.sql.SQL(
                    "UPDATE {}.graph_staged_sessions"
                    " SET retired_at = clock_timestamp() - %s WHERE session_id = %s"
                ).format(psycopg.sql.Identifier(schema)),
                (self.RETENTION + timedelta(minutes=1), A),
            )
        assert backend.purge_merge_records(self.RETENTION, limit=10) == 1

    def test_two_instances_purging_at_once_delete_each_record_once(self, make):
        backend = make()
        backend.save_graph_data(snapshot([node_payload("a", revision=1)]))
        backend.record_staged(A)
        ids = [f"m-{n}" for n in range(12)]
        for merge_id in ids:
            self._merged(backend, A, merge_id)
        backend.retire(A)
        now = ls.utc_now() + self.RETENTION + timedelta(seconds=5)
        counts, errors = [], []
        start = threading.Barrier(4)

        def purge():
            instance = make()
            start.wait()
            try:
                while True:
                    count = instance.purge_merge_records(
                        self.RETENTION, limit=1, now=now
                    )
                    counts.append(count)
                    if count == 0:
                        return
            except Exception as exc:  # pragma: no cover - the failure reported
                errors.append(exc)

        threads = [threading.Thread(target=purge) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        assert errors == []
        assert sum(counts) == len(ids)
        assert all(backend.load_merge(merge_id) is None for merge_id in ids)

    def test_a_purge_takes_only_its_own_scopes_records(self, make):
        first, second = make(scope="scope-a"), make(scope="scope-b")
        first.save_graph_data(snapshot([node_payload("a", revision=1)]))
        first.record_staged(A)
        self._merged(first, A, "m-a")
        first.retire(A)
        far = ls.utc_now() + timedelta(days=3650)
        assert second.purge_merge_records(self.RETENTION, limit=10, now=far) == 0
        assert first.load_merge("m-a") is not None
        assert first.purge_merge_records(self.RETENTION, limit=10, now=far) == 1
