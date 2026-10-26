"""The file backend against the layer-store contract, and its write-ahead
merge (docs/SESSION_OVERLAY_CONTRACT.md §11.6)."""

import json
from datetime import timedelta

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
    B,
    LayerStoreContract,
    PinnedClockRetentionContract,
    entry,
    fresh,
)
from backend.core.tests.persistence_contract import (
    by_id,
    edge_payload,
    node_payload,
    snapshot,
)


class TestFileLayerStoreContract(LayerStoreContract, PinnedClockRetentionContract):
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
                "record": {
                    "merge_id": "x",
                    "session_id": A,
                    "status": ls.MERGE_APPLIED,
                    "applied": [],
                },
            }
        )
        staged.merge_log_path.write_text(
            line + "\n" + line.replace('"x"', '"y"') + "\n"
        )
        with pytest.raises(GraphJournalError, match="two merges pending"):
            reopen(path).load_graph_data()

    @pytest.mark.parametrize("then", ["upsert", "save"])
    def test_a_refused_layers_load_refuses_every_write_after_it(
        self, staged, path, monkeypatch, then
    ):
        """A merge landed and crashed before its mark; the next instance
        cannot read the layers file once. Nothing may be written before the
        merge is judged, or the judgement later would be against a graph the
        write changed."""
        crash_on(monkeypatch, staged, "_commit_sessions_locked")
        with pytest.raises(Crash):
            merge_a(staged)
        after = reopen(path)
        real = after._load_layers_locked
        fails = {"n": 1}

        def flaky():
            if fails["n"]:
                fails["n"] -= 1
                raise OSError("transient read error")
            return real()

        monkeypatch.setattr(after, "_load_layers_locked", flaky)
        with pytest.raises(OSError):
            after.load_graph_data()
        if then == "upsert":
            after.apply_batch(
                [EntityOperation.upsert_node(fresh(node_payload("a", name="Later")))]
            )
        else:
            after.save_graph_data(
                snapshot([node_payload("a", name="Later", revision=50)])
            )
        final = reopen(path)
        assert final.load_merge("m1")["status"] == ls.MERGE_APPLIED
        assert staged_keys(final) == [("node", "b")]

    def test_a_damaged_layers_file_refuses_writes_too(self, staged, path):
        staged.layers_path.write_text("{not json")
        after = reopen(path)
        with pytest.raises(GraphJournalError):
            after.apply_batch([EntityOperation.upsert_node(node_payload("z"))])
        with pytest.raises(GraphJournalError):
            after.save_graph_data(snapshot([node_payload("z")]))

    def test_reads_do_not_show_a_landed_merge_as_still_staged(
        self, staged, path, monkeypatch
    ):
        def broken(sessions):
            raise OSError("disk full")

        monkeypatch.setattr(staged, "_write_layers_file", broken)
        merge_a(staged)
        assert staged_keys(staged) == [("node", "b")]
        assert staged.layers_in_use() == [A]
        with pytest.raises(OSError):
            staged.stage(A, [entry("node", "c")])
        after = reopen(path)
        assert staged_keys(after) == [("node", "b")]

    @pytest.mark.parametrize("error", [KeyboardInterrupt, OSError])
    @pytest.mark.parametrize("failing_line", [1, 2], ids=["pending", "applied"])
    def test_a_log_line_that_landed_with_an_error_does_not_break_the_log(
        self, staged, path, monkeypatch, error, failing_line
    ):
        """The line is synced and the append still raises, so it may be
        written again by whatever settles next. The log must still load, and
        say what happened."""
        real = staged._append_merge_log_locked
        calls = {"n": 0}

        def durable_then_raise(line):
            real(line)
            calls["n"] += 1
            if calls["n"] == failing_line:
                raise error()

        monkeypatch.setattr(staged, "_append_merge_log_locked", durable_then_raise)
        if failing_line == 1:
            with pytest.raises(error):
                merge_a(staged)
        else:
            try:
                merge_a(staged)
            except KeyboardInterrupt:
                pass
        # Whatever comes next settles, then retries the same merge id.
        staged.stage(A, [entry("node", "c")])
        retry = merge_a(staged)
        monkeypatch.undo()
        after = reopen(path)
        assert name_of_a(after) == "Merged"
        assert after.load_merge("m1") == retry.record
        assert staged_keys(after) == [("node", "b"), ("node", "c")]

    def test_a_repeated_pending_line_for_another_merge_still_stops_the_load(
        self, staged, path
    ):
        merge_a(staged)
        lines = staged.merge_log_path.read_text().splitlines()
        pending = json.loads(lines[0])
        other = {**pending, "merge_id": "other"}
        staged.merge_log_path.write_text(
            "\n".join([json.dumps(pending), json.dumps(other)]) + "\n"
        )
        with pytest.raises(GraphJournalError, match="two merges pending"):
            reopen(path).load_graph_data()

    def test_an_interrupt_during_the_commit_is_judged_on_disk(
        self, staged, path, monkeypatch
    ):
        """The record is synced and an interrupt lands before memory follows
        it: the next operation must re-read the disk, not judge the merge on
        the stale mirror."""

        def interrupted(op):
            raise KeyboardInterrupt()

        monkeypatch.setattr(staged, "_apply", interrupted)
        with pytest.raises(KeyboardInterrupt):
            merge_a(staged)
        monkeypatch.undo()
        staged.stage(A, [entry("node", "c")])
        after = reopen(path)
        assert name_of_a(after) == "Merged"
        assert after.load_merge("m1")["status"] == ls.MERGE_APPLIED
        assert staged_keys(after) == [("node", "b"), ("node", "c")]

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

    def test_a_later_operation_failing_leaves_the_mirror_untouched(self, staged):
        current = by_id(staged.load_graph_data(), "nodes")
        ok = EntityOperation(
            "node",
            "upsert",
            "a",
            fresh({**current["a"], "name": "M"}),
            expected_revision=current["a"]["revision"],
        )
        stale = EntityOperation(
            "node",
            "upsert",
            "b",
            fresh({**current["b"], "name": "M"}),
            expected_revision=current["b"]["revision"] + 5,
        )
        with pytest.raises(ls.MergeRaced):
            staged.merge(A, "m1", [ok, stale], [], [("node", "a"), ("node", "b")], {})
        assert staged._nodes["a"]["name"] == "A"

    @pytest.mark.parametrize(
        "lines,match",
        [
            (["{garbage", "VALID"], "cannot be read"),
            (['{"merge_id": "x", "status": "applied"}'], "no pending merge"),
            (["PENDING", '{"merge_id": "other", "status": "applied"}'], "no pending"),
            (['{"merge_id": "x", "status": "aborted"}'], "no pending merge"),
            (["PENDING", '{"merge_id": "m1", "status": "weird"}'], "unknown status"),
        ],
        ids=[
            "damaged-middle",
            "applied-first",
            "mark-for-other",
            "aborted-first",
            "unknown",
        ],
    )
    def test_a_damaged_merge_log_stops_the_load_and_is_kept(
        self, staged, path, lines, match
    ):
        merge_a(staged)
        pending = staged.merge_log_path.read_text().splitlines()[0]
        text = (
            "\n".join(
                pending if line in ("PENDING", "VALID") else line for line in lines
            )
            + "\n"
        )
        staged.merge_log_path.write_text(text)
        with pytest.raises(GraphJournalError, match=match):
            reopen(path).load_graph_data()
        assert staged.merge_log_path.read_text() == text

    def test_load_merge_answers_only_for_the_pending_merge_it_names(
        self, staged, monkeypatch
    ):
        def broken(sessions):
            raise OSError("disk gone")

        monkeypatch.setattr(staged, "_write_layers_file", broken)
        merge_a(staged)
        assert staged.load_merge("unrelated") is None

    @pytest.mark.parametrize(
        "crash_at", ["_append_journal_locked", "_commit_sessions_locked"]
    )
    def test_a_pending_merge_with_a_delete_is_judged_both_ways(
        self, staged, path, monkeypatch, crash_at
    ):
        staged.stage(A, [entry("edge", "e1", action="delete")])
        edge = by_id(staged.load_graph_data(), "edges")["e1"]
        op = EntityOperation("edge", "delete", "e1", expected_revision=edge["revision"])
        crash_on(monkeypatch, staged, crash_at)
        with pytest.raises(Crash):
            staged.merge(A, "md", [op], [], [("edge", "e1")], {})
        after = reopen(path)
        landed = crash_at == "_commit_sessions_locked"
        assert ("e1" in by_id(after.load_graph_data(), "edges")) is not landed
        assert (after.load_merge("md") is not None) is landed
        assert (("edge", "e1") in staged_keys(after)) is not landed

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


# -- failures beyond a crash: an error that lands after a line is durable, an
# interrupt, and the merge-log shapes the reader must refuse ------------------


def log_lines(backend):
    return backend.merge_log_path.read_text().splitlines()


def write_log(backend, lines):
    backend.merge_log_path.write_text("\n".join(lines) + "\n")


def durable_then_raise(backend, monkeypatch, failing_line, error):
    real = backend._append_merge_log_locked
    calls = {"n": 0}

    def f(line):
        real(line)
        calls["n"] += 1
        if calls["n"] == failing_line:
            raise error()

    monkeypatch.setattr(backend, "_append_merge_log_locked", f)


@pytest.mark.parametrize("error", [OSError, KeyboardInterrupt])
def test_a_pending_line_that_landed_and_raised_is_seen_by_the_next_merge(
    staged, path, monkeypatch, error
):
    current = by_id(staged.load_graph_data(), "nodes")["a"]

    def op(name):
        return EntityOperation(
            "node",
            "upsert",
            "a",
            fresh({**current, "name": name}),
            expected_revision=current["revision"],
        )

    durable_then_raise(staged, monkeypatch, 1, error)
    with pytest.raises(error):
        staged.merge(A, "m1", [op("One")], [], [("node", "a")], {})
    monkeypatch.undo()
    # Next operation is another merge id entirely (no graph read in between).
    staged.merge(A, "m2", [op("Two")], [], [("node", "a")], {})
    after = reopen(path)
    assert name_of_a(after) == "Two"


def test_a_landed_merge_is_reported_even_when_its_completion_also_fails(
    staged, path, monkeypatch
):
    real = staged._append_journal_locked

    def durable_then_raise_j(ops, stored):
        real(ops, stored)
        raise OSError("close failed")

    def broken(sessions):
        raise OSError("disk full")

    monkeypatch.setattr(staged, "_append_journal_locked", durable_then_raise_j)
    monkeypatch.setattr(staged, "_write_layers_file", broken)
    result = merge_a(staged)  # landed: must be reported as success
    assert not result.replayed


def test_load_merge_answers_none_for_a_merge_that_did_not_land(
    staged, path, monkeypatch
):
    crash_on(monkeypatch, staged, "_append_journal_locked")
    with pytest.raises(Crash):
        merge_a(staged)
    monkeypatch.undo()
    after = reopen(path)
    monkeypatch.setattr(
        FileGraphPersistenceBackend,
        "_append_merge_log_locked",
        lambda self, line: (_ for _ in ()).throw(OSError("log gone")),
    )
    assert after.load_merge("m1") is None


def test_load_merge_refuses_to_guess_at_a_part_landed_merge(staged, path, monkeypatch):
    current = by_id(staged.load_graph_data(), "nodes")
    ops = [
        EntityOperation(
            "node",
            "upsert",
            k,
            fresh({**current[k], "name": "M"}),
            expected_revision=current[k]["revision"],
        )
        for k in ("a", "b")
    ]

    def broken(sessions):
        raise OSError("disk full")

    monkeypatch.setattr(staged, "_write_layers_file", broken)
    staged.merge(A, "m1", ops, [], [("node", "a"), ("node", "b")], {})
    assert staged._pending_merge is not None
    old_b = dict(current["b"])
    staged._nodes["b"] = old_b  # memory now shows part of the batch only
    with pytest.raises(GraphJournalError):
        staged.load_merge("m1")


def test_an_interrupt_during_the_layers_load_leaves_the_backend_unmirrored(
    staged, path, monkeypatch
):
    fresh_b = reopen(path)
    real = fresh_b._load_layers_locked
    calls = {"n": 0}

    def f():
        calls["n"] += 1
        if calls["n"] == 1:
            raise KeyboardInterrupt()
        return real()

    monkeypatch.setattr(fresh_b, "_load_layers_locked", f)
    with pytest.raises(KeyboardInterrupt):
        fresh_b.load_layer(A)
    assert fresh_b._mirrored is False


def test_a_read_that_settles_the_pending_merge_shows_the_settled_layer(
    staged, path, monkeypatch
):
    real = staged._complete_pending_locked
    calls = {"n": 0}

    def once():
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("first completion fails")
        return real()

    monkeypatch.setattr(staged, "_complete_pending_locked", once)
    merge_a(staged)
    assert staged_keys(staged) == [("node", "b")]


def test_a_landed_merge_hides_only_its_own_sessions_entries(staged, path, monkeypatch):
    staged.record_staged(B)
    staged.stage(B, [entry("node", "a", session_id=B)])

    def broken(sessions):
        raise OSError("disk full")

    monkeypatch.setattr(staged, "_write_layers_file", broken)
    merge_a(staged)
    assert [e["entity_id"] for e in staged.load_layer(B)] == ["a"]
    assert B in staged.layers_in_use()


def test_a_layer_emptied_by_a_landed_merge_is_not_in_use(staged, path, monkeypatch):
    current = by_id(staged.load_graph_data(), "nodes")["a"]
    staged.discard(A)
    staged.stage(A, [entry("node", "a")])
    op = EntityOperation(
        "node",
        "upsert",
        "a",
        fresh({**current, "name": "M"}),
        expected_revision=current["revision"],
    )

    def broken(sessions):
        raise OSError("disk full")

    monkeypatch.setattr(staged, "_write_layers_file", broken)
    staged.merge(A, "m1", [op], [], [("node", "a")], {})
    assert staged.layers_in_use() == []
    assert staged.load_layer(A) == []


def test_a_second_pending_line_for_one_merge_with_other_content_is_damage(staged, path):
    merge_a(staged)
    lines = log_lines(staged)
    p = json.loads(lines[0])
    q = {**p, "record": {**p["record"], "actor": "someone-else"}}
    write_log(staged, [json.dumps(p), json.dumps(q)])
    with pytest.raises(GraphJournalError):
        reopen(path).load_graph_data()


def test_a_mark_for_another_merge_after_a_closed_one_is_damage(staged, path):
    merge_a(staged)
    lines = log_lines(staged)
    write_log(staged, lines + [json.dumps({"merge_id": "zzz", "status": "applied"})])
    with pytest.raises(GraphJournalError):
        reopen(path).load_graph_data()


def test_applied_then_aborted_for_one_merge_is_damage(staged, path):
    merge_a(staged)
    lines = log_lines(staged)
    write_log(staged, lines + [json.dumps({"merge_id": "m1", "status": "aborted"})])
    with pytest.raises(GraphJournalError):
        reopen(path).load_graph_data()


def test_a_repeated_mark_after_another_merge_opened_is_damage(staged, path):
    merge_a(staged)
    lines = log_lines(staged)
    p = json.loads(lines[0])
    other = {**p, "merge_id": "other"}
    write_log(
        staged,
        lines
        + [json.dumps(other), json.dumps({"merge_id": "m1", "status": "applied"})],
    )
    with pytest.raises(GraphJournalError):
        reopen(path).load_graph_data()


def test_a_merge_that_did_not_land_raises_even_when_its_abort_mark_fails(
    staged, path, monkeypatch
):
    def fail_journal(ops, stored):
        raise OSError("before durable")

    monkeypatch.setattr(staged, "_append_journal_locked", fail_journal)
    real = FileGraphPersistenceBackend._append_merge_log_locked
    calls = {"n": 0}

    def log(self, line):
        calls["n"] += 1
        if line["status"] == "aborted":
            raise OSError("log gone")
        return real(self, line)

    monkeypatch.setattr(FileGraphPersistenceBackend, "_append_merge_log_locked", log)
    with pytest.raises(OSError):
        merge_a(staged)


def test_a_failed_reload_after_a_failed_commit_raises_the_original_error(
    staged, path, monkeypatch
):
    def fail_journal(ops, stored):
        raise OSError("original")

    monkeypatch.setattr(staged, "_append_journal_locked", fail_journal)
    current = by_id(staged.load_graph_data(), "nodes")["a"]
    op = EntityOperation(
        "node",
        "upsert",
        "a",
        fresh({**current, "name": "X"}),
        expected_revision=current["revision"],
    )

    def bad_load():
        raise ValueError("reload failed")

    monkeypatch.setattr(staged, "_load_locked", bad_load)
    with pytest.raises(OSError, match="original"):
        staged.merge(A, "m1", [op], [], [("node", "a")], {})
    assert staged._mirrored is False


@pytest.mark.parametrize("line", [b"{garbage\n", b"{}\n", b"null\n", b"\xff\n"])
def test_a_damaged_complete_last_merge_log_line_is_preserved(staged, path, line):
    merge_a(staged)
    raw = staged.merge_log_path.read_bytes() + line
    staged.merge_log_path.write_bytes(raw)
    with pytest.raises(GraphJournalError, match="cannot be read"):
        reopen(path).load_graph_data()
    assert staged.merge_log_path.read_bytes() == raw


@pytest.mark.parametrize(
    "field,value",
    [("merge_id", "other"), ("session_id", B), ("status", ls.MERGE_PENDING)],
)
def test_a_pending_merge_must_match_its_record(staged, path, field, value):
    merge_a(staged)
    pending = json.loads(log_lines(staged)[0])
    pending["record"][field] = value
    write_log(staged, [json.dumps(pending)])
    before = staged.merge_log_path.read_bytes()
    with pytest.raises(GraphJournalError, match="does not match its record"):
        reopen(path).load_graph_data()
    assert staged.merge_log_path.read_bytes() == before


def test_a_pending_merge_cannot_reopen_an_applied_id(staged, path):
    merge_a(staged)
    lines = log_lines(staged)
    write_log(staged, lines + [lines[0]])
    before = staged.merge_log_path.read_bytes()
    with pytest.raises(GraphJournalError, match="already applied"):
        reopen(path).load_graph_data()
    assert staged.merge_log_path.read_bytes() == before


@pytest.mark.parametrize("error", [OSError, KeyboardInterrupt])
def test_an_empty_merge_with_a_durable_pending_line_has_landed(
    staged, path, monkeypatch, error
):
    durable_then_raise(staged, monkeypatch, 1, error)
    if error is KeyboardInterrupt:
        with pytest.raises(error):
            staged.merge(A, "empty", [], [], [("node", "a")], {})
    else:
        result = staged.merge(A, "empty", [], [], [("node", "a")], {})
        assert result.record["status"] == ls.MERGE_APPLIED
        assert not result.replayed
    monkeypatch.undo()
    assert staged.load_merge("empty") is not None
    assert staged_keys(staged) == [("node", "b")]
    after = reopen(path)
    assert after.merge(A, "empty", [], [], [("node", "a")], {}).replayed
    assert staged_keys(after) == [("node", "b")]


def test_an_empty_merge_whose_pending_line_did_not_land_still_fails(
    staged, path, monkeypatch
):
    def broken(line):
        raise OSError("before durable")

    monkeypatch.setattr(staged, "_append_merge_log_locked", broken)
    with pytest.raises(OSError, match="before durable"):
        staged.merge(A, "empty", [], [], [("node", "a")], {})
    monkeypatch.undo()
    assert reopen(path).load_merge("empty") is None
    assert staged_keys(reopen(path)) == [("node", "a"), ("node", "b")]


@pytest.mark.parametrize("status", [ls.MERGE_PENDING, ls.MERGE_APPLIED, "aborted"])
def test_a_short_merge_log_write_with_a_failed_rollback_is_repaired_before_retry(
    staged, path, monkeypatch, status
):
    real_open = open
    failed = False

    class ShortWriter:
        def __init__(self, file):
            self.file = file

        def __getattr__(self, name):
            return getattr(self.file, name)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return self.file.__exit__(*exc)

        def write(self, data):
            nonlocal failed
            if not failed and json.loads(data)["status"] == status:
                failed = True
                return self.file.write(data[: len(data) // 2])
            return self.file.write(data)

    def short_open(target, mode="r", *args, **kwargs):
        file = real_open(target, mode, *args, **kwargs)
        if target == staged.merge_log_path and mode == "ab":
            return ShortWriter(file)
        return file

    def fail_truncate(fd, length):
        raise OSError("rollback failed")

    def fail_journal(ops, stored):
        raise OSError("journal failed")

    with monkeypatch.context() as patch:
        patch.setattr(storage_backends, "open", short_open, raising=False)
        patch.setattr(storage_backends.os, "ftruncate", fail_truncate)
        if status == "aborted":
            patch.setattr(staged, "_append_journal_locked", fail_journal)
        if status == ls.MERGE_APPLIED:
            merge_a(staged)
        else:
            with pytest.raises(OSError):
                merge_a(staged)
    assert failed
    assert not staged.merge_log_path.read_bytes().endswith(b"\n")
    staged.stage(A, [entry("node", "c")])
    after = reopen(path)
    landed = status == ls.MERGE_APPLIED
    assert name_of_a(after) == ("Merged" if landed else "A")
    assert (after.load_merge("m1") is not None) is landed
    expected = [("node", "b"), ("node", "c")]
    if not landed:
        expected.insert(0, ("node", "a"))
    assert staged_keys(after) == expected
    staged.stage(A, [entry("node", "a", session_seq=2)])
    result = merge_a(staged, "m2", "Later")
    assert reopen(path).load_merge("m2") == result.record
    assert name_of_a(reopen(path)) == "Later"
    assert staged.merge_log_path.read_bytes().endswith(b"\n")


@pytest.mark.parametrize("prior_write", [False, True])
def test_checkpoint_reloads_a_merge_interrupted_before_the_mirror_was_updated(
    staged, path, monkeypatch, prior_write
):
    if prior_write:
        staged.upsert_node(fresh(node_payload("c")))
    current = by_id(staged.load_graph_data(), "nodes")["a"]
    op = EntityOperation(
        "node",
        "upsert",
        "a",
        fresh({**current, "name": "Merged"}),
        expected_revision=current["revision"],
    )

    def interrupted(op):
        raise KeyboardInterrupt()

    monkeypatch.setattr(staged, "_apply", interrupted)
    with pytest.raises(KeyboardInterrupt):
        staged.merge(A, "m1", [op], [], [("node", "a")], {})
    monkeypatch.undo()
    staged.checkpoint()
    assert staged.journal_path.read_text() == ""
    after = reopen(path)
    assert name_of_a(after) == "Merged"
    assert after.load_merge("m1")["status"] == ls.MERGE_APPLIED
    assert staged_keys(after) == [("node", "b")]
    assert ("c" in by_id(after.load_graph_data(), "nodes")) is prior_write


def test_an_edge_upsert_journal_contains_only_the_persistent_operation_fields(staged):
    staged.apply_batch(
        [
            EntityOperation(
                "edge",
                "upsert",
                "e2",
                fresh(edge_payload("e2", "a", "b")),
                expect_absent=True,
                endpoint_types=(("a", "Actor"), ("b", "Actor")),
            )
        ]
    )
    batch = json.loads(staged.journal_path.read_text().splitlines()[-1])
    (operation,) = batch["ops"]
    assert set(operation) == {"kind", "action", "entity_id", "payload"}
    assert (operation["kind"], operation["action"], operation["entity_id"]) == (
        "edge",
        "upsert",
        "e2",
    )


# -- merge-record retention (§11.5) ---------------------------------------------

RETENTION = timedelta(days=90)


def _retire_long_ago(backend, session_id):
    """Retire `session_id`; a moment past its retention window."""
    backend.retire(session_id)
    return ls.utc_now() + RETENTION + timedelta(seconds=1)


def test_a_purge_rewrites_the_log_without_the_purged_lines(staged, path):
    staged.record_staged(B)
    staged.stage(B, [entry("node", "c", session_id=B)])
    staged.merge(B, "m-b", [], [], [("node", "c")], {})
    merge_a(staged)
    staged.discard(A)
    now = _retire_long_ago(staged, B)
    assert staged.purge_merge_records(RETENTION, limit=10, now=now) == 1
    assert "m-b" not in staged.merge_log_path.read_text()
    after = reopen(path)
    assert after.load_merge("m-b") is None
    assert after.load_merge("m1")["status"] == ls.MERGE_APPLIED
    # The rewritten log takes appends as the old one did.
    after.stage(A, [entry("node", "b")])
    after.merge(A, "m2", [], [], [("node", "b")], {})
    assert reopen(path).load_merge("m2") is not None


def test_a_pending_merge_that_cannot_settle_stops_the_purge(staged, path, monkeypatch):
    staged.record_staged(B)
    staged.stage(B, [entry("node", "c", session_id=B)])
    staged.merge(B, "m-b", [], [], [("node", "c")], {})
    now = _retire_long_ago(staged, B)
    real = staged._append_merge_log_locked
    calls = {"n": 0}

    def broken(line):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise OSError("disk gone")
        return real(line)

    monkeypatch.setattr(staged, "_append_merge_log_locked", broken)
    merge_a(staged)
    log_before = staged.merge_log_path.read_bytes()
    with pytest.raises(OSError):
        staged.purge_merge_records(RETENTION, limit=10, now=now)
    assert staged.merge_log_path.read_bytes() == log_before
    monkeypatch.undo()
    # Settled first, then purged; the merge that was pending is A's, kept.
    assert staged.purge_merge_records(RETENTION, limit=10, now=now) == 1
    after = reopen(path)
    assert after.load_merge("m1")["status"] == ls.MERGE_APPLIED
    assert after.load_merge("m-b") is None


def test_a_retired_record_without_a_time_starts_its_window_at_the_first_purge(
    staged, path
):
    merge_a(staged)
    staged.retire(A)
    layers = json.loads(staged.layers_path.read_text())
    del layers["sessions"][A]["retired_at"]
    staged.layers_path.write_text(json.dumps(layers))
    first = ls.utc_now() + timedelta(days=3650)
    assert reopen(path).purge_merge_records(RETENTION, limit=10, now=first) == 0
    assert reopen(path).load_merge("m1") is not None
    stamped = json.loads(staged.layers_path.read_text())["sessions"][A]
    assert ls.parse_retired_at(stamped["retired_at"]) == first
    later = first + RETENTION + timedelta(seconds=1)
    assert reopen(path).purge_merge_records(RETENTION, limit=10, now=later) == 1


@pytest.mark.parametrize("stored", ["not a time", "2026-01-01T00:00:00", 12])
def test_an_unreadable_retirement_time_stops_the_load(staged, path, stored):
    staged.discard(A)
    staged.retire(A)
    layers = json.loads(staged.layers_path.read_text())
    layers["sessions"][A]["retired_at"] = stored
    staged.layers_path.write_text(json.dumps(layers))
    with pytest.raises(GraphJournalError):
        reopen(path).staged_status(A)
