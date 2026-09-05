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

    def test_a_complete_last_line_that_is_not_json_is_dropped_too(
        self, backend, capsys
    ):
        backend.upsert_node(_payload("c"))
        with open(backend.journal_path, "a", encoding="utf-8") as f:
            f.write("not json at all\n")

        data = FileGraphPersistenceBackend(backend.json_path).load_graph_data()

        assert _ids(data) == {"a", "b", "c"}
        assert "incomplete last record" in capsys.readouterr().out

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
