"""The file backend against the layer-store contract, and its write-ahead
merge (docs/SESSION_OVERLAY_CONTRACT.md §11.6)."""

import json

import pytest

import backend.core.layer_store as ls
import backend.core.storage_backends as storage_backends
from backend.core.storage_backends import (
    BackendCapabilities,
    EntityOperation,
    FileGraphPersistenceBackend,
    GraphJournalError,
)
from backend.core.tests.layer_store_contract import (
    A,
    LayerStoreContract,
    entry,
    fresh,
)
from backend.core.tests.persistence_contract import (
    by_id,
    edge_payload,
    node_payload,
    snapshot,
)


class TestFileLayerStoreContract(LayerStoreContract):
    @pytest.fixture
    def factory(self, tmp_path):
        path = tmp_path / "graph.json"
        return lambda: FileGraphPersistenceBackend(path, staged_sessions=True)


class Crash(BaseException):
    """The process dying: not an Exception, so no handler in the backend runs."""


@pytest.fixture
def path(tmp_path):
    return tmp_path / "graph.json"


@pytest.fixture
def staged(path):
    backend = FileGraphPersistenceBackend(path, staged_sessions=True)
    backend.save_graph_data(
        snapshot(
            [node_payload("a", revision=1), node_payload("b", revision=2)],
            [edge_payload("e1", "a", "b", revision=3)],
        )
    )
    backend.record_staged(A)
    backend.stage(A, [entry("node", "a"), entry("node", "b")])
    return backend


def reopen(path):
    return FileGraphPersistenceBackend(path, staged_sessions=True)


def merge_a(backend, merge_id="m1", name="Merged"):
    current = by_id(backend.load_graph_data(), "nodes")["a"]
    op = EntityOperation(
        "node",
        "upsert",
        "a",
        fresh({**current, "name": name}),
        expected_revision=current["revision"],
    )
    return backend.merge(A, merge_id, [op], [], [("node", "a")], {"actor": "t"})


def name_of_a(backend):
    return by_id(backend.load_graph_data(), "nodes")["a"]["name"]


def staged_keys(backend):
    return sorted((e["kind"], e["entity_id"]) for e in backend.load_layer(A))


def crash_on(monkeypatch, backend, method, call=1):
    real = getattr(backend, method)
    calls = {"n": 0}

    def crashing(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == call:
            raise Crash()
        return real(*args, **kwargs)

    monkeypatch.setattr(backend, method, crashing)


class TestTheWriteAheadMerge:
    def test_a_crash_before_the_pending_record_lands_leaves_nothing(
        self, staged, path, monkeypatch
    ):
        crash_on(monkeypatch, staged, "_append_merge_log_locked")
        with pytest.raises(Crash):
            merge_a(staged)
        after = reopen(path)
        assert name_of_a(after) == "A"
        assert staged_keys(after) == [("node", "a"), ("node", "b")]
        assert after.load_merge("m1") is None

    def test_a_crash_before_the_batch_lands_is_rolled_back_at_load(
        self, staged, path, monkeypatch
    ):
        crash_on(monkeypatch, staged, "_append_journal_locked")
        with pytest.raises(Crash):
            merge_a(staged)
        assert '"pending"' in staged.merge_log_path.read_text()
        after = reopen(path)
        assert name_of_a(after) == "A"
        assert staged_keys(after) == [("node", "a"), ("node", "b")]
        assert after.load_merge("m1") is None
        # The aborted id is free: the merge can be made again under it.
        merge_a(after)
        assert name_of_a(reopen(path)) == "Merged"

    @pytest.mark.parametrize(
        "method,call",
        [("_commit_sessions_locked", 1), ("_append_merge_log_locked", 2)],
        ids=["before-the-layer-rewrite", "before-the-applied-mark"],
    )
    def test_a_crash_after_the_batch_landed_is_completed_at_load(
        self, staged, path, monkeypatch, method, call
    ):
        crash_on(monkeypatch, staged, method, call)
        with pytest.raises(Crash):
            merge_a(staged)
        after = reopen(path)
        assert name_of_a(after) == "Merged"
        assert staged_keys(after) == [("node", "b")]
        record = after.load_merge("m1")
        assert record["status"] == ls.MERGE_APPLIED and record["actor"] == "t"
        # And durably: the completion was written, not only judged.
        assert staged_keys(reopen(path)) == [("node", "b")]
        assert reopen(path).load_merge("m1") == record

    def test_recovery_reads_the_graph_not_the_journal(self, staged, path, monkeypatch):
        """A checkpoint folds the journal into graph.json; the judgement must
        still find the batch there."""
        crash_on(monkeypatch, staged, "_commit_sessions_locked")
        with pytest.raises(Crash):
            merge_a(staged)
        monkeypatch.undo()
        staged.checkpoint()
        assert staged.journal_path.read_text() == ""
        after = reopen(path)
        assert name_of_a(after) == "Merged"
        assert staged_keys(after) == [("node", "b")]

    def test_a_retry_after_a_crash_returns_the_first_result(
        self, staged, path, monkeypatch
    ):
        crash_on(monkeypatch, staged, "_commit_sessions_locked")
        with pytest.raises(Crash):
            merge_a(staged)
        again = merge_a(reopen(path), name="Second")
        assert again.replayed
        assert name_of_a(reopen(path)) == "Merged"

    def test_a_layer_write_failing_after_the_commit_does_not_fail_the_merge(
        self, staged, path, monkeypatch
    ):
        real = staged._write_layers_file
        failures = {"n": 0}

        def failing(sessions):
            if failures["n"] == 0:
                failures["n"] += 1
                raise OSError("disk full")
            return real(sessions)

        monkeypatch.setattr(staged, "_write_layers_file", failing)
        result = merge_a(staged)
        assert not result.replayed
        # Pending until the next write settles it; reads see the judged state.
        assert staged.load_merge("m1")["status"] == ls.MERGE_APPLIED
        assert staged_keys(staged) == [("node", "b")]
        staged.stage(A, [entry("node", "c")])
        after = reopen(path)
        assert staged_keys(after) == [("node", "b"), ("node", "c")]
        assert name_of_a(after) == "Merged"

    def test_an_error_after_the_batch_is_durable_does_not_hide_the_merge(
        self, staged, path, monkeypatch
    ):
        """The journal line is on disk and the append still raises - a close
        reporting a failed upload, say. The merge landed and is reported so,
        not marked aborted while the journal replays it."""
        real = staged._append_journal_locked

        def durable_then_raise(ops, stored):
            real(ops, stored)
            raise OSError("close reported a failed upload")

        monkeypatch.setattr(staged, "_append_journal_locked", durable_then_raise)
        result = merge_a(staged)
        assert not result.replayed
        monkeypatch.undo()
        after = reopen(path)
        assert name_of_a(after) == "Merged"
        assert staged_keys(after) == [("node", "b")]
        assert after.load_merge("m1") == result.record
        assert merge_a(after, name="Again").replayed

    def test_an_error_before_the_batch_is_durable_still_fails_the_merge(
        self, staged, path, monkeypatch
    ):
        real_fsync = storage_backends.os.fsync

        def fail_once(fd):
            monkeypatch.setattr(storage_backends.os, "fsync", real_fsync)
            raise OSError("power lost during append")

        # The first fsync is the pending line's; fail the journal's.
        calls = {"n": 0}

        def second(fd):
            calls["n"] += 1
            if calls["n"] == 2:
                return fail_once(fd)
            return real_fsync(fd)

        monkeypatch.setattr(storage_backends.os, "fsync", second)
        with pytest.raises(OSError):
            merge_a(staged)
        monkeypatch.undo()
        after = reopen(path)
        assert name_of_a(after) == "A"
        assert staged_keys(after) == [("node", "a"), ("node", "b")]
        assert after.load_merge("m1") is None

    def test_load_merge_answers_for_a_landed_merge_whose_completion_fails(
        self, staged, monkeypatch
    ):
        def broken(sessions):
            raise OSError("disk gone")

        monkeypatch.setattr(staged, "_write_layers_file", broken)
        result = merge_a(staged)
        assert staged.load_merge("m1") == result.record

    def test_a_failed_layers_write_changes_nothing(self, staged, path, monkeypatch):
        def broken(sessions):
            raise OSError("disk full")

        monkeypatch.setattr(staged, "_write_layers_file", broken)
        for call in (
            lambda: staged.stage(A, [entry("node", "c")]),
            lambda: staged.discard(A),
            lambda: staged.retire(A),
            lambda: staged.record_staged("9999-9999-9999-9999"),
        ):
            with pytest.raises(OSError):
                call()
        assert staged_keys(staged) == [("node", "a"), ("node", "b")]
        assert staged.staged_status(A) == ls.STATUS_ACTIVE
        assert staged.staged_status("9999-9999-9999-9999") == ls.STATUS_NONE

    @pytest.mark.parametrize("then", ["save", "stage"])
    def test_a_pending_merge_settles_before_the_next_write(
        self, staged, path, monkeypatch, then
    ):
        """Before a whole-graph save replaces the graph it is judged against,
        and before a layer write."""
        real = staged._append_merge_log_locked
        calls = {"n": 0}

        def mark_fails(line):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("disk full")
            return real(line)

        monkeypatch.setattr(staged, "_append_merge_log_locked", mark_fails)
        merge_a(staged)
        assert staged._pending_merge is not None
        if then == "save":
            staged.save_graph_data(snapshot([node_payload("other", revision=9)]))
        else:
            staged.stage(A, [entry("node", "c")])
        assert staged._pending_merge is None
        assert reopen(path).load_merge("m1")["status"] == ls.MERGE_APPLIED

    def test_two_pending_merges_in_the_log_stop_the_load(self, staged, path):
        line = json.dumps(
            {
                "merge_id": "x",
                "status": "pending",
                "session_id": A,
                "journal_id": None,
                "operations": [],
                "removals": [],
                "record": {},
            }
        )
        staged.merge_log_path.write_text(
            line + "\n" + line.replace('"x"', '"y"') + "\n"
        )
        with pytest.raises(GraphJournalError, match="two merges pending"):
            reopen(path).load_graph_data()

    def test_a_write_waits_for_a_pending_merge_to_settle(
        self, staged, path, monkeypatch
    ):
        """Nothing may touch the graph between a landed batch and its applied
        mark, or the load-time judgement could not be trusted."""
        real = staged._append_merge_log_locked
        state = {"calls": 0, "fail": True}

        def flaky(line):
            state["calls"] += 1
            if state["calls"] == 2 and state["fail"]:
                raise OSError("disk full")
            return real(line)

        monkeypatch.setattr(staged, "_append_merge_log_locked", flaky)
        merge_a(staged)
        state["fail"] = False
        staged.apply_batch(
            [EntityOperation.upsert_node(fresh(node_payload("a", name="Later")))]
        )
        after = reopen(path)
        assert name_of_a(after) == "Later"
        assert after.load_merge("m1")["status"] == ls.MERGE_APPLIED

    def test_a_write_is_refused_while_a_pending_merge_cannot_settle(
        self, staged, monkeypatch
    ):
        real = staged._append_merge_log_locked
        calls = {"n": 0}

        def broken(line):
            calls["n"] += 1
            if calls["n"] >= 2:
                raise OSError("disk gone")
            return real(line)

        monkeypatch.setattr(staged, "_append_merge_log_locked", broken)
        merge_a(staged)
        with pytest.raises(OSError):
            staged.apply_batch([EntityOperation.upsert_node(node_payload("z"))])
        assert "z" not in by_id(staged.load_graph_data(), "nodes")

    def test_a_failed_batch_aborts_the_merge_and_leaves_the_layer(
        self, staged, path, monkeypatch
    ):
        def failing(ops, stored):
            raise OSError("journal unwritable")

        monkeypatch.setattr(staged, "_append_journal_locked", failing)
        with pytest.raises(OSError):
            merge_a(staged)
        monkeypatch.undo()
        assert staged.load_merge("m1") is None
        assert staged_keys(reopen(path)) == [("node", "a"), ("node", "b")]
        assert reopen(path).load_merge("m1") is None

    def test_a_graph_holding_part_of_a_pending_merge_is_not_guessed_at(
        self, staged, path, monkeypatch
    ):
        crash_on(monkeypatch, staged, "_append_journal_locked")
        current = by_id(staged.load_graph_data(), "nodes")
        ops = [
            EntityOperation(
                "node",
                "upsert",
                key,
                fresh({**current[key], "name": "M"}),
                expected_revision=current[key]["revision"],
            )
            for key in ("a", "b")
        ]
        with pytest.raises(Crash):
            staged.merge(A, "m1", ops, [], [("node", "a"), ("node", "b")], {})
        # Half of it reaches the graph behind the backend's back.
        data = json.loads(path.read_text())
        for node in data["nodes"]:
            if node["id"] == "a":
                node.update(ops[0].payload)
        path.write_text(json.dumps(data))
        with pytest.raises(GraphJournalError, match="part of it"):
            reopen(path).load_graph_data()

    def test_a_pending_merge_from_another_graph_file_stops_the_load(
        self, staged, path, monkeypatch
    ):
        crash_on(monkeypatch, staged, "_append_journal_locked")
        with pytest.raises(Crash):
            merge_a(staged)
        data = json.loads(path.read_text())
        data["metadata"]["journal_id"] = "another-graph"
        path.write_text(json.dumps(data))
        staged.journal_path.write_text("")
        with pytest.raises(GraphJournalError, match="different graph.json"):
            reopen(path).load_graph_data()

    def test_a_torn_last_line_of_the_merge_log_is_dropped(self, staged, path):
        merge_a(staged)
        with open(staged.merge_log_path, "a", encoding="utf-8") as f:
            f.write('{"merge_id": "m2", "sta')
        after = reopen(path)
        assert after.load_merge("m1")["status"] == ls.MERGE_APPLIED
        assert after.merge_log_path.read_text().endswith("\n")

    def test_a_damaged_layers_file_stops_the_load(self, staged, path):
        staged.layers_path.write_text("{not json")
        with pytest.raises(GraphJournalError, match="layers"):
            reopen(path).load_graph_data()

    def test_a_merge_upsert_must_carry_a_fresh_revision(self, staged, path):
        current = by_id(staged.load_graph_data(), "nodes")["a"]
        for payload in (
            {**current, "name": "x"},
            {**current, "name": "x", "revision": None},
        ):
            op = EntityOperation(
                "node", "upsert", "a", payload, expected_revision=current["revision"]
            )
            with pytest.raises(ls.InvalidLayerRequest):
                staged.merge(A, "m", [op], [], [("node", "a")], {})
        assert reopen(path).load_merge("m") is None


class TestStagedSessionsAreOptIn:
    def test_without_it_nothing_new_is_declared(self, path):
        assert FileGraphPersistenceBackend(path).capabilities() == BackendCapabilities(
            incremental_writes=True, transactions=True
        )

    def test_without_it_no_sidecar_is_read_or_written(self, path):
        backend = FileGraphPersistenceBackend(path)
        backend.save_graph_data(snapshot([node_payload("a")]))
        backend.apply_batch([EntityOperation.upsert_node(node_payload("b"))])
        backend.checkpoint()
        backend.layers_path.write_text("{damaged")
        backend.merge_log_path.write_text("{damaged")
        assert {
            n["id"]
            for n in FileGraphPersistenceBackend(path).load_graph_data()["nodes"]
        } == {"a", "b"}

    def test_without_it_the_layer_store_refuses(self, path):
        backend = FileGraphPersistenceBackend(path)
        backend.save_graph_data(snapshot([node_payload("a")]))
        with pytest.raises(RuntimeError, match="staged_sessions"):
            backend.record_staged(A)

    def test_without_it_a_save_reads_nothing_first(self, path):
        """A whole-graph save over a graph.json that cannot be read - which a
        save exists to replace - succeeds as it always has."""
        path.write_text("{not json")
        backend = FileGraphPersistenceBackend(path)
        backend.save_graph_data(snapshot([node_payload("a")]))
        assert [
            n["id"]
            for n in FileGraphPersistenceBackend(path).load_graph_data()["nodes"]
        ] == ["a"]

    def test_with_it_the_sidecars_appear_only_when_used(self, path):
        backend = FileGraphPersistenceBackend(path, staged_sessions=True)
        backend.save_graph_data(snapshot([node_payload("a")]))
        backend.apply_batch([EntityOperation.upsert_node(node_payload("b"))])
        assert not backend.layers_path.exists()
        assert not backend.merge_log_path.exists()
