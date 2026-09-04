"""
History queries and compaction must not size their memory by the file.

Both used to load every record to answer a page of 50 or to trim: measured at
4.75 s and 429 MiB on a 79 MB sidecar, under an exclusive lock, in a container
with a 1 GiB limit that already holds the graph and the vector matrix. That is
an out-of-memory failure waiting for someone to open the history view.
"""

import json
import os
import tempfile
import tracemalloc
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.core.history_store import GraphHistoryStore, _iter_lines_reverse


def _record(entity_id: str, occurred_at: str = "2026-01-01T00:00:00Z", pad: int = 0):
    record = {
        "event_id": f"evt-{entity_id}",
        "entity_id": entity_id,
        "entity_kind": "node",
        "occurred_at": occurred_at,
    }
    if pad:
        record["payload"] = "x" * pad
    return record


@pytest.fixture
def store_path():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir) / "graph.history.ndjson"


def _write(path: Path, records) -> None:
    """Write records exactly as GraphHistoryStore.append_record would."""
    with open(path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


class TestReverseLineReader:
    """The reader underneath every query. A chunk boundary landing mid-line or
    mid-character is the whole risk here, so drive it at a chunk size of one."""

    @staticmethod
    def _reverse(raw: bytes, chunk: int = 64 * 1024):
        """Read `raw` backwards at the given chunk size.

        The chunk size is applied here rather than left to the caller: a helper
        that accepts the argument and ignores it makes every boundary case
        silently run at the default and pass regardless.
        """
        import backend.core.history_store as hs

        original = hs._REVERSE_CHUNK_BYTES
        hs._REVERSE_CHUNK_BYTES = chunk
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                path = Path(tmpdir) / "f"
                path.write_bytes(raw)
                with open(path, "rb") as f:
                    return list(_iter_lines_reverse(f))
        finally:
            hs._REVERSE_CHUNK_BYTES = original

    def test_matches_reading_forwards_and_reversing(self):
        raw = b"first\nsecond\nthird\n"

        assert self._reverse(raw) == [b"", b"third", b"second", b"first"]

    def test_handles_a_missing_trailing_newline(self):
        raw = b"first\nsecond\nthird"

        assert [line for line in self._reverse(raw) if line] == [
            b"third",
            b"second",
            b"first",
        ]

    def test_empty_file_yields_nothing(self):
        assert self._reverse(b"") == []

    @pytest.mark.parametrize("chunk", [1, 2, 3, 7, 64])
    def test_every_chunk_boundary_gives_the_same_answer(self, chunk):
        """A line split across chunks must be reassembled, not truncated."""
        raw = b"alpha\nbeta\ngamma\ndelta\n"

        assert [line for line in self._reverse(raw, chunk) if line] == [
            b"delta",
            b"gamma",
            b"beta",
            b"alpha",
        ]

    @pytest.mark.parametrize("chunk", [1, 3, 64])
    def test_a_missing_trailing_newline_at_any_chunk_size(self, chunk):
        raw = b"first\nsecond\nthird"

        assert [line for line in self._reverse(raw, chunk) if line] == [
            b"third",
            b"second",
            b"first",
        ]

    @pytest.mark.parametrize("chunk", [1, 2, 3, 7])
    def test_multibyte_characters_split_across_chunks_survive(self, chunk):
        """Holding back the leading fragment is what keeps a UTF-8 sequence
        whole; decoding a half character would raise instead."""
        raw = "Åland\nGöteborg\nMalmö\n".encode("utf-8")

        decoded = [line.decode("utf-8") for line in self._reverse(raw, chunk) if line]
        assert decoded == ["Malmö", "Göteborg", "Åland"]

    @pytest.mark.parametrize("chunk", [1, 64])
    def test_a_single_byte_file(self, chunk):
        assert [line for line in self._reverse(b"x", chunk) if line] == [b"x"]

    @pytest.mark.parametrize("chunk", [1, 2, 64])
    def test_a_file_of_only_newlines_yields_no_content(self, chunk):
        assert [line for line in self._reverse(b"\n\n\n", chunk) if line] == []

    @pytest.mark.parametrize("chunk", [1, 2, 3, 5, 64])
    def test_agrees_with_reading_forwards_and_reversing(self, chunk):
        """The property the whole change rests on, stated directly."""
        raw = b"a\nbb\nccc\n\ndddd\neeeee"
        forwards = [line for line in raw.split(b"\n") if line]

        backwards = [line for line in self._reverse(raw, chunk) if line]

        assert backwards == list(reversed(forwards))


class TestQueriesReadOnlyWhatTheyReturn:
    def test_recent_matches_the_whole_file_read_forwards(self, store_path):
        records = [_record(f"n{i}") for i in range(200)]
        _write(store_path, records)
        store = GraphHistoryStore(store_path)

        page = store.get_recent(limit=5)

        assert [r["entity_id"] for r in page] == [
            "n199",
            "n198",
            "n197",
            "n196",
            "n195",
        ]

    def test_pagination_offset_walks_backwards(self, store_path):
        _write(store_path, [_record(f"n{i}") for i in range(20)])
        store = GraphHistoryStore(store_path)

        assert [r["entity_id"] for r in store.get_recent(limit=2, offset=3)] == [
            "n16",
            "n15",
        ]

    def test_entity_history_filters_while_streaming(self, store_path):
        records = []
        for i in range(60):
            records.append(_record("target" if i % 10 == 0 else f"other{i}"))
        _write(store_path, records)
        store = GraphHistoryStore(store_path)

        page = store.get_entity_history("target", limit=3)

        assert len(page) == 3
        assert all(r["entity_id"] == "target" for r in page)

    def test_entity_kind_still_separates_a_node_from_an_edge(self, store_path):
        node = _record("shared")
        edge = dict(_record("shared"), entity_kind="edge")
        _write(store_path, [node, edge])
        store = GraphHistoryStore(store_path)

        assert len(store.get_entity_history("shared", kind="node")) == 1
        assert len(store.get_entity_history("shared", kind="edge")) == 1

    def test_a_malformed_line_is_skipped_not_fatal(self, store_path):
        with open(store_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(_record("good-1")) + "\n")
            f.write("{ this is not json\n")
            f.write(json.dumps(_record("good-2")) + "\n")
        store = GraphHistoryStore(store_path)

        assert [r["entity_id"] for r in store.get_recent(limit=10)] == [
            "good-2",
            "good-1",
        ]

    def test_a_missing_file_returns_nothing(self, store_path):
        store = GraphHistoryStore(store_path)

        assert store.get_recent(limit=10) == []
        assert store.get_entity_history("anything") == []

    def test_a_zero_limit_reads_nothing(self, store_path):
        _write(store_path, [_record(f"n{i}") for i in range(10)])
        store = GraphHistoryStore(store_path)

        assert store.get_recent(limit=0) == []


class TestMemoryIsBoundedByThePageNotTheFile:
    """The point of the change, stated as a measurement rather than a claim."""

    @staticmethod
    def _big_history(path: Path, count: int, pad: int) -> None:
        with open(path, "w", encoding="utf-8") as f:
            for i in range(count):
                f.write(json.dumps(_record(f"n{i}", pad=pad)) + "\n")

    def test_a_page_of_fifty_does_not_allocate_the_whole_file(self, store_path):
        # ~4 MB of history; the old implementation parsed all of it per query.
        self._big_history(store_path, count=4000, pad=1000)
        file_size = store_path.stat().st_size
        assert file_size > 3_000_000, "fixture is too small to prove anything"

        store = GraphHistoryStore(store_path)
        tracemalloc.start()
        try:
            page = store.get_recent(limit=50)
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        assert len(page) == 50
        # Honest bound: one 64 KB chunk plus 50 records of ~1 kB, with room for
        # interpreter overhead. A quarter of the file would also catch a full
        # reload, but would let a few-hundred-kB regression through.
        assert peak < 400_000, (
            f"peak allocation {peak} is not bounded by the page: the query is "
            f"still sized by the {file_size}-byte file"
        )

    def test_compaction_does_not_allocate_the_whole_file(self, store_path):
        self._big_history(store_path, count=4000, pad=1000)
        file_size = store_path.stat().st_size

        store = GraphHistoryStore(store_path, max_events=100, compaction_interval=1000)
        tracemalloc.start()
        try:
            store.compact()
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        assert len(store.get_recent(limit=1000)) == 100
        assert peak < 400_000, (
            f"peak allocation {peak} is not bounded: compaction still loads the "
            f"{file_size}-byte file"
        )


class TestCompactionKeepsWhatRetentionSays:
    def test_records_are_written_through_byte_for_byte(self, store_path):
        """A trim decides which records to keep; it must not alter them. Writing
        the original line through is what guarantees that."""
        kept = [_record(f"n{i}") for i in range(5)]
        for record in kept:
            record["nested"] = {"unicode": "Göteborg", "number": 1.5}
        _write(store_path, kept)

        store = GraphHistoryStore(store_path, max_events=3, compaction_interval=1000)
        store.compact()

        # Compared as raw lines, not parsed values: re-serialising a record
        # parses back equal while changing the bytes on disk (escaping, key
        # order, separators), so only a byte comparison shows a write-through.
        expected_lines = [
            json.dumps(record, ensure_ascii=False) for record in kept[-3:]
        ]
        assert store_path.read_text().splitlines() == expected_lines

    def test_age_and_count_caps_apply_together(self, store_path):
        now = datetime.now(timezone.utc)
        records = [
            _record("old-1", (now - timedelta(days=40)).isoformat()),
            _record("old-2", (now - timedelta(days=35)).isoformat()),
            _record("new-1", (now - timedelta(days=2)).isoformat()),
            _record("new-2", (now - timedelta(days=1)).isoformat()),
            _record("new-3", now.isoformat()),
        ]
        _write(store_path, records)

        store = GraphHistoryStore(
            store_path, max_events=2, max_age_days=30, compaction_interval=1000
        )
        store.compact()

        assert [r["entity_id"] for r in store.get_recent(limit=10)] == [
            "new-3",
            "new-2",
        ]

    def test_a_record_with_an_unparseable_timestamp_is_kept(self, store_path):
        _write(
            store_path,
            [
                _record("bad-ts", "not a timestamp"),
                _record("fine", "2026-01-01T00:00:00Z"),
            ],
        )

        store = GraphHistoryStore(store_path, max_age_days=1, compaction_interval=1000)
        store.compact()

        assert {r["entity_id"] for r in store.get_recent(limit=10)} == {"bad-ts"}

    def test_nothing_is_rewritten_when_nothing_needs_dropping(self, store_path):
        _write(store_path, [_record(f"n{i}") for i in range(3)])
        store = GraphHistoryStore(store_path, max_events=10, compaction_interval=1000)
        before = store_path.stat()

        store.compact()

        after = store_path.stat()
        assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)

    def test_a_failed_rewrite_leaves_the_sidecar_and_no_temp_files(
        self, store_path, monkeypatch
    ):
        _write(store_path, [_record(f"n{i}") for i in range(5)])
        store = GraphHistoryStore(store_path, max_events=2, compaction_interval=1000)
        before = store_path.read_text()

        import backend.core.history_store as hs

        def boom(*args, **kwargs):
            raise OSError("simulated rename failure")

        monkeypatch.setattr(hs.os, "rename", boom)
        monkeypatch.setattr(hs.os, "replace", boom)

        with pytest.raises(OSError):
            store.compact()

        assert store_path.read_text() == before
        leftovers = [
            name
            for name in os.listdir(store_path.parent)
            if name.startswith("graph_history_")
        ]
        assert leftovers == []


class TestTornBytesDoNotDisableRetention:
    """A crash during an append leaves a line cut mid-character. If that raises
    out of compaction, append_record swallows it and retries every interval, so
    retention silently stops running and the sidecar grows without bound — the
    exact condition retention exists to prevent."""

    @staticmethod
    def _with_torn_line(path: Path, records) -> None:
        with open(path, "wb") as f:
            # A UTF-8 sequence cut in half, as an interrupted write leaves it:
            # 0xC3 is the lead byte of "ö" with its continuation byte missing.
            # Truncating after a complete character would still be valid UTF-8
            # and would prove nothing.
            torn = "Gö".encode("utf-8")[:-1]
            assert torn.decode("utf-8", errors="ignore") != torn.decode(
                "utf-8", errors="replace"
            ), "fixture is not actually invalid UTF-8"
            f.write(torn + b"\n")
            for record in records:
                f.write(json.dumps(record, ensure_ascii=False).encode("utf-8") + b"\n")

    def test_compaction_survives_a_torn_line(self, store_path):
        self._with_torn_line(store_path, [_record(f"n{i}") for i in range(5)])
        store = GraphHistoryStore(store_path, max_events=2, compaction_interval=1000)

        store.compact()

        assert [r["entity_id"] for r in store.get_recent(limit=10)] == ["n4", "n3"]

    def test_retention_keeps_running_after_a_torn_line(self, store_path):
        """append_record swallows a compaction failure, so a raise here shows up
        only as a sidecar that never gets trimmed again."""
        self._with_torn_line(store_path, [])
        store = GraphHistoryStore(store_path, max_events=2, compaction_interval=1)

        for i in range(6):
            store.append_record(_record(f"n{i}"))

        surviving = [
            line for line in store_path.read_text(errors="replace").splitlines() if line
        ]
        assert len(surviving) == 2, "retention stopped running"

    def test_queries_survive_a_torn_line(self, store_path):
        self._with_torn_line(store_path, [_record("good")])
        store = GraphHistoryStore(store_path)

        assert [r["entity_id"] for r in store.get_recent(limit=10)] == ["good"]
        assert [r["entity_id"] for r in store.get_entity_history("good")] == ["good"]


class TestLockOrdering:
    """The module documents 'an exclusive OS file lock plus an in-process lock'.
    A page walk stops mid-stream, so the reader has to be closed explicitly:
    left to refcounting, the file lock outlives the in-process lock, and an
    exception during the walk can strand it entirely."""

    @staticmethod
    def _record_order(store, call, monkeypatch):
        import backend.core.history_store as hs

        order = []
        real_unlock = hs._unlock_file
        monkeypatch.setattr(
            hs,
            "_unlock_file",
            lambda f: (order.append("file_unlock"), real_unlock(f))[1],
        )

        real_lock = store._lock

        class _Recording:
            def __enter__(self):
                return real_lock.__enter__()

            def __exit__(self, *exc):
                order.append("self_lock_release")
                return real_lock.__exit__(*exc)

        store._lock = _Recording()
        call()
        return order

    def test_the_file_lock_is_released_inside_the_in_process_lock(
        self, store_path, monkeypatch
    ):
        _write(store_path, [_record(f"n{i}") for i in range(20)])
        store = GraphHistoryStore(store_path)

        order = self._record_order(
            store, lambda: store.get_recent(limit=2), monkeypatch
        )

        assert order == ["file_unlock", "self_lock_release"]

    def test_entity_history_releases_in_the_same_order(self, store_path, monkeypatch):
        """The generator expression here outlives the lock block unless the
        underlying stream is closed explicitly, which inverted the order."""
        _write(store_path, [_record("target") for _ in range(20)])
        store = GraphHistoryStore(store_path)

        order = self._record_order(
            store, lambda: store.get_entity_history("target", limit=2), monkeypatch
        )

        assert order == ["file_unlock", "self_lock_release"]


def test_a_naive_timestamp_does_not_break_age_retention(store_path):
    """Records written before timestamps carried a timezone are still on disk.
    Comparing one against an aware cutoff raises, and that raise would escape
    compaction the same way a torn line does."""
    _write(
        store_path,
        [
            _record("naive", "2026-01-01T00:00:00"),
            _record("recent", datetime.now(timezone.utc).isoformat()),
        ],
    )
    store = GraphHistoryStore(store_path, max_age_days=30, compaction_interval=1000)

    store.compact()

    assert [r["entity_id"] for r in store.get_recent(limit=10)] == ["recent"]


class TestTheRewriteStaysOnTheSidecarsFilesystem:
    """os.rename is atomic only within a filesystem. The graph directory is a
    mounted volume in both deployment shapes while TMPDIR is the container's own
    disk, so a temp file created in TMPDIR makes every compaction raise EXDEV —
    which append_record swallows, leaving retention permanently dead.

    The existing "no temp files left behind" test cannot see this: it asserts
    nothing named graph_history_* remains beside the sidecar, which is vacuously
    true when none is ever created there."""

    def test_the_temp_file_is_created_beside_the_sidecar(self, store_path, monkeypatch):
        _write(store_path, [_record(f"n{i}") for i in range(5)])
        store = GraphHistoryStore(store_path, max_events=2, compaction_interval=1000)

        seen = []
        real_mkstemp = tempfile.mkstemp

        def spy(*args, **kwargs):
            fd, path = real_mkstemp(*args, **kwargs)
            seen.append(path)
            return fd, path

        monkeypatch.setattr(tempfile, "mkstemp", spy)
        store.compact()

        assert seen, "compaction created no temp file"
        assert os.path.dirname(seen[0]) == str(store_path.parent), (
            f"temp file was created in {os.path.dirname(seen[0])}, not beside the "
            f"sidecar in {store_path.parent}; os.rename across filesystems raises "
            f"EXDEV and retention would stop running"
        )

    def test_compaction_survives_when_tmpdir_is_a_different_filesystem(
        self, store_path, monkeypatch
    ):
        """Simulates the deployment shape directly: rename refuses to cross
        between the temp directory and the graph volume."""
        _write(store_path, [_record(f"n{i}") for i in range(6)])
        store = GraphHistoryStore(store_path, max_events=2, compaction_interval=1000)

        import backend.core.history_store as hs

        real_rename = os.rename

        def rename_refusing_to_cross(src, dst):
            if os.path.dirname(src) != os.path.dirname(dst):
                raise OSError(18, "Invalid cross-device link")
            return real_rename(src, dst)

        monkeypatch.setattr(hs.os, "rename", rename_refusing_to_cross)
        monkeypatch.setattr(hs.os, "replace", rename_refusing_to_cross)

        store.compact()

        assert [r["entity_id"] for r in store.get_recent(limit=10)] == ["n5", "n4"]


class TestCompactionThrottleScalesWithTheHistory:
    """A compaction pass reads and rewrites the whole sidecar while holding the
    store lock. A constant throttle therefore makes the work per mutation grow
    with the file — the same 'cost sized by the file, not by the work' this
    change removes from the read path, relocated to the write path."""

    @staticmethod
    def _interval(**kwargs) -> int:
        with tempfile.TemporaryDirectory() as tmpdir:
            store = GraphHistoryStore(Path(tmpdir) / "h.ndjson", **kwargs)
            return store._compaction_interval

    @pytest.mark.parametrize(
        "cap", [10, 100, 256, 257, 1000, 2559, 2560, 10_000, 100_000, 1_000_000]
    )
    def test_the_interval_is_a_tenth_of_the_cap_at_every_size(self, cap):
        """Uniform, with no band where a floor takes over — a floor is what made
        the documented 'tenth of the cap' false below 2560, and let a small cap
        overshoot to 200% while promising 110%."""
        assert self._interval(max_events=cap) == cap // 10

    def test_the_work_per_append_stays_flat_as_the_cap_grows(self):
        """records rewritten per append = cap / interval, held constant."""
        ratios = [
            cap / self._interval(max_events=cap) for cap in (10_000, 100_000, 1_000_000)
        ]

        assert max(ratios) <= 11, f"per-append work grows with the cap: {ratios}"

    def test_a_tiny_cap_never_drops_below_one(self):
        assert self._interval(max_events=5) == 1
        assert self._interval(max_events=1) == 1

    def test_an_explicit_interval_still_wins(self):
        assert self._interval(max_events=100_000, compaction_interval=7) == 7

    def test_age_only_retention_scales_off_what_the_last_pass_kept(self):
        """With no count cap there is nothing to derive the interval from, so a
        constant would put the per-mutation cost back in proportion to the
        history. The interval must follow the file instead."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "h.ndjson"
            recent = datetime.now(timezone.utc).isoformat()
            _write(path, [_record(f"n{i}", recent) for i in range(5000)])
            store = GraphHistoryStore(path, max_age_days=30)

            before = store._compaction_interval
            store.compact()
            after = store._compaction_interval

        assert before == 256, "pre-measurement fallback changed"
        assert after == 500, (
            f"interval stayed at {after} for a 5000-record age-only history; "
            f"the per-mutation cost then grows with the file"
        )


class TestCompactionRunsAsOftenAsTheThrottleSays:
    """The interval is only meaningful if it is actually applied. Asserting its
    value cannot see a counter that never resets, or one compared the wrong way
    — both of which make every append compact."""

    @staticmethod
    def _count_passes(store, appends: int) -> int:
        passes = []
        real = store._rewrite_streaming

        def counting(cutoff, skip):
            passes.append(1)
            return real(cutoff, skip)

        store._rewrite_streaming = counting
        for i in range(appends):
            store.append_record(_record(f"n{i}"))
        return len(passes)

    def test_passes_are_throttled_not_run_on_every_append(self, store_path):
        store = GraphHistoryStore(store_path, max_events=50, compaction_interval=10)

        passes = self._count_passes(store, 200)

        # 200 appends at one pass per 10 is ~20, and certainly not ~200.
        assert passes <= 25, f"{passes} compaction passes for 200 appends"
        assert passes >= 10, f"only {passes} passes; retention is barely running"

    def test_the_counter_resets_after_a_pass(self, store_path):
        """Without the reset the counter stays above the threshold forever, so
        every later append triggers a full rewrite."""
        store = GraphHistoryStore(store_path, max_events=20, compaction_interval=5)
        self._count_passes(store, 40)

        assert store._appends_since_compaction < store._compaction_interval

    def test_the_file_never_exceeds_the_cap_plus_one_interval(self, store_path):
        """Sampled after every append against a literal bound. A bound computed
        from the store's own interval passes for any interval the code picks,
        including one that disables compaction altogether."""
        store = GraphHistoryStore(store_path, max_events=50, compaction_interval=10)

        peak = 0
        for i in range(300):
            store.append_record(_record(f"n{i}"))
            lines = [ln for ln in store_path.read_text().splitlines() if ln]
            peak = max(peak, len(lines))

        assert peak <= 60, f"sidecar reached {peak} records against a cap of 50 + 10"
        assert peak > 50, "compaction never ran, so the bound proves nothing"
