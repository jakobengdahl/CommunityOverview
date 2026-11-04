"""PostgreSQL revision orchestration that needs no running server."""

# covers: backend/core/postgres_backend.py

from unittest.mock import MagicMock, Mock

import pytest

pytest.importorskip("psycopg", reason="psycopg is an optional dependency")
pytest.importorskip("psycopg_pool", reason="psycopg_pool is an optional dependency")

from psycopg_pool import PoolTimeout  # noqa: E402

from backend.core.postgres_backend import (  # noqa: E402
    EVIDENCE_CONNECT_TIMEOUT_SECONDS,
    SAVE_LOCK_KEY,
    PostgresGraphPersistenceBackend,
)
from backend.core.storage_backends import (  # noqa: E402
    EntityOperation,
    EntityRevisionConflict,
    GraphGenerationMoved,
)


@pytest.fixture
def backend():
    backend = object.__new__(PostgresGraphPersistenceBackend)
    backend.schema = "revision_tests"
    backend._migrated = True
    backend._scope = None
    backend._scope_column = False
    return backend


def test_evidence_uses_a_bounded_connection_wait_and_surfaces_pool_timeout(backend):
    backend._pool = Mock()
    backend._pool.connection.side_effect = PoolTimeout("pool unavailable")

    with pytest.raises(PoolTimeout):
        backend.read_write_evidence([("node", "a")])

    assert 0 < EVIDENCE_CONNECT_TIMEOUT_SECONDS <= 5
    backend._pool.connection.assert_called_once_with(
        timeout=EVIDENCE_CONNECT_TIMEOUT_SECONDS
    )


def test_endpoint_locks_are_sorted_deduplicated_and_exclude_upserted_nodes(backend):
    # Record each requested lock directly: a live server's deadlock retries
    # can otherwise conceal a batch acquiring these in inconsistent order.
    endpoints = "zyxwvutsrqponmlkjihgfedcba"
    operations = [EntityOperation("node", "upsert", "z", {"id": "z"})]
    operations.extend(
        EntityOperation(
            "edge", "upsert", f"e{index}", {"source": source, "target": target}
        )
        for index, (source, target) in enumerate(zip(endpoints, endpoints[1:]))
    )
    conn = MagicMock()
    backend._hold_endpoints(conn, operations)

    assert [call.args[1] for call in conn.execute.call_args_list] == [
        (endpoint,) for endpoint in "abcdefghijklmnopqrstuvwxy"
    ]


@pytest.mark.parametrize("incident", [None, "source", "target"])
@pytest.mark.parametrize(
    "scope_column,scope", [(False, None), (True, None), (True, "workspace")]
)
def test_node_create_guard_only_reads_and_refuses_dangling_edges(
    backend, incident, scope_column, scope
):
    backend._scope = scope
    backend._scope_column = scope_column
    conn = MagicMock()
    conn.execute.return_value.fetchone.side_effect = (
        [(1,)] if incident == "source" else [None, (1,) if incident else None]
    )
    operation = EntityOperation("node", "upsert", "n", {}, expect_absent=True)
    conflict = EntityRevisionConflict("node", "n", None, None)
    backend._refuse = Mock(return_value=conflict)

    if incident:
        with pytest.raises(EntityRevisionConflict) as refused:
            backend._refuse_dangling_edges_on_create(conn, [operation], [])
        assert "dangling edges reference this id" in str(refused.value)
    else:
        backend._refuse_dangling_edges_on_create(conn, [operation], [])
        backend._refuse.assert_not_called()

    calls = conn.execute.call_args_list
    assert len(calls) == (1 if incident == "source" else 2)
    for call, endpoint in zip(calls, ("source", "target")):
        query, params = call.args
        text = query.as_string()
        assert "SELECT 1" in text
        assert "DELETE" not in text
        assert f"doc->>'{endpoint}' = %s" in text
        other = "target" if endpoint == "source" else "source"
        assert f"doc->>'{other}'" not in text
        assert params == (("n", scope, []) if scope_column else ("n", []))
        if scope_column:
            assert '"scope_id" IS NULL' in text
            assert '"scope_id" = %s' in text
        else:
            assert "scope_id" not in text


@pytest.mark.parametrize("scoped", [False, True])
def test_a_refused_node_upsert_never_reports_the_stored_type(backend, scoped):
    from backend.core.storage_backends import NodeTypeChanged

    backend._scope_column = scoped
    backend._scope = "test" if scoped else None
    conn = MagicMock()
    conn.execute.return_value.fetchone.side_effect = [None, ("HiddenStoredType",)]
    operation = EntityOperation(
        "node", "upsert", "a", {"id": "a", "type": "Actor", "name": "Edited"}
    )
    with pytest.raises(NodeTypeChanged) as refused:
        backend._apply_one(conn, operation, "write")
    assert "HiddenStoredType" not in str(refused.value)
    assert refused.value.current_revision is None


@pytest.mark.parametrize("expect_absent", [False, True])
@pytest.mark.parametrize("inserted", [False, True])
def test_only_inserted_node_upserts_are_checked_for_dangling_edges(
    backend, expect_absent, inserted
):
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = (42, inserted)
    created = []
    operation = EntityOperation(
        "node",
        "upsert",
        "n",
        {"id": "n", "type": "Actor"},
        expect_absent=expect_absent,
    )
    assert backend._apply_one(conn, operation, None, created) == 42
    assert created == ([operation] if inserted else [])
    assert "(xmax = 0)" in conn.execute.call_args.args[0].as_string()


def test_create_guard_accounts_for_edges_written_in_the_batch(backend):
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = None
    node = EntityOperation("node", "upsert", "n", {}, expect_absent=True)
    operations = [
        node,
        EntityOperation("edge", "upsert", "new", {"source": "n", "target": "n"}),
        EntityOperation.delete_edge("old"),
    ]
    backend._refuse_dangling_edges_on_create(conn, [node], operations)
    for call in conn.execute.call_args_list:
        assert call.args[1] == ("n", ["new"])
        assert "id = ANY(%s::text[])" in call.args[0].as_string()


@pytest.mark.parametrize("stored", [None, ("Actor", []), ("Actor", ["Agency"])])
def test_edge_endpoint_subtypes_are_checked_under_key_share(backend, stored):
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = stored
    op = EntityOperation(
        "edge",
        "upsert",
        "e",
        {"source": "a", "target": "a"},
        endpoint_subtypes=(("a", ("Agency",)),),
    )
    if stored == ("Actor", ["Agency"]):
        backend._hold_endpoints(conn, [op])
    else:
        with pytest.raises(EntityRevisionConflict):
            backend._hold_endpoints(conn, [op])
    assert "FOR KEY SHARE" in str(conn.execute.call_args.args[0])


@pytest.mark.parametrize("rows", [[("new-edge", 5)], [("kept", 6)]])
def test_direct_incident_guard_refuses_added_or_changed_edges_before_writing(
    backend, rows
):
    backend._pool = MagicMock()
    conn = backend._pool.connection.return_value.__enter__.return_value
    conn.execute.return_value.fetchall.return_value = rows
    backend._bind_scope = Mock()
    backend._claim_or_check_graph_identity = Mock()
    backend._apply_one = Mock()
    backend._announce = Mock()
    op = EntityOperation(
        "node",
        "upsert",
        "a",
        {"id": "a", "type": "Actor", "subtypes": []},
        incident_edge_revisions=(("kept", 5),),
    )
    with pytest.raises(EntityRevisionConflict) as refused:
        backend._apply_batch_once([op], "token")
    backend._apply_one.assert_not_called()
    backend._announce.assert_not_called()
    assert "new-edge" not in str(refused.value)
    assert "kept" not in str(refused.value)
    assert any("FOR UPDATE" in str(c.args[0]) for c in conn.execute.call_args_list)


def _reading(rows_per_statement):
    """A connection answering each statement with the next list of rows."""
    conn = Mock()
    conn.execute.side_effect = [
        Mock(fetchall=Mock(return_value=rows), fetchone=Mock(return_value=rows))
        for rows in rows_per_statement
    ]
    return conn


def _conditions(**fields):
    return [EntityOperation("node", "upsert", "w", {"id": "w"}, **fields)]


@pytest.mark.parametrize("stored", [[("kept", 8)], []], ids=["moved", "gone"])
def test_a_read_that_moved_or_went_refuses_the_batch(backend, stored):
    with pytest.raises(EntityRevisionConflict) as refused:
        backend._verify_batch_conditions(
            _reading([stored]), _conditions(read_revisions=(("node", "kept", 7),))
        )
    assert refused.value.entity_id == "kept"
    assert refused.value.expected_revision == 7


def test_a_read_absent_that_now_exists_refuses_the_batch(backend):
    with pytest.raises(EntityRevisionConflict) as refused:
        backend._verify_batch_conditions(
            _reading([[("late", 3)]]),
            _conditions(read_revisions=(("edge", "late", None),)),
        )
    assert refused.value.kind == "edge"


def test_reads_that_hold_pass(backend):
    backend._verify_batch_conditions(
        _reading([[("a", 1)], [("e", 0)]]),
        _conditions(
            read_revisions=(("node", "a", 1), ("node", "b", None), ("edge", "e", 0))
        ),
    )


@pytest.mark.parametrize("row", [("3",), None], ids=["moved", "never-replaced"])
def test_a_stored_generation_other_than_the_expected_refuses_the_batch(backend, row):
    with pytest.raises(GraphGenerationMoved):
        backend._verify_batch_conditions(
            _reading([row]), _conditions(expected_generation=2)
        )


def test_a_never_replaced_store_is_at_generation_zero(backend):
    backend._verify_batch_conditions(
        _reading([None]), _conditions(expected_generation=0)
    )


def test_a_reserved_generation_is_drawn_above_the_stored_one_under_the_save_lock(
    backend,
):
    backend._sequence_regclass = lambda: "seq"
    conn = MagicMock()
    conn.execute.return_value.fetchone.side_effect = [("41",), (42,)]
    backend._pool = MagicMock()
    backend._pool.connection.return_value.__enter__.return_value = conn

    assert backend.reserve_generation() == 42

    params = [
        call.args[1] for call in conn.execute.call_args_list if len(call.args) > 1
    ]
    assert params == [
        (SAVE_LOCK_KEY, backend.schema),
        ("seq", 41),
        ("seq",),
    ]
