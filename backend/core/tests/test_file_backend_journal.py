"""The file backend behind the incremental contract: journal plus checkpoint.

graph.json stays the graph, written whole and atomically. A mutation is one
appended line beside it; a checkpoint folds the journal back in. These tests
pin what that buys - a mutation no longer rewrites every node, a crash loses
nothing that was acknowledged, a batch lands whole or not at all - and what it
must never do: replay a journal onto the wrong graph, or lose one silently.
"""

import json
import os
import tempfile
import threading
from pathlib import Path

import pytest

from backend.core.models import Edge, Node, NodeType
from backend.core.storage import GraphStorage
from backend.core.storage_backends import (
    EntityOperation,
    FileGraphPersistenceBackend,
    GraphJournalError,
)


def _payload(node_id, name=None):
    return {
        "id": node_id,
        "type": "Actor",
        "name": name or node_id.upper(),
        "description": "",
        "summary": "",
        "tags": [],
        "subtypes": [],
        "aliases": [],
        "metadata": {},
        "archived": False,
        "created_at": "2026-09-05T00:00:00+00:00",
        "updated_at": "2026-09-05T00:00:00+00:00",
    }


def _edge_payload(edge_id, source, target):
    return {
        "id": edge_id,
        "source": source,
        "target": target,
        "type": "RELATES_TO",
        "label": "",
        "metadata": {},
        "archived": False,
        "created_at": "2026-09-05T00:00:00+00:00",
    }


def _snapshot(*node_ids):
    return {
        "nodes": [_payload(n) for n in node_ids],
        "edges": [],
        "metadata": {"version": "1.0", "graph_name": "g"},
    }


def _lines(path: Path):
    return path.read_text(encoding="utf-8").splitlines()


def _ids(data, key="nodes"):
    return {entity["id"] for entity in data[key]}


@pytest.fixture
def tmp():
    with tempfile.TemporaryDirectory() as d:
        yield Path(d)


@pytest.fixture
def backend(tmp):
    b = FileGraphPersistenceBackend(tmp / "g.json")
    b.save_graph_data(_snapshot("a", "b"))
    return b


class TestJournalWrites:
    def test_the_journal_lives_beside_the_graph_file(self, tmp):
        b = FileGraphPersistenceBackend(tmp / "sub" / "graph.json")
        assert b.journal_path == tmp / "sub" / "graph.journal.ndjson"

    def test_an_entity_write_appends_one_line_and_leaves_the_graph_file_alone(
        self, backend
    ):
        before = backend.json_path.read_bytes()

        backend.upsert_node(_payload("c"))
        backend.delete_node("a")

        assert backend.json_path.read_bytes() == before
        lines = _lines(backend.journal_path)
        assert len(lines) == 2
        assert json.loads(lines[0])["ops"][0]["action"] == "upsert"
        assert json.loads(lines[1])["ops"] == [
            {"kind": "node", "action": "delete", "entity_id": "a", "payload": None}
        ]

    def test_a_batch_is_exactly_one_line(self, backend):
        backend.apply_batch(
            [
                EntityOperation.upsert_node(_payload("c")),
                EntityOperation.upsert_edge(_edge_payload("e", "a", "c")),
                EntityOperation.delete_node("b"),
            ]
        )

        lines = _lines(backend.journal_path)
        assert len(lines) == 1
        assert [op["entity_id"] for op in json.loads(lines[0])["ops"]] == [
            "c",
            "e",
            "b",
        ]

    def test_the_stored_payload_is_a_copy_of_what_was_passed(self, backend):
        payload = _payload("c")
        backend.upsert_node(payload)
        payload["name"] = "mutated afterwards"

        backend.checkpoint()

        data = json.loads(backend.json_path.read_text())
        assert {n["id"]: n["name"] for n in data["nodes"]}["c"] == "C"

    def test_a_payload_that_cannot_be_serialised_touches_nothing(self, backend):
        before = (
            backend.journal_path.read_bytes() if backend.journal_path.exists() else b""
        )

        with pytest.raises(TypeError):
            backend.upsert_node({**_payload("c"), "metadata": {"when": object()}})

        assert (
            not backend.journal_path.exists()
            or backend.journal_path.read_bytes() == before
        )
        assert "c" not in _ids(backend.load_graph_data())

    def test_a_fresh_backend_reads_the_graph_before_its_first_write(self, tmp):
        """A third party may construct the backend and write without loading
        first; the mirror must be the file's contents, not an empty graph."""
        path = tmp / "g.json"
        FileGraphPersistenceBackend(path).save_graph_data(_snapshot("a"))

        fresh = FileGraphPersistenceBackend(path)
        fresh.upsert_node(_payload("b"))
        fresh.checkpoint()

        assert _ids(json.loads(path.read_text())) == {"a", "b"}


class TestReplay:
    def test_a_load_replays_the_journal_onto_the_snapshot(self, backend):
        backend.upsert_node(_payload("c", name="Cee"))
        backend.delete_node("a")
        backend.upsert_edge(_edge_payload("e", "b", "c"))
        backend.delete_edge("never-existed")

        data = FileGraphPersistenceBackend(backend.json_path).load_graph_data()

        assert _ids(data) == {"b", "c"}
        assert {n["id"]: n["name"] for n in data["nodes"]}["c"] == "Cee"
        assert _ids(data, "edges") == {"e"}
        # graph.json itself is still the snapshot; only the load merged them.
        assert _ids(json.loads(backend.json_path.read_text())) == {"a", "b"}

    def test_replay_is_idempotent_over_a_snapshot_that_already_holds_it(self, backend):
        """A crash between writing the snapshot and truncating the journal
        replays records the snapshot already contains."""
        backend.upsert_node(_payload("c"))
        backend.delete_node("a")
        journal = backend.journal_path.read_bytes()
        backend.checkpoint()
        assert backend.journal_path.read_bytes() == b""

        backend.journal_path.write_bytes(journal)
        data = FileGraphPersistenceBackend(backend.json_path).load_graph_data()

        assert _ids(data) == {"b", "c"}

    def test_the_loaded_copy_can_be_mutated_without_touching_the_mirror(self, backend):
        backend.upsert_node(_payload("c"))
        data = backend.load_graph_data()
        for node in data["nodes"]:
            node["created_at"] = object()  # what Node.from_dict does, in effect

        backend.upsert_node(_payload("d"))
        backend.checkpoint()

        assert _ids(json.loads(backend.json_path.read_text())) == {"a", "b", "c", "d"}

    def test_the_cadence_counts_the_lines_a_load_found(self, tmp):
        path = tmp / "g.json"
        b = FileGraphPersistenceBackend(path, checkpoint_interval=3)
        b.save_graph_data(_snapshot("a"))
        b.upsert_node(_payload("b"))
        b.upsert_node(_payload("c"))
        assert len(_lines(b.journal_path)) == 2

        reopened = FileGraphPersistenceBackend(path, checkpoint_interval=3)
        reopened.load_graph_data()
        reopened.upsert_node(_payload("d"))

        assert reopened.journal_path.read_bytes() == b""
        assert _ids(json.loads(path.read_text())) == {"a", "b", "c", "d"}


class TestCrashShapes:
    def test_an_interrupted_last_append_is_dropped_whole(self, backend, capsys):
        backend.upsert_node(_payload("c"))
        with open(backend.journal_path, "a", encoding="utf-8") as f:
            f.write(
                '{"ops": [{"kind": "node", "action": "upsert", "entity_id": "d", "pay'
            )

        data = FileGraphPersistenceBackend(backend.json_path).load_graph_data()

        assert _ids(data) == {"a", "b", "c"}
        assert "incomplete last record" in capsys.readouterr().out
        assert backend.journal_path.read_bytes().endswith(b"\n")
        _appends_still_land_after(backend.json_path, {"a", "b", "c"})

    def test_a_complete_last_line_that_is_not_json_is_dropped_too(
        self, backend, capsys
    ):
        backend.upsert_node(_payload("c"))
        with open(backend.journal_path, "a", encoding="utf-8") as f:
            f.write("not json at all\n")

        data = FileGraphPersistenceBackend(backend.json_path).load_graph_data()

        assert _ids(data) == {"a", "b", "c"}
        assert "incomplete last record" in capsys.readouterr().out
        _appends_still_land_after(backend.json_path, {"a", "b", "c"})

    def test_an_interrupted_batch_lands_nowhere(self, backend):
        line = json.dumps(
            {
                "ops": [
                    {
                        "kind": "node",
                        "action": "delete",
                        "entity_id": "a",
                        "payload": None,
                    },
                    {
                        "kind": "node",
                        "action": "upsert",
                        "entity_id": "c",
                        "payload": _payload("c"),
                    },
                ]
            }
        )
        with open(backend.journal_path, "a", encoding="utf-8") as f:
            f.write(line[: len(line) // 2])

        data = FileGraphPersistenceBackend(backend.json_path).load_graph_data()

        assert _ids(data) == {"a", "b"}

    def test_damage_before_the_last_line_refuses_to_load(self, backend):
        backend.upsert_node(_payload("c"))
        with open(backend.journal_path, "a", encoding="utf-8") as f:
            f.write("garbage\n")
        backend.upsert_node(_payload("d"))

        with pytest.raises(GraphJournalError, match="line 2"):
            FileGraphPersistenceBackend(backend.json_path).load_graph_data()

    def test_a_refused_journal_stops_the_storage_from_loading(self, backend):
        with open(backend.journal_path, "a", encoding="utf-8") as f:
            f.write("garbage\n" + json.dumps({"ops": []}) + "\n")

        with pytest.raises(GraphJournalError):
            GraphStorage(json_path=str(backend.json_path))

    def test_the_snapshot_write_is_still_atomic(self, backend, monkeypatch):
        """A crash mid-write must leave the previous graph.json intact."""
        before = backend.json_path.read_bytes()
        backend.upsert_node(_payload("c"))

        real_dump = json.dump

        def crash(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr("backend.core.storage_backends.json.dump", crash)
        with pytest.raises(OSError):
            backend.checkpoint()
        monkeypatch.setattr("backend.core.storage_backends.json.dump", real_dump)

        assert backend.json_path.read_bytes() == before
        assert len(_lines(backend.journal_path)) == 1
        assert not [
            p for p in backend.json_path.parent.iterdir() if p.name.startswith("graph_")
        ]


class TestCheckpoints:
    def test_a_checkpoint_folds_the_journal_into_the_graph_file(self, backend):
        backend.upsert_node(_payload("c"))
        backend.delete_node("a")

        backend.checkpoint()

        data = json.loads(backend.json_path.read_text())
        assert _ids(data) == {"b", "c"}
        assert "last_updated" in data["metadata"]
        assert data["metadata"]["graph_name"] == "g"
        assert backend.journal_path.read_bytes() == b""

    def test_a_checkpoint_with_nothing_journaled_does_not_rewrite_the_graph_file(
        self, backend
    ):
        stat = os.stat(backend.json_path)

        backend.checkpoint()

        assert os.stat(backend.json_path).st_mtime_ns == stat.st_mtime_ns

    def test_the_interval_bounds_the_journal(self, tmp):
        b = FileGraphPersistenceBackend(tmp / "g.json", checkpoint_interval=3)
        b.save_graph_data(_snapshot())

        for i in range(7):
            b.upsert_node(_payload(f"n{i}"))

        assert len(_lines(b.journal_path)) == 1  # 7 = 3 + 3 + 1
        assert _ids(json.loads(b.json_path.read_text())) == {f"n{i}" for i in range(6)}

    def test_a_whole_graph_save_truncates_the_journal(self, backend):
        backend.upsert_node(_payload("c"))

        backend.save_graph_data(_snapshot("z"))

        assert backend.journal_path.read_bytes() == b""
        assert _ids(backend.load_graph_data()) == {"z"}

    def test_a_failed_cadence_checkpoint_keeps_the_mutation_and_retries(
        self, tmp, monkeypatch, capsys
    ):
        b = FileGraphPersistenceBackend(tmp / "g.json", checkpoint_interval=2)
        b.save_graph_data(_snapshot())
        real_dump = json.dump
        monkeypatch.setattr(
            "backend.core.storage_backends.json.dump",
            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
        )

        b.upsert_node(_payload("a"))
        b.upsert_node(_payload("b"))  # hits the interval, checkpoint fails

        assert "checkpoint failed" in capsys.readouterr().out
        assert len(_lines(b.journal_path)) == 2
        monkeypatch.setattr("backend.core.storage_backends.json.dump", real_dump)
        b.upsert_node(_payload("c"))
        assert b.journal_path.read_bytes() == b""
        assert _ids(json.loads(b.json_path.read_text())) == {"a", "b", "c"}

    def test_the_interval_must_be_positive(self, tmp):
        with pytest.raises(ValueError):
            FileGraphPersistenceBackend(tmp / "g.json", checkpoint_interval=0)


class TestThroughGraphStorage:
    def test_a_second_instance_sees_unflushed_mutations(self, tmp):
        path = str(tmp / "g.json")
        storage = GraphStorage(json_path=path)
        try:
            storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="A")], [])
            storage.update_node("a", {"name": "Renamed"})
            storage._io_executor.submit(lambda: None).result()  # drained, not flushed
            assert _ids(json.loads(Path(path).read_text())) == set()

            second = GraphStorage(json_path=path)
            try:
                assert second.get_node("a").name == "Renamed"
            finally:
                second.flush()
        finally:
            storage.flush()

    def test_flush_makes_the_graph_file_complete(self, tmp):
        path = str(tmp / "g.json")
        storage = GraphStorage(json_path=path)
        try:
            storage.add_nodes(
                [
                    Node(id="a", type=NodeType.ACTOR, name="A"),
                    Node(id="b", type=NodeType.ACTOR, name="B"),
                ],
                [Edge(id="e", source="a", target="b")],
            )
            storage.flush()

            data = json.loads(Path(path).read_text())
            assert _ids(data) == {"a", "b"} and _ids(data, "edges") == {"e"}
            assert storage._persistence_backend.journal_path.read_bytes() == b""
        finally:
            storage.flush()

    def test_shutdown_checkpoints_what_is_still_journaled(self, tmp):
        path = str(tmp / "g.json")
        storage = GraphStorage(json_path=path)
        storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="A")], [])

        storage.shutdown_events()

        assert _ids(json.loads(Path(path).read_text())) == {"a"}
        assert storage._persistence_backend.journal_path.read_bytes() == b""

    def test_a_mutation_costs_one_append_not_a_graph_rewrite(self, tmp):
        path = str(tmp / "g.json")
        storage = GraphStorage(json_path=path)
        try:
            storage.add_nodes(
                [
                    Node(id=f"n{i}", type=NodeType.ACTOR, name=f"N{i}")
                    for i in range(50)
                ],
                [],
            )
            storage.flush()
            stat = os.stat(path)

            for i in range(10):
                storage.update_node("n0", {"name": f"rename {i}"})
            storage._io_executor.submit(lambda: None).result()

            assert os.stat(path).st_mtime_ns == stat.st_mtime_ns
            assert len(_lines(storage._persistence_backend.journal_path)) == 10
        finally:
            storage.flush()


class TestFailureReporting:
    def test_a_valid_last_line_without_its_newline_is_still_an_interrupted_append(
        self, backend, capsys
    ):
        """The newline is what says the append completed; a well-formed line
        without it was not acknowledged and must not be replayed."""
        backend.upsert_node(_payload("c"))
        with open(backend.journal_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ops": [asdict_op(EntityOperation.delete_node("a"))]}))

        data = FileGraphPersistenceBackend(backend.json_path).load_graph_data()

        assert _ids(data) == {"a", "b", "c"}
        assert "incomplete last record" in capsys.readouterr().out

    def test_flush_surfaces_a_failed_checkpoint(self, tmp, monkeypatch):
        storage = GraphStorage(json_path=str(tmp / "g.json"))
        try:
            storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="A")], [])
            monkeypatch.setattr(
                "backend.core.storage_backends.json.dump",
                lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
            )
            with pytest.raises(OSError, match="disk full"):
                storage.flush()
        finally:
            monkeypatch.undo()
            storage.flush()

    def test_a_failed_checkpoint_at_shutdown_is_reported_not_swallowed(
        self, tmp, monkeypatch, capsys
    ):
        storage = GraphStorage(json_path=str(tmp / "g.json"))
        storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="A")], [])
        monkeypatch.setattr(
            "backend.core.storage_backends.json.dump",
            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
        )

        storage.shutdown_events()  # must not raise out of the shutdown hook

        assert "checkpoint at shutdown failed" in capsys.readouterr().out
        # The journal still holds the mutation for the next start.
        assert len(_lines(storage._persistence_backend.journal_path)) == 1

    def test_shutdown_twice_is_harmless(self, tmp):
        storage = GraphStorage(json_path=str(tmp / "g.json"))
        storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="A")], [])

        storage.shutdown_events()
        storage.shutdown_events()

        assert _ids(json.loads((tmp / "g.json").read_text())) == {"a"}


def _appends_still_land_after(json_path, expected):
    """After a load that dropped a tail, a further append must land on a fresh
    line and be seen - with no error - by the loads that follow."""
    reopened = FileGraphPersistenceBackend(json_path)
    reopened.load_graph_data()
    reopened.upsert_node(_payload("z"))
    assert _ids(
        FileGraphPersistenceBackend(json_path).load_graph_data()
    ) == expected | {"z"}


def asdict_op(op):
    return {
        "kind": op.kind,
        "action": op.action,
        "entity_id": op.entity_id,
        "payload": op.payload,
    }


class TestOrderingAndFailedAppends:
    def test_the_sidecar_lands_before_the_entity_write(self, tmp):
        storage = GraphStorage(json_path=str(tmp / "g.json"))
        order = []
        real_sidecar_save = storage._embedding_sidecar.save
        real_apply = storage._persistence_backend.apply_batch
        real_upsert = storage._persistence_backend.upsert_node

        def sidecar_save(vectors):
            order.append("sidecar")
            return real_sidecar_save(vectors)

        storage._embedding_sidecar.save = sidecar_save
        storage._persistence_backend.apply_batch = lambda ops: (
            order.append("entity"),
            real_apply(ops),
        )
        storage._persistence_backend.upsert_node = lambda n: (
            order.append("entity"),
            real_upsert(n),
        )
        try:
            storage.add_nodes(
                [Node(id="v", type=NodeType.ACTOR, name="V", embedding=[0.5, 0.25])],
                [],
            )
            storage._io_executor.submit(lambda: None).result()

            assert order[:2] == ["sidecar", "entity"]
        finally:
            storage.flush()

    def test_a_failed_append_leaves_the_mirror_untouched(self, backend):
        """A mutation the caller was told failed must not become durable at
        the next checkpoint: the mirror is applied only after the append."""
        backend.journal_path.mkdir()  # opening it for append now fails
        try:
            with pytest.raises(OSError):
                backend.upsert_node(_payload("c"))
        finally:
            backend.journal_path.rmdir()

        backend.upsert_node(_payload("d"))
        backend.checkpoint()

        assert _ids(json.loads(backend.json_path.read_text())) == {"a", "b", "d"}

    def test_operations_within_a_record_replay_in_order(self, backend):
        backend.apply_batch(
            [
                EntityOperation.upsert_node(_payload("c", name="first")),
                EntityOperation.upsert_node(_payload("c", name="second")),
                EntityOperation.upsert_node(_payload("d")),
                EntityOperation.delete_node("d"),
            ]
        )

        data = FileGraphPersistenceBackend(backend.json_path).load_graph_data()

        assert {n["id"]: n["name"] for n in data["nodes"]}["c"] == "second"
        assert "d" not in _ids(data)

    def test_records_replay_in_order(self, backend):
        backend.upsert_node(_payload("c", name="first"))
        backend.upsert_node(_payload("c", name="second"))
        backend.delete_node("b")
        backend.upsert_node(_payload("b", name="back"))

        data = FileGraphPersistenceBackend(backend.json_path).load_graph_data()

        names = {n["id"]: n["name"] for n in data["nodes"]}
        assert names["c"] == "second" and names["b"] == "back"


class TestRecoveryAfterFailure:
    def test_an_interrupted_tail_is_cut_off_so_the_next_append_is_not_glued_to_it(
        self, backend
    ):
        """Dropping the tail at read time is not enough: left in the file, the
        next append lands on the same line and becomes the next 'incomplete
        last record' - an acknowledged write lost at the following start."""
        backend.upsert_node(_payload("b2"))
        with open(backend.journal_path, "a", encoding="utf-8") as f:
            f.write(
                '{"ops": [{"kind": "node", "action": "upsert", "entity_id": "x", "pay'
            )

        restarted = FileGraphPersistenceBackend(backend.json_path)
        assert _ids(restarted.load_graph_data()) == {"a", "b", "b2"}
        assert backend.journal_path.read_bytes().endswith(b"\n")
        restarted.upsert_node(_payload("c"))

        third = FileGraphPersistenceBackend(backend.json_path)
        assert _ids(third.load_graph_data()) == {"a", "b", "b2", "c"}
        third.upsert_node(_payload("d"))
        assert _ids(
            FileGraphPersistenceBackend(backend.json_path).load_graph_data()
        ) == {
            "a",
            "b",
            "b2",
            "c",
            "d",
        }

    def test_a_failed_entity_write_is_healed_by_a_whole_graph_write(self, tmp):
        """Before the entity path existed, a transiently failed write was
        healed by the next save rewriting everything. A failed append leaves
        the backend's image without the mutation, so it must be re-issued
        whole or a later checkpoint writes an image that never had it."""
        path = str(tmp / "g.json")
        storage = GraphStorage(json_path=path)
        try:
            storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="A")], [])
            storage.flush()
            backend = storage._persistence_backend
            real = backend.upsert_node
            failures = {"left": 1}

            def flaky(node):
                if failures["left"]:
                    failures["left"] -= 1
                    raise OSError("transient append failure")
                return real(node)

            backend.upsert_node = flaky
            storage.update_node("a", {"name": "Renamed"})
            storage.flush()

            data = json.loads(Path(path).read_text())
            assert {n["id"]: n["name"] for n in data["nodes"]}["a"] == "Renamed"
            assert backend.journal_path.read_bytes() == b""
        finally:
            storage.flush()


class TestHealingWithoutDeadlock:
    def _failing_once(self, storage):
        backend = storage._persistence_backend
        real = backend.upsert_node
        failures = {"left": 1}

        def flaky(node):
            if failures["left"]:
                failures["left"] -= 1
                raise OSError("transient append failure")
            return real(node)

        backend.upsert_node = flaky

    def test_the_failed_write_keeps_its_own_exception(self, tmp):
        storage = GraphStorage(json_path=str(tmp / "g.json"))
        try:
            storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="A")], [])
            storage.flush()
            self._failing_once(storage)
            with storage._lock:
                future = storage._persist(
                    [
                        EntityOperation.upsert_node(
                            storage._serialize_node(storage.nodes["a"])
                        )
                    ]
                )
            with pytest.raises(OSError, match="transient append failure"):
                future.result()
        finally:
            storage.flush()

    def test_reload_after_a_failed_write_does_not_hang(self, tmp):
        """load() waits on the writer while holding the lock. A heal that
        took the lock on the writer would deadlock the whole process."""
        import threading

        storage = GraphStorage(json_path=str(tmp / "g.json"))
        storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="A")], [])
        storage.flush()
        self._failing_once(storage)
        storage.update_node("a", {"name": "Renamed"})
        # A missing file is the reload() shape that waits on the writer while
        # holding the lock (the bootstrap write); an existing file never does.
        os.unlink(tmp / "g.json")
        storage._persistence_backend.journal_path.unlink(missing_ok=True)

        done = threading.Event()

        def run():
            storage.reload()
            done.set()

        threading.Thread(target=run, daemon=True).start()
        assert done.wait(timeout=20), "reload() deadlocked after a failed entity write"
        storage.flush()

    def test_shutdown_heals_a_write_that_failed_after_the_last_flush(self, tmp):
        path = str(tmp / "g.json")
        storage = GraphStorage(json_path=path)
        storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="A")], [])
        storage.flush()
        self._failing_once(storage)
        storage.update_node("a", {"name": "Renamed"})

        storage.shutdown_events()

        data = json.loads(Path(path).read_text())
        assert {n["id"]: n["name"] for n in data["nodes"]}["a"] == "Renamed"

    def test_a_short_append_in_a_live_process_leaves_no_bytes_behind(
        self, backend, monkeypatch
    ):
        """The tail cut at load does not help the process that suffered the
        short write: it never loads. The append itself must restore the
        length, or the next append lands on the same line and, at the next
        start, that line is damage rather than a crash tail."""
        import backend.core.storage_backends as sb

        real_fsync = os.fsync

        def fail_once(fd):
            monkeypatch.setattr(sb.os, "fsync", real_fsync)
            raise OSError("fsync failed")

        monkeypatch.setattr(sb.os, "fsync", fail_once)
        before = (
            backend.journal_path.stat().st_size if backend.journal_path.exists() else 0
        )
        with pytest.raises(OSError, match="fsync failed"):
            backend.upsert_node(_payload("c"))

        assert (
            backend.journal_path.stat().st_size if backend.journal_path.exists() else 0
        ) == before
        backend.upsert_node(_payload("d"))
        backend.upsert_node(_payload("e"))

        data = FileGraphPersistenceBackend(backend.json_path).load_graph_data()
        assert _ids(data) == {"a", "b", "d", "e"}


class TestHealingIsWholeGraph:
    def _fail_once(self, obj, name):
        real = getattr(obj, name)
        failures = {"left": 1}

        def flaky(*args):
            if failures["left"]:
                failures["left"] -= 1
                raise OSError(f"transient {name} failure")
            return real(*args)

        setattr(obj, name, flaky)

    def test_a_failed_batch_is_healed_too(self, tmp):
        path = str(tmp / "g.json")
        storage = GraphStorage(json_path=path)
        try:
            storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="A")], [])
            storage.flush()
            self._fail_once(storage._persistence_backend, "apply_batch")

            storage.add_nodes(
                [
                    Node(id="b", type=NodeType.ACTOR, name="B"),
                    Node(id="c", type=NodeType.ACTOR, name="C"),
                ],
                [],
            )
            storage.flush()

            assert _ids(json.loads(Path(path).read_text())) == {"a", "b", "c"}
        finally:
            storage.flush()

    def test_a_permanently_failing_entity_write_is_still_healed_by_a_snapshot(
        self, tmp
    ):
        """Retrying the entity op would never land; only a whole-graph write
        can, and that is what the heal must be."""
        path = str(tmp / "g.json")
        storage = GraphStorage(json_path=path)
        backend = storage._persistence_backend
        real = backend.upsert_node
        try:
            storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="A")], [])
            storage.flush()

            def always_fails(node):
                raise OSError("entity path down")

            backend.upsert_node = always_fails
            storage.update_node("a", {"name": "Renamed"})
            storage.flush()

            data = json.loads(Path(path).read_text())
            assert {n["id"]: n["name"] for n in data["nodes"]}["a"] == "Renamed"
        finally:
            backend.upsert_node = real
            storage.flush()

    def test_a_failed_healing_snapshot_surfaces_and_is_retried(self, tmp):
        """Two failures in a row must not leave a permanent, silent gap: the
        heal that failed raises the flag again, flush() reports it, and the
        next write is whole-graph again until one lands."""
        path = str(tmp / "g.json")
        storage = GraphStorage(json_path=path)
        backend = storage._persistence_backend
        try:
            storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="A")], [])
            storage.flush()
            self._fail_once(backend, "upsert_node")
            self._fail_once(backend, "save_graph_data")

            storage.add_nodes([Node(id="b", type=NodeType.ACTOR, name="B")], [])
            with pytest.raises(OSError, match="save_graph_data"):
                storage.flush()
            assert storage._resync_pending

            storage.update_node("a", {"name": "Renamed"})
            storage.flush()

            data = json.loads(Path(path).read_text())
            assert _ids(data) == {"a", "b"}
            assert {n["id"]: n["name"] for n in data["nodes"]}["a"] == "Renamed"
        finally:
            storage.flush()

    def test_shutdown_writes_synchronously_when_the_queued_heal_is_gone(
        self, tmp, monkeypatch
    ):
        path = str(tmp / "g.json")
        storage = GraphStorage(json_path=path)
        storage.add_nodes([Node(id="a", type=NodeType.ACTOR, name="A")], [])
        storage.flush()
        self._fail_once(storage._persistence_backend, "upsert_node")
        storage.update_node("a", {"name": "Renamed"})
        # The in-queue heal never runs: only the synchronous fallback is left.
        monkeypatch.setattr(storage, "_heal_if_needed", lambda: None)

        storage.shutdown_events()

        data = json.loads(Path(path).read_text())
        assert {n["id"]: n["name"] for n in data["nodes"]}["a"] == "Renamed"

    def test_a_short_write_is_a_failed_append(self, backend, monkeypatch):
        """A raw write may land fewer bytes than given without raising; that
        is a failure, and the length is restored like any other."""
        import backend.core.storage_backends as sb

        real_open = open

        class HalfWriter:
            def __init__(self, f):
                self._f = f

            def write(self, data):
                return self._f.write(data[: len(data) // 2])

            def __getattr__(self, name):
                return getattr(self._f, name)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return self._f.__exit__(*exc)

        def half_open(path, mode="r", *args, **kwargs):
            f = real_open(path, mode, *args, **kwargs)
            if mode == "ab":
                monkeypatch.setattr(sb, "open", real_open, raising=False)
                return HalfWriter(f)
            return f

        backend.upsert_node(_payload("b2"))  # a real line to be glued onto
        before = backend.journal_path.stat().st_size
        monkeypatch.setattr(sb, "open", half_open, raising=False)
        with pytest.raises(OSError, match="short journal write"):
            backend.upsert_node(_payload("c"))

        assert backend.journal_path.stat().st_size == before
        backend.upsert_node(_payload("d"))
        assert _ids(
            FileGraphPersistenceBackend(backend.json_path).load_graph_data()
        ) == {
            "a",
            "b",
            "b2",
            "d",
        }


def _graph_metadata(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))["metadata"]


def _records(path: Path):
    return [json.loads(line) for line in _lines(path)]


class TestJournalIdentity:
    """A journal is bound to the graph.json it was written against."""

    def test_the_snapshot_carries_an_id_and_every_record_names_it(self, backend):
        stamp = _graph_metadata(backend.json_path)["journal_id"]
        assert stamp
        backend.upsert_node(_payload("c"))
        backend.apply_batch(
            [
                EntityOperation.delete_node("a"),
                EntityOperation.upsert_node(_payload("d")),
            ]
        )
        assert [r["journal_id"] for r in _records(backend.journal_path)] == [
            stamp,
            stamp,
        ]

    def test_the_id_is_kept_across_checkpoints_and_whole_graph_saves(self, backend):
        stamp = _graph_metadata(backend.json_path)["journal_id"]
        backend.upsert_node(_payload("c"))
        backend.checkpoint()
        assert _graph_metadata(backend.json_path)["journal_id"] == stamp
        backend.save_graph_data(_snapshot("z"))
        assert _graph_metadata(backend.json_path)["journal_id"] == stamp
        backend.upsert_node(_payload("y"))
        assert _records(backend.journal_path)[0]["journal_id"] == stamp

    def test_the_id_is_the_backends_own_and_not_handed_to_the_caller(self, tmp):
        backend = FileGraphPersistenceBackend(tmp / "g.json")
        backend.save_graph_data(_snapshot("a"))
        assert "journal_id" not in backend.load_graph_data()["metadata"]
        backend.upsert_node(_payload("b"))
        assert "journal_id" not in backend.load_graph_data()["metadata"]
        storage = GraphStorage(str(tmp / "g.json"))
        assert "journal_id" not in storage.graph_metadata
        storage.shutdown_events()

    def test_a_whole_graph_save_through_the_storage_keeps_the_files_id(self, tmp):
        storage = GraphStorage(str(tmp / "g.json"))
        stamp = _graph_metadata(tmp / "g.json")["journal_id"]
        storage.add_nodes([Node(id="n1", type=NodeType.ACTOR, name="N1")], [])
        storage.save().result()
        storage.shutdown_events()
        assert _graph_metadata(tmp / "g.json")["journal_id"] == stamp

    def test_a_journal_from_a_different_graph_is_refused(self, tmp, backend):
        backend.upsert_node(_payload("c"))
        other = FileGraphPersistenceBackend(tmp / "other.json")
        other.save_graph_data(_snapshot("x"))
        (tmp / "g.json").write_bytes((tmp / "other.json").read_bytes())

        before = backend.journal_path.read_bytes()
        with pytest.raises(GraphJournalError, match="different graph.json"):
            FileGraphPersistenceBackend(tmp / "g.json").load_graph_data()
        assert backend.journal_path.read_bytes() == before, (
            "the journal is left for the operator, byte for byte"
        )

    def test_the_refusal_names_both_ids_and_the_line(self, tmp, backend):
        backend.upsert_node(_payload("c"))
        record_id = _records(backend.journal_path)[0]["journal_id"]
        other = FileGraphPersistenceBackend(tmp / "other.json")
        other.save_graph_data(_snapshot("x"))
        file_id = _graph_metadata(tmp / "other.json")["journal_id"]
        (tmp / "g.json").write_bytes((tmp / "other.json").read_bytes())

        with pytest.raises(GraphJournalError) as excinfo:
            FileGraphPersistenceBackend(tmp / "g.json").load_graph_data()
        message = str(excinfo.value)
        assert "line 1" in message
        assert record_id in message and file_id in message
        assert "delete the journal" in message
        assert "put back the graph.json" in message
        # Two stamped files: nothing says the records are safe anywhere.
        assert "loses nothing" not in message

    def test_a_refused_journal_stops_the_storage_from_loading_too(self, tmp, backend):
        backend.upsert_node(_payload("c"))
        other = FileGraphPersistenceBackend(tmp / "other.json")
        other.save_graph_data(_snapshot("x"))
        (tmp / "g.json").write_bytes((tmp / "other.json").read_bytes())
        with pytest.raises(GraphJournalError, match="different graph.json"):
            GraphStorage(str(tmp / "g.json"))

    def test_a_matching_journal_beside_a_restored_copy_still_replays(
        self, tmp, backend
    ):
        """Same lineage: a graph.json copied out earlier plus the journal
        written since is exactly the crash-recovery shape, not a mismatch."""
        copy = (tmp / "g.json").read_bytes()
        backend.upsert_node(_payload("c"))
        (tmp / "g.json").write_bytes(copy)
        loaded = FileGraphPersistenceBackend(tmp / "g.json").load_graph_data()
        assert _ids(loaded) == {"a", "b", "c"}

    def test_an_unstamped_graph_with_an_unstamped_journal_replays(self, tmp):
        """The upgrade path: both files predate the stamp."""
        (tmp / "g.json").write_text(json.dumps(_snapshot("a")), encoding="utf-8")
        (tmp / "g.journal.ndjson").write_text(
            json.dumps({"ops": [asdict_op(EntityOperation.upsert_node(_payload("b")))]})
            + "\n",
            encoding="utf-8",
        )
        loaded = FileGraphPersistenceBackend(tmp / "g.json").load_graph_data()
        assert _ids(loaded) == {"a", "b"}

    def test_an_unstamped_graph_with_a_stamped_journal_is_refused(self, tmp, backend):
        backend.upsert_node(_payload("c"))
        (tmp / "g.json").write_text(json.dumps(_snapshot("a", "b")), encoding="utf-8")
        with pytest.raises(GraphJournalError, match="id none") as excinfo:
            FileGraphPersistenceBackend(tmp / "g.json").load_graph_data()
        # A pre-stamp copy put back: the records are NOT in it.
        assert "loses nothing" not in str(excinfo.value)
        assert "put back the graph.json" in str(excinfo.value)

    def test_a_foreign_record_in_the_middle_is_refused_at_its_line(self, tmp, backend):
        backend.upsert_node(_payload("c"))
        own = _lines(backend.journal_path)[0]
        foreign = json.dumps(
            {
                "journal_id": "elsewhere",
                "ops": [asdict_op(EntityOperation.upsert_node(_payload("x")))],
            }
        )
        backend.journal_path.write_text(
            "\n".join([own, foreign, own]) + "\n", encoding="utf-8"
        )
        before = backend.journal_path.read_bytes()
        with pytest.raises(GraphJournalError, match="line 2") as excinfo:
            FileGraphPersistenceBackend(tmp / "g.json").load_graph_data()
        assert "elsewhere" in str(excinfo.value)
        assert backend.journal_path.read_bytes() == before

    def test_a_stamped_graph_with_an_unstamped_journal_is_refused(self, backend):
        backend.journal_path.write_text(
            json.dumps({"ops": [asdict_op(EntityOperation.upsert_node(_payload("c")))]})
            + "\n",
            encoding="utf-8",
        )
        with pytest.raises(GraphJournalError, match="journal id none") as excinfo:
            FileGraphPersistenceBackend(backend.json_path).load_graph_data()
        # The one shape that is not a swap: the stamping checkpoint interrupted
        # after the snapshot and before the truncate. The message covers it.
        assert "interrupted before it emptied the journal" in str(excinfo.value)

    def test_a_null_id_record_beside_a_stamped_graph_is_refused(self, backend):
        backend.journal_path.write_text(
            json.dumps(
                {
                    "journal_id": None,
                    "ops": [asdict_op(EntityOperation.upsert_node(_payload("c")))],
                }
            )
            + "\n",
            encoding="utf-8",
        )
        with pytest.raises(GraphJournalError, match="journal id none"):
            FileGraphPersistenceBackend(backend.json_path).load_graph_data()

    def test_the_interrupted_stamp_checkpoint_shape_loses_nothing(
        self, tmp, monkeypatch
    ):
        """Crash between the stamping snapshot and the truncate: the file is
        stamped and holds the legacy records; the journal still has them
        without an id. Load refuses, and following the message (delete the
        journal) gives the complete graph."""
        (tmp / "g.json").write_text(json.dumps(_snapshot("a")), encoding="utf-8")
        (tmp / "g.journal.ndjson").write_text(
            json.dumps({"ops": [asdict_op(EntityOperation.upsert_node(_payload("b")))]})
            + "\n",
            encoding="utf-8",
        )
        backend = FileGraphPersistenceBackend(tmp / "g.json")
        monkeypatch.setattr(
            backend,
            "_truncate_journal",
            lambda: (_ for _ in ()).throw(OSError("crash")),
        )
        with pytest.raises(OSError, match="crash"):
            backend.upsert_node(_payload("c"))

        with pytest.raises(GraphJournalError, match="loses nothing"):
            FileGraphPersistenceBackend(tmp / "g.json").load_graph_data()
        (tmp / "g.journal.ndjson").unlink()
        assert _ids(FileGraphPersistenceBackend(tmp / "g.json").load_graph_data()) == {
            "a",
            "b",
        }

    def test_the_cadence_checkpoint_keeps_the_id(self, tmp):
        backend = FileGraphPersistenceBackend(tmp / "g.json", checkpoint_interval=2)
        backend.save_graph_data(_snapshot("a"))
        stamp = _graph_metadata(tmp / "g.json")["journal_id"]
        backend.upsert_node(_payload("b"))
        backend.upsert_node(_payload("c"))
        assert _lines(tmp / "g.journal.ndjson") == [], "the cadence checkpoint ran"
        assert _graph_metadata(tmp / "g.json")["journal_id"] == stamp
        backend.upsert_node(_payload("d"))
        assert _records(tmp / "g.journal.ndjson")[0]["journal_id"] == stamp

    def test_the_first_append_on_an_unstamped_graph_stamps_it_first(self, tmp):
        (tmp / "g.json").write_text(json.dumps(_snapshot("a")), encoding="utf-8")
        (tmp / "g.journal.ndjson").write_text(
            json.dumps({"ops": [asdict_op(EntityOperation.upsert_node(_payload("b")))]})
            + "\n",
            encoding="utf-8",
        )
        backend = FileGraphPersistenceBackend(tmp / "g.json")
        backend.upsert_node(_payload("c"))

        stamp = _graph_metadata(tmp / "g.json").get("journal_id")
        assert stamp, "the file was stamped by a checkpoint"
        assert _ids(json.loads((tmp / "g.json").read_text())) == {"a", "b"}, (
            "the pre-stamp journal was folded in by that checkpoint"
        )
        records = _records(tmp / "g.journal.ndjson")
        assert [r["journal_id"] for r in records] == [stamp]
        assert _ids(FileGraphPersistenceBackend(tmp / "g.json").load_graph_data()) == {
            "a",
            "b",
            "c",
        }

    def test_a_failed_stamp_fails_the_append_and_leaves_no_record(
        self, tmp, monkeypatch
    ):
        (tmp / "g.json").write_text(json.dumps(_snapshot("a")), encoding="utf-8")
        backend = FileGraphPersistenceBackend(tmp / "g.json")
        real_dump = json.dump
        calls = {"n": 0}

        def failing_dump(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("disk full")
            return real_dump(*args, **kwargs)

        monkeypatch.setattr(json, "dump", failing_dump)
        with pytest.raises(OSError, match="disk full"):
            backend.upsert_node(_payload("b"))

        assert "journal_id" not in _graph_metadata(tmp / "g.json")
        assert not (tmp / "g.journal.ndjson").exists() or not _lines(
            tmp / "g.journal.ndjson"
        )
        # The next append retries the stamp rather than naming an id the
        # file does not carry.
        backend.upsert_node(_payload("b"))
        stamp = _graph_metadata(tmp / "g.json")["journal_id"]
        assert [r["journal_id"] for r in _records(tmp / "g.journal.ndjson")] == [stamp]

    def test_a_failed_whole_graph_save_leaves_the_mirror_unstamped(
        self, tmp, monkeypatch
    ):
        """Round-2 finding: the minted id must not sit in the mirror while the
        file is still unstamped, or the next append writes a record naming an
        id no graph.json ever carried."""
        (tmp / "g.json").write_text(json.dumps(_snapshot("a")), encoding="utf-8")
        backend = FileGraphPersistenceBackend(tmp / "g.json")
        backend.load_graph_data()
        real_dump = json.dump
        calls = {"n": 0}

        def failing_dump(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("disk full")
            return real_dump(*args, **kwargs)

        monkeypatch.setattr(json, "dump", failing_dump)
        with pytest.raises(OSError, match="disk full"):
            backend.save_graph_data(_snapshot("a", "z"))
        assert "journal_id" not in _graph_metadata(tmp / "g.json")

        backend.upsert_node(_payload("b"))
        stamp = _graph_metadata(tmp / "g.json")["journal_id"]
        assert stamp, "the append stamped the file first"
        assert [r["journal_id"] for r in _records(tmp / "g.journal.ndjson")] == [stamp]
        assert _ids(FileGraphPersistenceBackend(tmp / "g.json").load_graph_data()) == {
            "a",
            "z",
            "b",
        }

    def test_a_failed_save_through_the_storage_never_strands_a_record(self, tmp):
        """The same hazard through GraphStorage: a whole-graph save whose
        snapshot fails on a pre-stamp file, then a mutation. Every record on
        disk must name an id the file carries, and a restart must load."""
        (tmp / "g.json").write_text(json.dumps(_snapshot("a")), encoding="utf-8")
        storage = GraphStorage(str(tmp / "g.json"))
        backend = storage._persistence_backend
        real_write = backend._write_snapshot
        save_may_fail = threading.Event()
        fail = {"once": True}

        def failing_write(data):
            if fail["once"]:
                fail["once"] = False
                # Hold the save until the mutation is queued behind it, so
                # the mutation was shaped as an entity write before the
                # failure could ask for a whole-graph resync instead.
                assert save_may_fail.wait(5)
                raise OSError("disk full")
            real_write(data)

        backend._write_snapshot = failing_write
        failing_save = storage.save()
        storage.add_nodes([Node(id="b", type=NodeType.ACTOR, name="B")], [])
        save_may_fail.set()
        with pytest.raises(OSError, match="disk full"):
            failing_save.result()
        # Drain the queued entity write without the heal a flush would run:
        # the disk must already be loadable at this point (a crash here is
        # the reviewer's shape).
        storage._io_executor.submit(lambda: None).result()

        stamp = _graph_metadata(tmp / "g.json").get("journal_id")
        assert stamp
        assert [r["journal_id"] for r in _records(tmp / "g.journal.ndjson")] == [stamp]
        reopened = GraphStorage(str(tmp / "g.json"))
        assert set(reopened.nodes) == {"a", "b"}
        reopened.shutdown_events()
        storage.shutdown_events()

    def test_a_stamp_whose_truncate_fails_is_retried_and_never_mixes_the_journal(
        self, tmp, monkeypatch
    ):
        """Round-2 finding: the stamping checkpoint's snapshot lands but its
        truncate raises in-process. The append that asked for the stamp
        fails and writes nothing; the mirror stays unstamped, so the next
        append stamps again - and the journal never holds a stamped record
        behind pre-stamp ones, which is the shape the refusal message could
        not be honest about."""
        (tmp / "g.json").write_text(json.dumps(_snapshot("a")), encoding="utf-8")
        (tmp / "g.journal.ndjson").write_text(
            json.dumps({"ops": [asdict_op(EntityOperation.upsert_node(_payload("b")))]})
            + "\n",
            encoding="utf-8",
        )
        backend = FileGraphPersistenceBackend(tmp / "g.json")
        real_truncate = backend._truncate_journal
        fail = {"once": True}

        def failing_truncate():
            if fail["once"]:
                fail["once"] = False
                raise OSError("crash")
            real_truncate()

        monkeypatch.setattr(backend, "_truncate_journal", failing_truncate)
        with pytest.raises(OSError, match="crash"):
            backend.upsert_node(_payload("c"))
        assert _records(tmp / "g.journal.ndjson") == [
            {"ops": [asdict_op(EntityOperation.upsert_node(_payload("b")))]}
        ], "no stamped record landed behind the pre-stamp one"

        backend.upsert_node(_payload("d"))
        stamp = _graph_metadata(tmp / "g.json")["journal_id"]
        assert [r["journal_id"] for r in _records(tmp / "g.journal.ndjson")] == [stamp]
        assert _ids(FileGraphPersistenceBackend(tmp / "g.json").load_graph_data()) == {
            "a",
            "b",
            "d",
        }

    def test_a_save_whose_truncate_fails_leaves_the_mirror_unstamped_too(
        self, tmp, monkeypatch
    ):
        """The whole-graph twin of the previous test: the save's snapshot
        lands stamped, its truncate raises. The mirror must stay unstamped,
        or the next append writes a stamped record behind the pre-stamp one."""
        (tmp / "g.json").write_text(json.dumps(_snapshot("a")), encoding="utf-8")
        (tmp / "g.journal.ndjson").write_text(
            json.dumps({"ops": [asdict_op(EntityOperation.upsert_node(_payload("b")))]})
            + "\n",
            encoding="utf-8",
        )
        backend = FileGraphPersistenceBackend(tmp / "g.json")
        backend.load_graph_data()
        real_truncate = backend._truncate_journal
        fail = {"once": True}

        def failing_truncate():
            if fail["once"]:
                fail["once"] = False
                raise OSError("crash")
            real_truncate()

        monkeypatch.setattr(backend, "_truncate_journal", failing_truncate)
        with pytest.raises(OSError, match="crash"):
            backend.save_graph_data(_snapshot("a", "b", "z"))
        assert "journal_id" not in backend._metadata

        backend.upsert_node(_payload("c"))
        stamp = _graph_metadata(tmp / "g.json")["journal_id"]
        assert [r["journal_id"] for r in _records(tmp / "g.journal.ndjson")] == [stamp]
        assert _ids(FileGraphPersistenceBackend(tmp / "g.json").load_graph_data()) == {
            "a",
            "b",
            "z",
            "c",
        }

    def test_checkpoint_stamps_an_unstamped_graph_when_folding_a_legacy_journal(
        self, tmp
    ):
        (tmp / "g.json").write_text(json.dumps(_snapshot("a")), encoding="utf-8")
        (tmp / "g.journal.ndjson").write_text(
            json.dumps({"ops": [asdict_op(EntityOperation.upsert_node(_payload("b")))]})
            + "\n",
            encoding="utf-8",
        )
        backend = FileGraphPersistenceBackend(tmp / "g.json")
        backend.load_graph_data()
        backend.checkpoint()
        assert _graph_metadata(tmp / "g.json").get("journal_id")
        assert _lines(tmp / "g.journal.ndjson") == []

    def test_a_whole_graph_save_keeps_the_files_id_over_one_the_data_carries(
        self, backend
    ):
        """The mirror's id is what any record on disk names; a dict that
        arrives with another lineage's id (a snapshot copied from elsewhere)
        does not rename the file."""
        stamp = _graph_metadata(backend.json_path)["journal_id"]
        data = _snapshot("z")
        data["metadata"]["journal_id"] = "given"
        backend.save_graph_data(data)
        assert _graph_metadata(backend.json_path)["journal_id"] == stamp
        backend.upsert_node(_payload("y"))
        assert _records(backend.journal_path)[0]["journal_id"] == stamp

    def test_a_null_id_in_a_hand_edited_file_counts_as_unstamped(self, tmp):
        data = _snapshot("a")
        data["metadata"]["journal_id"] = None
        (tmp / "g.json").write_text(json.dumps(data), encoding="utf-8")
        backend = FileGraphPersistenceBackend(tmp / "g.json")
        backend.upsert_node(_payload("b"))
        stamp = _graph_metadata(tmp / "g.json")["journal_id"]
        assert stamp
        assert _records(tmp / "g.journal.ndjson")[0]["journal_id"] == stamp

    def test_a_snapshot_saved_with_an_id_of_its_own_keeps_it_on_a_fresh_file(self, tmp):
        """A caller restoring a snapshot that already carries an id (a copy of
        another graph.json's content) keeps that lineage - the journal is
        truncated in the same call, so nothing can mismatch."""
        data = _snapshot("a")
        data["metadata"]["journal_id"] = "given"
        backend = FileGraphPersistenceBackend(tmp / "g.json")
        backend.save_graph_data(data)
        assert _graph_metadata(tmp / "g.json")["journal_id"] == "given"
        backend.upsert_node(_payload("b"))
        assert _records(tmp / "g.journal.ndjson")[0]["journal_id"] == "given"
