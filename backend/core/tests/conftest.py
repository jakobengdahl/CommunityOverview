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


@pytest.fixture
def storage_log(caplog, capsys):
    """What GraphStorage logged since the previous call, as level -> messages.

    None of those messages may also have reached stdout: the storage module
    reports through its logger, and a report printed as well would reach an
    operator twice, once without a level. Checked again at teardown, for
    whatever was logged after the last call or never read at all."""
    caplog.set_level(logging.DEBUG, logger=_STORAGE_LOGGER)
    # A cursor rather than caplog.clear(), for the reason given on the file
    # backend's `reported` fixture in test_file_backend_journal.py. The
    # records already taken, by identity: a test that clears caplog itself
    # would otherwise shift new records under the cursor unread.
    taken = []

    def report(fresh):
        logged = defaultdict(list)
        for record in fresh:
            if record.name == _STORAGE_LOGGER:
                logged[record.levelno].append(record.getMessage())
        out = capsys.readouterr().out
        leaked = [m for ms in logged.values() for m in ms if m in out]
        assert not leaked, f"a report went to stdout: {leaked}"
        return logged

    def unread(records):
        assert records[: len(taken)] == taken, (
            "caplog was cleared under the storage_log fixture; read reports "
            "through it instead"
        )
        return records[len(taken) :]

    def take():
        fresh = unread(caplog.records)
        taken.extend(fresh)
        return report(fresh)

    yield take
    # By now caplog.records is the teardown phase's own list, so the test
    # body's records are read back by phase.
    report(unread(caplog.get_records("call")) + caplog.get_records("teardown"))
