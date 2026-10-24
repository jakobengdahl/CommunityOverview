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


@pytest.mark.parametrize("incident", [False, True])
def test_node_delete_guard_only_reads_and_refuses_remaining_edges(backend, incident):
    conn = MagicMock()
    conn.execute.return_value.fetchone.return_value = (1,) if incident else None
    operation = EntityOperation.delete_node("n")
    conflict = EntityRevisionConflict("node", "n", None, None)
    backend._refuse = Mock(return_value=conflict)

    if incident:
        with pytest.raises(EntityRevisionConflict) as refused:
            backend._refuse_remaining_incident_edges(conn, [operation])
        assert refused.value is conflict
        backend._refuse.assert_called_once_with(conn, "graph_nodes", operation)
    else:
        backend._refuse_remaining_incident_edges(conn, [operation])
        backend._refuse.assert_not_called()

    query, params = conn.execute.call_args.args
    assert "SELECT 1" in str(query)
    assert "DELETE" not in str(query)
    assert params == ("n", "n")
    conn.execute.assert_called_once()


def test_batch_checks_incident_edges_after_named_writes_before_announcing(backend):
    backend._pool = MagicMock()
    backend._bind_scope = Mock()
    backend._claim_or_check_graph_identity = Mock()
    backend._hold_endpoints = Mock()
    steps = []
    backend._apply_one = Mock(side_effect=lambda *args: steps.append("write"))
    backend._refuse_remaining_incident_edges = Mock(
        side_effect=lambda *args: steps.append("check")
    )
    backend._announce = Mock(side_effect=lambda *args: steps.append("announce"))
    backend._apply_batch_once(
        [EntityOperation.delete_node("n"), EntityOperation.delete_edge("e")], None
    )
    assert steps == ["write", "write", "check", "announce"]
