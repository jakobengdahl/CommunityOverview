"""PostgreSQL revision orchestration that needs no running server."""

from unittest.mock import MagicMock, Mock

import pytest

pytest.importorskip("psycopg", reason="psycopg is an optional dependency")
pytest.importorskip("psycopg_pool", reason="psycopg_pool is an optional dependency")

from psycopg_pool import PoolTimeout  # noqa: E402

from backend.core.postgres_backend import (  # noqa: E402
    EVIDENCE_CONNECT_TIMEOUT_SECONDS,
    PostgresGraphPersistenceBackend,
)
from backend.core.storage_backends import (  # noqa: E402
    EntityOperation,
    EntityRevisionConflict,
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
