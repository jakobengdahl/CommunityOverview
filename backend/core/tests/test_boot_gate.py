"""The boot window: what a report that arrives before the first load costs.

`GraphStorage` used to load and then start change notification. A write
another instance committed between those two moments was announced on a
channel nobody was listening to, and no transport here replays: narrow in
time, unbounded in consequence, because an entity written in the gap and
never written again was never reported and the instance served the stale
value for as long as it ran.

It now starts listening first and holds what arrives until the load has
returned. These tests pin both halves — that nothing is lost, and that
nothing is delivered against a model that does not exist yet, which is what
the original ordering existed to guarantee.
"""

import threading
import time

import pytest

from backend.core import storage as storage_module
from backend.core.storage import _BOOT_BUFFER_LIMIT, GraphStorage, _BootGate
from backend.core.storage_backends import (
    BackendCapabilities,
    EntityOperation,
    ExternalChange,
)

from .persistence_contract import InMemoryGraphPersistenceBackend


def _change(name: str) -> ExternalChange:
    """A report that carries its own name, so a replay's ORDER is readable.

    `ExternalChange` is frozen, so the name rides on an operation rather than
    on an attribute. A delete of an id no graph holds applies as a no-op,
    which keeps these usable against a real `apply_external_change`.
    """
    return ExternalChange.entities([EntityOperation.delete_node(name)])


def _names(changes) -> list:
    return [c.operations[0].entity_id for c in changes]


class _YieldingLock:
    """A `threading.Lock` that gives up the interpreter before each acquire."""

    def __init__(self):
        self._inner = threading.Lock()

    def acquire(self, blocking=True, timeout=-1):
        time.sleep(0)
        return self._inner.acquire(blocking, timeout)

    def release(self):
        self._inner.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


def _free_to_another_thread(lock) -> bool:
    """Whether a DIFFERENT thread could take `lock` right now.

    Probed from a thread of its own because an `RLock` held by the caller
    would grant a same-thread acquire and hide exactly what this looks for.
    """
    got = []

    def probe():
        acquired = lock.acquire(blocking=False)
        if acquired:
            lock.release()
        got.append(acquired)

    prober = threading.Thread(target=probe)
    prober.start()
    prober.join()
    return got[0]


class TestTheGateHoldsAndReplays:
    def test_a_report_before_open_is_not_delivered(self):
        seen = []
        gate = _BootGate(seen.append)

        gate(_change("during-load"))

        assert seen == [], (
            "a report was delivered before the load returned, which is what "
            "the seam's original ordering existed to prevent"
        )

    def test_open_replays_what_was_held(self):
        seen = []
        gate = _BootGate(seen.append)
        first, second = _change("first"), _change("second")

        gate(first)
        gate(second)
        gate.open()

        assert _names(seen) == ["first", "second"]

    def test_a_report_after_open_goes_straight_through(self):
        seen = []
        gate = _BootGate(seen.append)
        gate.open()

        gate(_change("after"))

        assert _names(seen) == ["after"]

    def test_a_report_arriving_mid_replay_does_not_overtake_the_held_ones(self):
        """The ordering property, and the reason `open` loops.

        Raising the flag before draining would let a report that arrives
        during the drain be delivered ahead of reports that were held before
        it — the store applied them in the other order, so the replay would
        contradict the store.
        """
        seen = []
        gate = _BootGate(None)  # set below; the listener needs the gate
        arrived_late = _change("late")
        fired = []

        def listener(change):
            seen.append(change)
            # Exactly once, while the first held report is being delivered.
            if not fired:
                fired.append(True)
                # Checked first because re-entering a gate that drains under
                # its own plain Lock deadlocks, and a hang reports nothing.
                assert _free_to_another_thread(gate._lock), (
                    "the replay called the listener with the gate lock held"
                )
                gate(arrived_late)

        gate._listener = listener
        gate(_change("held-1"))
        gate(_change("held-2"))
        gate.open()

        assert _names(seen) == ["held-1", "held-2", "late"]

    def test_the_gate_is_open_after_the_replay(self):
        seen = []
        gate = _BootGate(seen.append)
        gate(_change("held"))
        gate.open()
        seen.clear()

        gate(_change("later"))

        assert _names(seen) == ["later"], (
            "the gate did not stay open after replaying, so every later "
            "report would be buffered and never delivered"
        )

    def test_a_second_open_delivers_nothing_again(self):
        seen = []
        gate = _BootGate(seen.append)
        gate(_change("held"))
        gate.open()
        seen.clear()

        gate.open()

        assert seen == [], "a second open() replayed what the first already had"


class TestTheBufferIsBounded:
    def test_past_the_limit_the_held_reports_become_one_reload(self):
        seen = []
        gate = _BootGate(seen.append)

        for i in range(_BOOT_BUFFER_LIMIT + 1):
            gate(_change(f"held-{i}"))
        gate.open()

        assert len(seen) == 1, (
            f"expected one reload to subsume {_BOOT_BUFFER_LIMIT + 1} held "
            f"reports, got {len(seen)} deliveries"
        )
        assert seen[0].operations is None and not seen[0].content_read_on_demand(), (
            "overflow must degrade to unknown(), the whole-graph reload — "
            "anything else drops writes on the floor"
        )

    def test_overflow_does_not_keep_buffering_after_it_trips(self):
        """Once it has overflowed, holding more reports only costs memory:
        the reload already covers them."""
        gate = _BootGate(lambda change: None)

        for i in range(_BOOT_BUFFER_LIMIT + 1):
            gate(_change(f"held-{i}"))
        gate(_change("after-overflow"))

        assert gate._held == []

    def test_a_report_after_an_overflowed_open_still_goes_through(self):
        seen = []
        gate = _BootGate(seen.append)
        for i in range(_BOOT_BUFFER_LIMIT + 1):
            gate(_change(f"held-{i}"))
        gate.open()
        seen.clear()

        gate(_change("later"))

        assert _names(seen) == ["later"]

    def test_the_overflow_is_announced_once(self, capsys):
        gate = _BootGate(lambda change: None)

        for i in range(_BOOT_BUFFER_LIMIT + 3):
            gate(_change(f"held-{i}"))

        out = capsys.readouterr().out
        assert out.count("dropping them for a whole-graph reload") == 1, (
            "an overflowed boot must warn exactly once: the reload replacing "
            f"the dropped reports is best-effort. Output was: {out!r}"
        )

    def test_an_overflow_during_the_drain_ends_in_one_reload(self, monkeypatch):
        """Reports that arrive mid-drain are held, so they can overflow too.

        The held ones already delivered stand; the late ones are subsumed by
        a reload that must still be requested, after them, and the gate must
        end up open.
        """
        monkeypatch.setattr(storage_module, "_BOOT_BUFFER_LIMIT", 2)
        seen = []
        gate = _BootGate(None)

        def listener(change):
            seen.append(change)
            if len(seen) == 1:
                assert _free_to_another_thread(gate._lock), (
                    "the replay called the listener with the gate lock held"
                )
                for i in range(3):
                    gate(_change(f"late-{i}"))

        gate._listener = listener
        gate(_change("held-0"))
        gate(_change("held-1"))
        gate.open()

        assert _names(seen[:2]) == ["held-0", "held-1"]
        assert len(seen) == 3, (
            f"expected the two held reports and one reload, got {len(seen)}"
        )
        assert seen[2].operations is None and not seen[2].content_read_on_demand(), (
            "an overflow during the drain must still degrade to unknown()"
        )

        gate(_change("later"))
        assert _names(seen[3:]) == ["later"], "the gate stayed shut after the reload"

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "open() re-requests the reload on every call after an overflow; "
            "harmless today because GraphStorage opens exactly once"
        ),
    )
    def test_a_second_open_after_an_overflow_requests_no_second_reload(self):
        seen = []
        gate = _BootGate(seen.append)
        for i in range(_BOOT_BUFFER_LIMIT + 1):
            gate(_change(f"held-{i}"))
        gate.open()
        seen.clear()

        gate.open()

        assert seen == []


class TestTheListenerRunsOutsideTheGateLock:
    """The gate must never hold its own lock while it calls the listener.

    The listener is `apply_external_change`, which takes the storage lock and
    can wait on the write queue. Holding the gate lock across it is a
    lock-ordering inversion against any thread that reports while holding
    storage state - a deadlock that no test here would otherwise see unless
    it happened to re-enter the gate from inside the listener.
    """

    def _gate_recording_lock_state(self):
        free = []
        gate = _BootGate(None)
        gate._listener = lambda change: free.append(_free_to_another_thread(gate._lock))
        return gate, free

    def test_a_pass_through_delivery_does_not_hold_the_lock(self):
        gate, free = self._gate_recording_lock_state()
        gate.open()

        gate(_change("after"))

        assert free == [True], "the listener was called with the gate lock held"

    def test_the_replay_does_not_hold_the_lock(self):
        gate, free = self._gate_recording_lock_state()
        gate(_change("held-1"))
        gate(_change("held-2"))

        gate.open()

        assert free == [True, True], (
            "the replay called the listener with the gate lock held"
        )

    def test_the_overflow_reload_does_not_hold_the_lock(self, monkeypatch):
        monkeypatch.setattr(storage_module, "_BOOT_BUFFER_LIMIT", 2)
        gate, free = self._gate_recording_lock_state()
        for i in range(3):
            gate(_change(f"held-{i}"))

        gate.open()

        assert free == [True], (
            "the overflow reload was requested with the gate lock held"
        )


class _NotifyingDuringLoad(InMemoryGraphPersistenceBackend):
    """A backend that reports a change while the application is loading.

    This is the boot window made reproducible: the report is dispatched from
    inside `load_graph_data`, which is exactly the moment the old ordering
    was not listening in.
    """

    def __init__(self, store=None, **kwargs):
        # `written` decides `exists()`, and an empty store bootstraps instead
        # of loading - which would skip `load_graph_data` and with it the
        # window this fixture exists to reproduce.
        super().__init__({"written": True} if store is None else store, **kwargs)
        self._listener = None
        self.reported = []
        self.stopped = False

    def capabilities(self) -> BackendCapabilities:
        return BackendCapabilities(
            incremental_writes=False,
            transactions=False,
            change_notification=True,
            store_traversal=False,
        )

    def start_change_notification(self, listener) -> None:
        self._listener = listener

    def stop_change_notification(self) -> None:
        self.stopped = True
        self._listener = None

    reports_during_load = 1
    load_raises = None

    def load_graph_data(self):
        if self._listener is not None:
            for i in range(self.reports_during_load):
                change = _change(f"committed-during-load-{i}")
                self.reported.append(change)
                self._listener(change)
        if self.load_raises is not None:
            raise self.load_raises
        return super().load_graph_data()


def _spy_on_applies(monkeypatch):
    """Record, for every report applied, whether the load was still running."""
    applied = []
    real_load = GraphStorage.load

    def spy_apply(self, change):
        applied.append((self._loading_now, change))

    def load_marking(self, *args, **kwargs):
        self._loading_now = True
        try:
            return real_load(self, *args, **kwargs)
        finally:
            self._loading_now = False

    monkeypatch.setattr(GraphStorage, "apply_external_change", spy_apply)
    monkeypatch.setattr(GraphStorage, "load", load_marking)
    monkeypatch.setattr(GraphStorage, "_loading_now", False, raising=False)
    return applied


class TestGraphStorageClosesTheWindow:
    def test_a_change_reported_during_the_load_is_not_lost(self):
        """The regression test for the window itself.

        Against the old ordering the backend has no listener to call at all
        while `load_graph_data` runs, so this report is never made — which is
        the defect: the write it describes is never seen by this instance.
        """
        backend = _NotifyingDuringLoad()
        storage = GraphStorage(persistence_backend=backend)
        try:
            # Assert on the backend's side: it had someone to report to at the
            # moment it reported. Against the old ordering `self._listener` is
            # still None there, so `reported` stays empty and the write the
            # report describes is never seen by this instance.
            assert backend.reported, (
                "the backend was not listening while the application loaded, "
                "so a write committed in that window is announced to nobody "
                "and never replayed"
            )
        finally:
            storage.shutdown_events()

    def test_the_report_is_applied_only_after_the_load(self, monkeypatch):
        """The other half: held, not merely received.

        A report delivered to `apply_external_change` mid-load would run
        against a half-built model — the thing the seam's original ordering
        was protecting.
        """
        backend = _NotifyingDuringLoad()
        applied = _spy_on_applies(monkeypatch)

        storage = GraphStorage(persistence_backend=backend)
        try:
            seen_during_load = [during for during, _ in applied]
            assert seen_during_load == [False], (
                f"expected one report applied after the load, got "
                f"{seen_during_load} (True means it landed mid-load)"
            )
        finally:
            storage.shutdown_events()

    def test_a_replay_that_raises_stops_notification(self, monkeypatch):
        """The replay is inside the construction guard, not after it.

        A raise out of `open()` leaves the rest of the buffer undelivered and
        the gate shut, so every later report from the still-live backend
        thread buffers into an object nobody will ever open. It is also the
        one delivery the backend's own `except` around the listener does not
        cover, because the backend is no longer on the stack. Failing
        construction with the listener stopped is the honest outcome.
        """
        backend = _NotifyingDuringLoad()

        def angry(self, change):
            raise RuntimeError("replay failed")

        monkeypatch.setattr(GraphStorage, "apply_external_change", angry)

        with pytest.raises(RuntimeError, match="replay failed"):
            GraphStorage(persistence_backend=backend)

        assert backend.stopped, (
            "the replay raised and construction failed with the listener "
            "still running - a worse leak than the one the guard was "
            "written for"
        )

    @pytest.mark.parametrize("exc_type", [RuntimeError, KeyboardInterrupt, SystemExit])
    def test_a_failed_load_stops_notification(self, monkeypatch, exc_type):
        """Otherwise the listener outlives the object it reports into, holding
        its connection and refreshing something nothing will shut down.

        An interrupt mid-load is the likeliest way a boot fails in practice,
        and it is not an `Exception`."""
        backend = _NotifyingDuringLoad()

        def boom(self, *args, **kwargs):
            raise exc_type("load failed")

        monkeypatch.setattr(GraphStorage, "load", boom)

        with pytest.raises(exc_type, match="load failed"):
            GraphStorage(persistence_backend=backend)

        assert backend.stopped, "construction failed with the listener still running"

    def test_a_failed_load_without_notification_raises_its_own_error(
        self, monkeypatch, tmp_path
    ):
        """The file backend has no listener to stop, and no
        `stop_change_notification` either: the guard must not reach for it and
        bury the load's error under an AttributeError."""

        def boom(self, *args, **kwargs):
            raise RuntimeError("load failed")

        monkeypatch.setattr(GraphStorage, "load", boom)

        with pytest.raises(RuntimeError, match="load failed"):
            GraphStorage(json_path=str(tmp_path / "graph.json"))


class TestGraphStorageNeverAppliesMidLoad:
    def test_an_overflowed_boot_becomes_one_reload_after_the_load(self, monkeypatch):
        """No GraphStorage-level test overflowed before, and the gate-level
        ones never check WHEN delivery happens. An overflowed gate that
        delivered instead of holding would apply reports against a half-built
        model."""
        monkeypatch.setattr(storage_module, "_BOOT_BUFFER_LIMIT", 2)
        applied = _spy_on_applies(monkeypatch)
        backend = _NotifyingDuringLoad()
        # Past the limit AND past the point it trips, so the overflowed
        # branch itself is exercised, not just the transition into it.
        backend.reports_during_load = 5

        storage = GraphStorage(persistence_backend=backend)
        try:
            assert len(backend.reported) == 5
            assert [during for during, _ in applied] == [False], (
                f"expected one reload applied after the load, got "
                f"{[during for during, _ in applied]} (True means mid-load)"
            )
            change = applied[0][1]
            assert change.operations is None and not change.content_read_on_demand(), (
                "an overflowed boot must degrade to unknown(), the whole-graph reload"
            )
        finally:
            storage.shutdown_events()

    def test_a_report_held_by_a_load_that_fails_is_never_applied(self, monkeypatch):
        """The failure path with something actually held.

        The other failure-path test replaces `load` outright, so the buffer
        is always empty there. A construction handler that drained what was
        held before stopping would apply it against a model that failed to
        load.
        """
        applied = _spy_on_applies(monkeypatch)
        backend = _NotifyingDuringLoad()
        backend.load_raises = RuntimeError("load failed after a report")

        with pytest.raises(RuntimeError, match="load failed after a report"):
            GraphStorage(persistence_backend=backend)

        assert backend.reported, "the fixture did not hold anything"
        assert applied == [], (
            "a report held during a load that then failed was applied anyway"
        )
        assert backend.stopped, "construction failed with the listener still running"


class TestTheGateIsThreadSafe:
    def test_concurrent_reports_are_all_replayed(self):
        """The backend reports from a thread of its own, and nothing says one
        thread: a poller and a reconnect can both be live at once."""
        seen = []
        gate = _BootGate(seen.append)
        threads = [
            threading.Thread(target=gate, args=(_change(f"t{i}"),)) for i in range(50)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        gate.open()

        assert sorted(_names(seen)) == sorted(f"t{i}" for i in range(50))

    def test_no_report_is_lost_when_open_races_a_reporting_thread(self):
        """Every report sent while `open()` runs is delivered exactly once, in
        the order it was sent.

        The concurrent test above joins every thread BEFORE opening, so no
        report is ever in flight during the drain. Here one thread reports
        continuously while another opens the gate partway through. A gate
        that read its flag outside the lock could decide "closed", lose the
        race to `open()`, and then buffer into a list nobody drains again.
        """
        trials, per_trial = 300, 40
        for trial in range(trials):
            seen = []

            def listener(change, seen=seen):
                # Yield mid-drain so a report let through by a flag raised
                # too early can overtake the held ones; a drain that finishes
                # within one interpreter slice would hide that reorder.
                time.sleep(0)
                seen.append(change)

            gate = _BootGate(listener)
            # The lost-report race sits between a flag read and the lock
            # acquire - a few bytecodes, which the interpreter almost never
            # switches inside on its own. Yielding at every acquire puts a
            # switch exactly there, so the race is exercised on most trials.
            gate._lock = _YieldingLock()
            sent = [_change(f"r{trial}-{i}") for i in range(per_trial)]
            halfway = threading.Event()

            def report(gate=gate, sent=sent, halfway=halfway):
                for i, change in enumerate(sent):
                    if i == per_trial // 2:
                        halfway.set()
                    gate(change)

            reporter = threading.Thread(target=report)
            reporter.start()
            assert halfway.wait(timeout=5), (
                f"trial {trial}: the reporter never got halfway"
            )
            gate.open()
            reporter.join(timeout=5)
            assert not reporter.is_alive(), (
                f"trial {trial}: the reporter did not finish"
            )

            assert _names(seen) == _names(sent), (
                f"trial {trial}: delivered {len(seen)} of {per_trial} "
                "reports, or out of order"
            )
