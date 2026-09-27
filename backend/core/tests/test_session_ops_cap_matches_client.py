"""The browser sync client's op-batch cap must fit the server's default budget.

``sessionSyncClient.js`` chunks its outbound queue at ``MAX_OPS_PER_BATCH``
ops per ``POST /ops``. The server refuses a batch costing more than a whole
rate bucket as ``OpBatchTooLarge`` (413), which backing off never cures, and
the client then degrades to one-op-at-a-time sends. The two values live in
different languages, so this reads the client's literal and sends a full
client-sized batch from a fresh client through a default ``SessionManager``.
"""

import re
from pathlib import Path

import pytest

from backend.core.session_manager import SessionManager
from backend.core.session_store import InMemorySessionPersistenceBackend, SessionStore

pytestmark = pytest.mark.asyncio

CLIENT_SOURCE = (
    Path(__file__).resolve().parents[3]
    / "frontend"
    / "web"
    / "src"
    / "services"
    / "sessionSyncClient.js"
)


def _client_max_ops_per_batch() -> int:
    matches = re.findall(
        r"^const MAX_OPS_PER_BATCH = (\d+);$",
        CLIENT_SOURCE.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert len(matches) == 1, (
        f"expected one `const MAX_OPS_PER_BATCH = <int>;` in {CLIENT_SOURCE}, "
        f"found {len(matches)}; update this test's pattern if the constant moved"
    )
    return int(matches[0])


async def test_a_full_client_batch_is_admitted_by_a_default_session_manager():
    client_cap = _client_max_ops_per_batch()
    mgr = SessionManager(SessionStore(InMemorySessionPersistenceBackend()))
    s = mgr.create_session()
    ops = [{"op": "nodes_added", "node_ids": [f"n{i}"]} for i in range(client_cap)]

    res = await mgr.apply_ops(s.id, "fresh-client", 0, ops)

    assert res["seq"] == client_cap
