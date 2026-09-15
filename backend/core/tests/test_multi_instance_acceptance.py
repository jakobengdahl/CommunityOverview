"""The four things that must hold before a deployment runs more than one instance.

This is the close-out of the multi-instance work, stated as tests rather than
as an argument. A production deployment was pinned to exactly one instance
in 2026 because a second instance both answered 404 to MCP sessions it did
not create and silently overwrote the first one's writes. Lifting that pin
needs evidence, so each criterion below is either proved here or proved
somewhere this file names.

1. Concurrent writes from two instances lose no updates.
   Proved in `test_multi_instance_postgres.py`, not duplicated here: that
   module runs two GraphStorage instances on one store through the entity
   path and asserts content, and it is where the contested-entity divergence
   was found and fixed.

2. An MCP session survives being served by a different instance.
   Proved here, and it is the criterion with a CONDITION rather than a yes:
   the open core's session store is file-backed, so this holds exactly when
   the instances share the directory and fails when they do not.

3. A change written by one instance becomes visible to the others within a
   stated bound. Proved here, with the bound measured rather than asserted
   loosely - and only meaningful since the boot window was closed, because an
   entity written during another instance's startup used to be reported to
   nobody and no bound covered it. The boot gate itself is NOT exercised
   here: `test_boot_gate.py` holds it, and `test_persistence_contract_file.py`
   holds the other half of criterion 5 below.

4. Restart does not lose acknowledged writes. Proved here.

5. A file-backed installation stays single-instance. Not proved here either:
   `test_persistence_contract_file.py` asserts that the file backend declares
   no change notification and that a storage on it never starts any.

What a green run here therefore does NOT establish: criterion 1, the boot
gate, the file backend's capabilities, or that the server wires the session
directory it resolves (`backend/api_host/tests/test_session_api.py` covers
that last one). Each is held somewhere named above. This matters because the
"four criteria" framing invites the opposite reading - that a green run here
is the whole evidence - and it is not.
"""

import os
import time
import uuid

import pytest

from backend.core.models import Edge, Node, NodeType
from backend.core.session_store import FileSessionPersistenceBackend, SessionStore
from backend.core.storage import GraphStorage

# Imported softly rather than with a module-level `importorskip`. The session
# criterion below needs no database, and it is the one that encodes the
# production failure this suite exists for - a module-level skip would make it
# vanish on a clone without the optional PostgreSQL extra, with no signal that
# the criterion went unchecked.
try:
    import psycopg
except ImportError:  # pragma: no cover - depends on which extras are installed
    psycopg = None

DSN = os.environ.get("CO_TEST_POSTGRES_DSN", "")
REQUIRE = os.environ.get("CO_REQUIRE_POSTGRES") == "1"


def _server_reachable() -> bool:
    if psycopg is None or not DSN:
        return False
    try:
        with psycopg.connect(DSN, connect_timeout=3):
            return True
    except Exception:
        return False


needs_server = pytest.mark.skipif(
    not _server_reachable() and not REQUIRE,
    reason=(
        "install the PostgreSQL extra and set CO_TEST_POSTGRES_DSN to a "
        "reachable server"
    ),
)

# What "within a stated bound" is allowed to mean. Generous on purpose: this
# is an acceptance ceiling for a shared CI runner, not the latency anyone
# should quote. The test reports what it actually measured, and that number -
# not this constant - is what belongs in the capacity envelope.
VISIBILITY_CEILING_S = 10.0


@pytest.fixture
def schema():
    name = f"co_acc_{uuid.uuid4().hex[:16]}"
    yield name
    with psycopg.connect(DSN, autocommit=True) as conn:
        conn.execute(f'DROP SCHEMA IF EXISTS "{name}" CASCADE')


@pytest.fixture
def instances(schema):
    """Instances on one store, torn down after every assertion has run.

    Teardown is the fixture's rather than each test's because
    `shutdown_events()` is not neutral on a shared store: it heals a failed
    write by re-issuing this instance's whole graph, which would overwrite
    what the other one committed.
    """
    from backend.core.postgres_backend import PostgresGraphPersistenceBackend

    built = []

    def start():
        backend = PostgresGraphPersistenceBackend(DSN, schema=schema)
        storage = GraphStorage(persistence_backend=backend)
        built.append((storage, backend))
        return storage

    yield start
    for storage, backend in built:
        try:
            storage.shutdown_events()
        finally:
            backend.close()


def _node(node_id: str, name: str | None = None) -> Node:
    return Node(id=node_id, type=NodeType.ACTOR, name=name or node_id)


def _wait_until_visible(
    storage, node_id: str, timeout: float, started: float | None = None
) -> float:
    """Seconds until `node_id` appears, or fail. Polls rather than sleeps once.

    A fixed sleep would either pass by being longer than the real latency -
    measuring the sleep, not the system - or flake. Polling reports the
    latency itself, which is the number this criterion is about.

    `started` is the moment the write was committed, and callers that want a
    figure worth quoting must pass it. Defaulting to "now" measures only what
    is left after the commit returns, which for a large enough write is
    nothing: propagation finishes while `flush()` is still running and the
    reported latency is 0 ms - not because the system is instant but because
    the clock started after the thing it was timing.
    """
    started = time.perf_counter() if started is None else started
    deadline = started + timeout
    while time.perf_counter() < deadline:
        if storage.nodes.get(node_id) is not None:
            return time.perf_counter() - started
        time.sleep(0.01)
    raise AssertionError(
        f"{node_id} never became visible to the second instance within "
        f"{timeout}s - no bound covers this change, so the pinning to one "
        f"instance cannot be lifted on this evidence"
    )


@needs_server
class TestCriterion3ChangesBecomeVisible:
    def test_a_write_on_one_instance_reaches_the_other_within_the_bound(
        self, instances, capsys
    ):
        writer = instances()
        reader = instances()
        node_id = f"visible-{uuid.uuid4().hex[:8]}"

        # A node AND an edge. Criterion 3 says "a change", not "a node
        # change": with only a node here, the edge half of the report could be
        # dropped entirely and this suite would stay silent.
        other_id = f"{node_id}-peer"
        edge_id = f"edge-{uuid.uuid4().hex[:8]}"
        writer.add_nodes(
            [_node(node_id, "Beacon"), _node(other_id)],
            [Edge(id=edge_id, source=node_id, target=other_id)],
        )
        committed = time.perf_counter()
        writer.flush()

        elapsed = _wait_until_visible(
            reader, node_id, VISIBILITY_CEILING_S, started=committed
        )

        assert reader.nodes[node_id].name == "Beacon", (
            "the node arrived but not its content, so the report named an "
            "entity without carrying what the store holds"
        )

        deadline = time.perf_counter() + VISIBILITY_CEILING_S
        while reader.edges.get(edge_id) is None:
            assert time.perf_counter() < deadline, (
                f"the node became visible but {edge_id} did not, so an "
                f"edge write is announced to nobody and no bound covers it - "
                f"criterion 3 would hold for nodes only"
            )
            time.sleep(0.01)
        with capsys.disabled():
            print(f"\n  measured visibility latency: {elapsed * 1000:.0f} ms")

    def test_the_second_instance_sees_a_delete_too(self, instances):
        """A delete carries no payload, so it is the case a content-only
        refresh would silently skip - leaving the reader serving a node the
        store no longer has."""
        writer = instances()
        reader = instances()
        node_id = f"doomed-{uuid.uuid4().hex[:8]}"

        writer.add_nodes([_node(node_id)], [])
        writer.flush()
        _wait_until_visible(reader, node_id, VISIBILITY_CEILING_S)

        # `confirmed=True` is not ceremony: delete_nodes is a no-op without
        # it, so a test that omits it asserts against its own mistake rather
        # than against the system.
        writer.delete_nodes([node_id], confirmed=True)
        writer.flush()

        deadline = time.perf_counter() + VISIBILITY_CEILING_S
        while time.perf_counter() < deadline:
            if reader.nodes.get(node_id) is None:
                return
            time.sleep(0.01)
        raise AssertionError(
            f"{node_id} was deleted on one instance and is still served by the other"
        )


@needs_server
class TestCriterion4RestartKeepsAcknowledgedWrites:
    def test_a_flushed_write_survives_shutdown_and_a_fresh_instance(self, instances):
        """`flush()` is the acknowledgement: it returns once the write has
        landed in the store, so anything it covered must outlive the process
        that made it. A rollout replaces instances one at a time, and a write
        acknowledged just before an instance goes away has no second chance."""
        first = instances()
        node_id = f"durable-{uuid.uuid4().hex[:8]}"

        first.add_nodes([_node(node_id, "Survivor")], [])
        first.flush()
        first.shutdown_events()

        second = instances()

        assert second.nodes.get(node_id) is not None, (
            "a write acknowledged by flush() did not survive the instance "
            "that made it - a rollout would lose it"
        )
        assert second.nodes[node_id].name == "Survivor"

    def test_a_flushed_write_is_in_the_store_before_the_instance_stops(
        self, instances, schema
    ):
        """The rollout case: the instance is killed, not stopped politely.

        The test above shuts the writer down between the write and the read,
        and `shutdown_events()` drains the executor, heals a failed write and
        checkpoints on its own - so it passes even if `flush()` is a complete
        no-op, which is not the claim its docstring makes. Here the store is
        read through a fresh backend while the writer is still running, so
        `flush()` is the only thing that can have put the node there.
        """
        from backend.core.postgres_backend import PostgresGraphPersistenceBackend

        first = instances()
        node_id = f"acked-{uuid.uuid4().hex[:8]}"

        first.add_nodes([_node(node_id, "Acknowledged")], [])
        first.flush()

        reader = PostgresGraphPersistenceBackend(DSN, schema=schema)
        try:
            stored = reader.load_graph_data()
        finally:
            reader.close()

        assert any(n["id"] == node_id for n in stored["nodes"]), (
            "flush() returned but the write was not in the store, so an "
            "instance killed mid-rollout loses a write it acknowledged"
        )


class TestCriterion2SessionsAcrossInstances:
    """The criterion that is conditional, and the condition is the directory.

    The open core ships one session backend and it is file-backed
    (`session_store.py` says the SaaS layer swaps in a DB-backed one behind
    the same seam). `server.py` resolves the directory as `sessions/` beside
    the graph path, which `config.get_graph_path()` resolves against the
    project root. So whether two instances share sessions is decided by
    whether that path is on shared storage - a mounted bucket - and not by
    which graph backend is configured.

    These two tests state both halves, because the failing half is the one
    that took a production deployment down, and nothing else in the repo
    says it out loud.
    """

    def test_two_instances_on_one_directory_share_a_session(self, tmp_path):
        shared = tmp_path / "sessions"
        first = SessionStore(FileSessionPersistenceBackend(shared))
        second = SessionStore(FileSessionPersistenceBackend(shared))

        created = first.create(name="from-instance-a")

        found = second.get(created.id)
        assert found is not None, (
            "an MCP session created on one instance was not found by the "
            "other although both read the same directory"
        )
        assert found.name == "from-instance-a"

    def test_two_instances_on_separate_directories_do_not(self, tmp_path):
        """The 2026-09-01 failure, made explicit rather than left implicit.

        This is what a deployment gets when the graph moved to PostgreSQL and
        nothing mounts shared storage any more: each instance keeps its own
        session directory on container-local disk, and a session created on
        one is absent from the other. 45% of MCP calls failed that way.
        """
        first = SessionStore(FileSessionPersistenceBackend(tmp_path / "a"))
        second = SessionStore(FileSessionPersistenceBackend(tmp_path / "b"))

        created = first.create(name="from-instance-a")

        assert second.get(created.id) is None, (
            "separate directories unexpectedly shared a session - if this "
            "starts passing, the deployment note in docs/CAPACITY.md about "
            "needing shared storage is no longer the whole story"
        )

    def test_the_default_directory_is_derived_from_the_graph_path(self, monkeypatch):
        """Pins WHERE the directory comes from, because that is the whole
        condition. If this derivation changes, the deployment requirement
        changes with it and the capacity document must be re-read.

        Asserts against `AppConfig.resolve_sessions_dir`, which is what the
        server calls. An earlier version of this test built the expected path
        itself and then asserted that path's own shape - which holds for any
        path by construction and could not fail.
        """
        from backend.api_host.config import AppConfig

        monkeypatch.delenv("SESSIONS_DIR", raising=False)
        config = AppConfig()

        assert (
            config.resolve_sessions_dir() == config.get_graph_path().parent / "sessions"
        ), (
            "the default session directory is no longer derived from the "
            "graph path, so the deployment rule in docs/CAPACITY.md about "
            "sharing that directory now points at the wrong place"
        )

    def test_sessions_dir_overrides_the_derivation(self, tmp_path, monkeypatch):
        """The override half. This is the knob a multi-instance deployment
        actually turns to satisfy criterion 2, so it has to work.

        Both this and the test above stop at `AppConfig`. That the SERVER
        uses what it resolves is a separate property, held by
        `backend/api_host/tests/test_session_api.py::TestSessionsDirIsolation`
        - a server that resolved the path correctly and then ignored it would
        pass everything in this file.
        """
        from backend.api_host.config import AppConfig

        elsewhere = tmp_path / "shared" / "sessions"
        monkeypatch.setenv("SESSIONS_DIR", str(elsewhere))

        assert AppConfig().resolve_sessions_dir() == elsewhere, (
            "SESSIONS_DIR no longer overrides the derived path, so a "
            "deployment cannot point its instances at shared storage"
        )
