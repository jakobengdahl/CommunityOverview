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

import pytest

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

    def load_graph_data(self):
        if self._listener is not None:
            change = _change("committed-during-load")
            self.reported.append(change)
            self._listener(change)
        return super().load_graph_data()


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
        seen_during_load = []
        real_load = GraphStorage.load

        def spy_apply(self, change):
            seen_during_load.append(self._loading_now)

        def load_marking(self, *args, **kwargs):
            self._loading_now = True
            try:
                return real_load(self, *args, **kwargs)
            finally:
                self._loading_now = False

        monkeypatch.setattr(GraphStorage, "apply_external_change", spy_apply)
        monkeypatch.setattr(GraphStorage, "load", load_marking)
        GraphStorage._loading_now = False

        storage = GraphStorage(persistence_backend=backend)
        try:
            assert seen_during_load == [False], (
                f"expected one report applied after the load, got "
                f"{seen_during_load} (True means it landed mid-load)"
            )
        finally:
            storage.shutdown_events()

    def test_a_failed_load_stops_notification(self, monkeypatch):
        """Otherwise the listener outlives the object it reports into, holding
        its connection and refreshing something nothing will shut down."""
        backend = _NotifyingDuringLoad()

        def boom(self, *args, **kwargs):
            raise RuntimeError("load failed")

        monkeypatch.setattr(GraphStorage, "load", boom)

        with pytest.raises(RuntimeError, match="load failed"):
            GraphStorage(persistence_backend=backend)

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
