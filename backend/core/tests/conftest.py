"""
Pytest configuration for graph_core tests
"""

import logging
from collections import defaultdict

import pytest

_STORAGE_LOGGER = "backend.core.storage"


def pytest_configure(config):
    """Configure custom markers"""
    config.addinivalue_line(
        "markers",
        "slow: marks tests as slow (require model loading, deselect with '-m \"not slow\"')",
    )


def pytest_collection_modifyitems(config, items):
    """Skip slow tests by default unless explicitly requested"""
    if config.getoption("-m"):
        # If markers are explicitly specified, don't modify
        return

    skip_slow = pytest.mark.skip(reason="slow test - use '-m slow' to run")
    for item in items:
        if "slow" in item.keywords:
            item.add_marker(skip_slow)


class _Collected(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.NOTSET)
        self.records = []

    def emit(self, record):
        self.records.append(record)


@pytest.fixture
def storage_log(caplog, capsys):
    """What GraphStorage logged since the previous call, as level -> messages.

    None of those messages may also have reached stdout: the storage module
    reports through its logger, and a report printed as well would reach an
    operator twice, once without a level. Checked again at teardown, for
    whatever was logged after the last call or never read at all."""
    caplog.set_level(logging.DEBUG, logger=_STORAGE_LOGGER)
    # Collected on a handler of the fixture's own rather than read from
    # caplog: caplog.clear() and caplog's per-phase lists would otherwise let
    # a record go unread - one cleared before the first call, or one logged
    # in a phase other than the one being read.
    collected = _Collected()
    storage_logger = logging.getLogger(_STORAGE_LOGGER)
    storage_logger.addHandler(collected)
    seen = 0

    def take():
        nonlocal seen
        fresh = collected.records[seen:]
        seen += len(fresh)
        logged = defaultdict(list)
        for record in fresh:
            if record.name == _STORAGE_LOGGER:
                logged[record.levelno].append(record.getMessage())
        out = capsys.readouterr().out
        leaked = [m for ms in logged.values() for m in ms if m in out]
        assert not leaked, f"a report went to stdout: {leaked}"
        return logged

    try:
        yield take
        take()
    finally:
        storage_logger.removeHandler(collected)
