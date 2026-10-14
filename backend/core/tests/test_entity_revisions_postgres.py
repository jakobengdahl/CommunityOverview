"""Entity revisions in a shared store (docs/SESSION_OVERLAY_CONTRACT.md §4.4).

PostgreSQL assigns every revision from one sequence, checks each expectation
in the statement that writes, and every write waits for it. A write whose
acknowledgement is lost is decided on its own write token, never on content
read back.

Needs a real server: the properties asserted - a sequence shared by every
instance, a conditional UPDATE refusing, a row lock ordering two writers -
are the server's. Skips without CO_TEST_POSTGRES_DSN, like the backend's own
tests.
"""

from __future__ import annotations

import os
import threading
import uuid
from typing import Any, List

import pytest

from backend.core.models import Edge, Node, NodeType

REQUIRE = os.environ.get("CO_REQUIRE_POSTGRES", "").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)

if REQUIRE:
    import psycopg  # noqa: F401  (a skip here would be the failure, not a pass)
else:
    psycopg = pytest.importorskip("psycopg", reason="psycopg is an optional dependency")

from backend.core.postgres_backend import (  # noqa: E402  (after importorskip)
    WRITE_TOKEN_KEY,
    PostgresGraphPersistenceBackend,
)
from backend.core.storage import GraphStorage  # noqa: E402
from backend.core.storage_backends import (  # noqa: E402
    EntityRevisionConflict,
    GraphWriteFailed,
    GraphWriteOutcomeUnknown,
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
                f"CO_REQUIRE_POSTGRES=1 but the server is unreachable: {exc}"
            ) from exc
        return False


pytestmark = pytest.mark.skipif(
    not _server_reachable(), reason="set CO_TEST_POSTGRES_DSN to run these"
)


class _Deaf(PostgresGraphPersistenceBackend):
    """An instance that is never told about the other's writes, so its model
    stays behind the store for as long as a test needs it to."""

    def start_change_notification(self, listener) -> None:  # noqa: D401
        return None

    def stop_change_notification(self) -> None:
        return None


@pytest.fixture
def schema():
    name = f"co_rev_{uuid.uuid4().hex[:16]}"
    yield name
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')


@pytest.fixture
def instances(schema):
    built = []

    def start(deaf: bool = False) -> GraphStorage:
        backend = (_Deaf if deaf else PostgresGraphPersistenceBackend)(
            DSN, schema=schema
        )
        storage = GraphStorage(persistence_backend=backend)
        storage.WRITE_RESOLVE_DELAY_SECONDS = 0.0
        built.append((storage, backend))
        return storage

    yield start
    for storage, backend in built:
        try:
            storage.shutdown_events()
        finally:
            backend.close()


def _node(node_id: str, name: str = "") -> Node:
    return Node(id=node_id, type=NodeType.ACTOR, name=name or node_id)


def _stored(schema: str, table: str, entity_id: str) -> Any:
    with psycopg.connect(DSN) as conn:
        row = conn.execute(
            f'SELECT doc FROM "{schema}".{table} WHERE id = %s', (entity_id,)
        ).fetchone()
    return None if row is None else row[0]


def _events(storage: GraphStorage) -> List[Any]:
    seen: List[Any] = []
    storage.add_system_listener(seen.append)
    return seen


def test_the_store_assigns_revisions_unique_across_instances(instances, schema):
    a = instances()
    b = instances()
    a.add_nodes([_node("a1"), _node("a2")], [])
    b.add_nodes([_node("b1")], [Edge(id="e", source="b1", target="b1")])
    a.update_node("a1", {"name": "x"})

    held = [a.nodes["a1"].revision, a.nodes["a2"].revision]
    held += [b.nodes["b1"].revision, b.edges["e"].revision]
    assert len(set(held)) == len(held)
    assert min(held) >= 1
    # What memory holds is what the store wrote, not what the instance sent.
    assert _stored(schema, "graph_nodes", "a1")["revision"] == a.nodes["a1"].revision
    assert _stored(schema, "graph_edges", "e")["revision"] == b.edges["e"].revision


def test_the_store_refuses_a_stale_expectation_the_instance_could_not_see(
    instances, schema
):
    a = instances()
    behind = instances(deaf=True)
    a.add_nodes([_node("n")], [])
    behind.load()
    seen = behind.nodes["n"].revision
    a.update_node("n", {"name": "moved on"})
    events = _events(behind)

    # `behind` still holds `seen`, so its own check passes; the store's fails.
    with pytest.raises(EntityRevisionConflict) as refused:
        behind.update_node("n", {"name": "stale"}, expected_revision=seen)
    assert refused.value.current_revision == a.nodes["n"].revision
    assert behind.nodes["n"].name == "n"
    assert behind.nodes["n"].revision == seen
    assert events == []
    assert _stored(schema, "graph_nodes", "n")["name"] == "moved on"

    with pytest.raises(EntityRevisionConflict):
        behind.delete_nodes(["n"], confirmed=True, expected_revisions={"n": seen})
    assert "n" in behind.nodes
    assert _stored(schema, "graph_nodes", "n") is not None


def test_a_create_expecting_absence_is_refused_when_another_instance_has_it(
    instances, schema
):
    a = instances()
    behind = instances(deaf=True)
    a.add_nodes([_node("dup", "first")], [])

    with pytest.raises(EntityRevisionConflict):
        behind.add_nodes([_node("dup", "second")], [], expect_absent=True)
    assert "dup" not in behind.nodes
    assert _stored(schema, "graph_nodes", "dup")["name"] == "first"


def test_the_write_token_never_leaves_the_store(instances, schema):
    a = instances()
    b = instances(deaf=True)
    a.add_nodes([_node("n")], [Edge(id="e", source="n", target="n")])
    assert WRITE_TOKEN_KEY in _stored(schema, "graph_nodes", "n")
    assert WRITE_TOKEN_KEY in _stored(schema, "graph_edges", "e")
    loaded = b._persistence_backend.load_graph_data()
    assert all(WRITE_TOKEN_KEY not in doc for doc in loaded["nodes"])
    assert all(WRITE_TOKEN_KEY not in doc for doc in loaded["edges"])
    # What a peer is told to apply is read back the same way.
    resolved = b._persistence_backend._resolve([("node", "n"), ("edge", "e")])
    assert all(WRITE_TOKEN_KEY not in op.payload for op in resolved)
    b.load()
    assert WRITE_TOKEN_KEY not in b.nodes["n"].to_dict()
    assert WRITE_TOKEN_KEY not in b.nodes["n"].metadata


def test_a_write_whose_acknowledgement_was_lost_completes(instances, schema):
    storage = instances()
    storage.add_nodes([_node("n")], [])
    real = storage._persistence_backend.apply_batch

    def lands_then_drops(operations, write_token=None):
        real(operations, write_token=write_token)
        raise psycopg.OperationalError("connection lost after commit")

    storage._persistence_backend.apply_batch = lands_then_drops
    events = _events(storage)
    storage.update_node("n", {"name": "landed"})

    assert storage.nodes["n"].name == "landed"
    assert (
        storage.nodes["n"].revision == _stored(schema, "graph_nodes", "n")["revision"]
    )
    assert len(events) == 1


def test_a_write_that_did_not_land_is_undone_without_an_event(instances, schema):
    storage = instances()
    storage.add_nodes([_node("n"), _node("m")], [Edge(id="e", source="n", target="m")])
    before = storage.nodes["n"].to_dict()

    def never_lands(operations, write_token=None):
        raise psycopg.OperationalError("connection refused")

    storage._persistence_backend.apply_batch = never_lands
    events = _events(storage)

    with pytest.raises(GraphWriteFailed):
        storage.update_node("n", {"name": "lost"})
    with pytest.raises(GraphWriteFailed):
        storage.add_nodes([_node("new")], [])
    assert storage.nodes["n"].to_dict() == before
    assert "new" not in storage.nodes
    assert events == []


def test_a_delete_that_failed_is_never_confirmed_and_memory_follows_the_store(
    instances, schema
):
    storage = instances()
    storage.add_nodes([_node("n")], [])
    real = storage._persistence_backend.apply_batch

    def lands_then_drops(operations, write_token=None):
        real(operations, write_token=write_token)
        raise psycopg.OperationalError("connection lost after commit")

    storage._persistence_backend.apply_batch = lands_then_drops
    events = _events(storage)
    # A delete leaves no row to carry its token, so its landing cannot be
    # confirmed - only ruled out. Here it did land: unknown, and re-read.
    with pytest.raises(GraphWriteOutcomeUnknown):
        storage.delete_nodes(["n"], confirmed=True)
    assert "n" not in storage.nodes
    assert events == []


def test_an_unreadable_store_after_a_failure_is_an_unknown_outcome(instances):
    storage = instances()
    storage.add_nodes([_node("n")], [])

    def never_lands(operations, write_token=None):
        raise psycopg.OperationalError("connection refused")

    def unreadable(keys):
        raise psycopg.OperationalError("still down")

    storage._persistence_backend.apply_batch = never_lands
    storage._persistence_backend.read_write_evidence = unreadable
    with pytest.raises(GraphWriteOutcomeUnknown):
        storage.update_node("n", {"name": "?"})
    # Re-read from the store, which never took the write.
    assert storage.nodes["n"].name == "n"


def test_a_whole_graph_save_raises_the_counter_past_what_it_wrote(instances, schema):
    storage = instances()
    storage.add_nodes([_node("n")], [])
    storage.nodes["n"].revision = 500
    storage.save().result()
    storage.add_nodes([_node("after")], [])
    assert storage.nodes["after"].revision > 500


def test_an_edge_write_holds_its_endpoints_against_a_node_delete(instances, schema):
    storage = instances()
    storage.add_nodes([_node("s"), _node("t")], [])
    finished = threading.Event()

    with psycopg.connect(DSN) as blocker:
        # Another writer holds the endpoint row exclusively, as a node
        # delete does until it commits.
        blocker.execute(
            f'SELECT 1 FROM "{schema}".graph_nodes WHERE id = %s FOR UPDATE', ("t",)
        )

        def write_edge():
            storage.add_edge(Edge(id="e", source="s", target="t"))
            finished.set()

        writer = threading.Thread(target=write_edge)
        writer.start()
        assert not finished.wait(1.0), "the edge write did not wait for its endpoint"
        blocker.rollback()
    writer.join(10)
    assert finished.is_set()
    assert _stored(schema, "graph_edges", "e") is not None


def test_a_write_the_store_refuses_fails_at_once_without_reading_back(instances):
    storage = instances()
    storage.add_nodes([_node("n")], [])
    storage.WRITE_RESOLVE_DELAY_SECONDS = 30.0  # would show as a stall

    def never_asked(keys):
        raise AssertionError("a refused write read its evidence back")

    storage._persistence_backend.read_write_evidence = never_asked
    # jsonb cannot hold NUL: refused inside the transaction, nothing landed.
    with pytest.raises(GraphWriteFailed) as failed:
        storage.update_node("n", {"description": "bad\u0000value"})
    assert "\u0000" not in str(failed.value)
    assert storage.nodes["n"].description == ""
    assert not storage._resync_pending


def test_a_whole_graph_save_never_lowers_the_counter(instances):
    storage = instances()
    storage.add_nodes([_node("a"), _node("b")], [])
    storage.update_node("a", {"name": "moved"})
    highest = storage.nodes["a"].revision
    # A save carrying revisions below what the sequence has handed out.
    storage.nodes["a"].revision = 1
    storage.nodes["b"].revision = 1
    storage.save().result()
    storage.add_nodes([_node("after")], [])
    assert storage.nodes["after"].revision > highest


def test_a_write_still_landing_at_the_first_read_is_found_at_the_second(instances):
    storage = instances()
    storage.add_nodes([_node("n")], [])
    backend = storage._persistence_backend
    real_apply = backend.apply_batch
    real_read = backend.read_write_evidence
    held: List[Any] = []

    def lands_late(operations, write_token=None):
        held.append((operations, write_token))
        raise psycopg.OperationalError("connection lost while committing")

    reads: List[Any] = []

    def evidence(keys):
        if reads:
            # The commit that was still flushing lands between the two reads.
            operations, token = held[0]
            real_apply(operations, write_token=token)
        reads.append(keys)
        return real_read(keys)

    backend.apply_batch = lands_late
    backend.read_write_evidence = evidence
    storage.update_node("n", {"name": "late"})

    assert len(reads) == 2
    assert storage.nodes["n"].name == "late"
    assert (
        storage.nodes["n"].revision
        == _stored(schema_of(storage), "graph_nodes", "n")["revision"]
    )
    assert not storage._resync_pending


def schema_of(storage: GraphStorage) -> str:
    return storage._persistence_backend.schema


def test_an_import_does_not_carry_the_documents_revisions_into_the_store(instances):
    from backend.core.models import MAX_REVISION

    storage = instances()
    storage.add_nodes([_node("old")], [])
    storage.replace_all_nodes_and_edges(
        [Node(id="n", type=NodeType.ACTOR, name="n", revision=MAX_REVISION)], []
    )
    assert storage.nodes["n"].revision == 0
    storage.add_nodes([_node("after")], [])
    assert 0 < storage.nodes["after"].revision < 1_000_000


def test_a_cross_scope_create_is_refused_at_once_without_reading_back(schema):
    owner = PostgresGraphPersistenceBackend(DSN, schema=schema, scope="scope-a")
    other = PostgresGraphPersistenceBackend(DSN, schema=schema, scope="scope-b")
    storage = None
    try:
        owner.save_graph_data({"nodes": [], "edges": [], "metadata": {}})
        owner.upsert_node(_node("taken").to_dict())
        storage = GraphStorage(persistence_backend=other)
        storage.WRITE_RESOLVE_DELAY_SECONDS = 30.0

        def never_asked(keys):
            raise AssertionError("a refused write read its evidence back")

        other.read_write_evidence = never_asked
        with pytest.raises(GraphWriteFailed) as failed:
            storage.add_nodes([_node("taken")], [])
        assert "scope" not in str(failed.value)
        assert "taken" not in storage.nodes
    finally:
        if storage is not None:
            storage.shutdown_events()
        owner.close()
        other.close()
