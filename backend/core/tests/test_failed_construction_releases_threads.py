"""A `GraphStorage` whose construction raises leaves no thread behind.

`__init__` owns two threads besides the change-notification listener: the
single writer behind `_io_executor`, and the embedding-model preload. A boot
that failed - a load that raised, a replay that raised, an interrupt - used to
stop only the listener. The writer idled forever on a queue nothing could
reach, and the preload ran on for as long as the model took to load, in a
process that may simply retry construction.
"""

import threading

import pytest

from backend.core.storage import GraphStorage
from backend.core.vector_store import VectorStore

from .test_boot_gate import _NotifyingDuringLoad

_JOIN_TIMEOUT = 5.0


@pytest.fixture
def model_load_never_returns(monkeypatch):
    """Make a preload that has started stay alive until the test ends, so a
    started one cannot pass for a finished one."""
    release = threading.Event()

    def blocked(self):
        release.wait()

    monkeypatch.setattr(VectorStore, "_load_model", blocked)
    yield
    release.set()


def _preload_threads():
    return {t for t in threading.enumerate() if t.name == "embedding-preload"}


class _Captured:
    """The half-built instance and the writer thread it ran on."""

    storage = None
    writer = None


def _load_that_writes_then_raises(captured, exc_type):
    def load(self, *args, **kwargs):
        captured.storage = self
        # A real load waits on the writer queue, which is what spawns the
        # writer thread in the first place.
        captured.writer = self._io_executor.submit(threading.current_thread).result()
        raise exc_type("load failed")

    return load


@pytest.mark.usefixtures("model_load_never_returns")
@pytest.mark.parametrize("exc_type", [RuntimeError, KeyboardInterrupt, SystemExit])
@pytest.mark.parametrize("notifying", [True, False], ids=["notifying", "file"])
def test_a_failed_load_leaves_no_thread_running(
    monkeypatch, tmp_path, exc_type, notifying
):
    before = _preload_threads()
    captured = _Captured()
    monkeypatch.setattr(
        GraphStorage, "load", _load_that_writes_then_raises(captured, exc_type)
    )

    with pytest.raises(exc_type, match="load failed"):
        if notifying:
            GraphStorage(persistence_backend=_NotifyingDuringLoad())
        else:
            GraphStorage(json_path=str(tmp_path / "graph.json"))

    captured.writer.join(_JOIN_TIMEOUT)
    assert not captured.writer.is_alive(), (
        "construction failed with the writer thread still running"
    )
    with pytest.raises(RuntimeError):
        captured.storage._io_executor.submit(lambda: None)
    assert _preload_threads() <= before, (
        "construction failed with the model preload still running"
    )


@pytest.mark.usefixtures("model_load_never_returns")
def test_a_replay_that_raises_leaves_no_thread_running(monkeypatch):
    """The replay is the last step that can fail, after the load has
    already put the writer to work."""
    before = _preload_threads()
    captured = _Captured()
    real_load = GraphStorage.load

    def load(self, *args, **kwargs):
        captured.storage = self
        captured.writer = self._io_executor.submit(threading.current_thread).result()
        return real_load(self, *args, **kwargs)

    def angry(self, change):
        raise RuntimeError("replay failed")

    monkeypatch.setattr(GraphStorage, "load", load)
    monkeypatch.setattr(GraphStorage, "apply_external_change", angry)

    with pytest.raises(RuntimeError, match="replay failed"):
        GraphStorage(persistence_backend=_NotifyingDuringLoad())

    captured.writer.join(_JOIN_TIMEOUT)
    assert not captured.writer.is_alive()
    assert _preload_threads() <= before


class _StopRaises(_NotifyingDuringLoad):
    """Fails to stop - after recording whether the writer was still usable
    at that moment."""

    def __init__(self):
        super().__init__()
        self.storage = None
        self.writer_open_at_stop = None

    def stop_change_notification(self):
        try:
            self.storage._io_executor.submit(lambda: None).result()
            self.writer_open_at_stop = True
        except RuntimeError:
            self.writer_open_at_stop = False
        raise OSError("stop failed")


def test_the_listener_stops_first_and_a_failed_stop_still_releases_the_writer(
    monkeypatch,
):
    backend = _StopRaises()
    captured = _Captured()

    def load(self, *args, **kwargs):
        backend.storage = captured.storage = self
        captured.writer = self._io_executor.submit(threading.current_thread).result()
        raise RuntimeError("load failed")

    monkeypatch.setattr(GraphStorage, "load", load)

    with pytest.raises((RuntimeError, OSError)):
        GraphStorage(persistence_backend=backend)

    assert backend.writer_open_at_stop is True, (
        "the writer was shut down before the listener that can still call "
        "into the half-built object was stopped"
    )
    captured.writer.join(_JOIN_TIMEOUT)
    assert not captured.writer.is_alive(), (
        "a listener that failed to stop kept the writer thread alive"
    )


def test_a_construction_that_succeeds_still_preloads_the_model(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(VectorStore, "preload_model", lambda self: calls.append(self))

    storage = GraphStorage(json_path=str(tmp_path / "graph.json"))
    try:
        assert calls == [storage.vector_store]
    finally:
        storage.shutdown_events()
