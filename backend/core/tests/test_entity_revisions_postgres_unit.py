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
from backend.core.storage_backends import EntityOperation  # noqa: E402


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
