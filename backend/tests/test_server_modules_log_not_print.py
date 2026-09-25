"""
Server-side modules report through their module logger, not print().

A print() goes to stdout with no level, no logger name and no timestamp, so an
operator cannot filter a failed checkpoint apart from routine chatter. These
tests pin the level each converted report is logged at, and that it does not
also reach stdout.
"""

import ast
import json
import logging
import os
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from backend.agents.config import AgentsSettings
from backend.core.embedding_sidecar import FileEmbeddingSidecar
from backend.core.events.dispatcher import EventDispatcher
from backend.core.events.models import (
    EntityData,
    EntityKind,
    Event,
    EventContext,
    EventType,
)
from backend.core.storage_events import emit_event
from backend.federation.config import load_federation_config

REPO_ROOT = Path(__file__).resolve().parents[2]

CONVERTED_MODULES = (
    "backend/agents/config.py",
    "backend/core/embedding_sidecar.py",
    "backend/core/events/dispatcher.py",
    "backend/core/postgres_backend.py",
    "backend/core/storage_backends.py",
    "backend/core/storage_events.py",
    "backend/federation/config.py",
)


def _logged(caplog, capsys, logger_name, level):
    """Messages `logger_name` logged at exactly `level`, after checking that
    none of them also went to stdout."""
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == logger_name and record.levelno == level
    ]
    out = capsys.readouterr().out
    leaked = [message for message in messages if message in out]
    assert not leaked, f"a report went to stdout: {leaked}"
    return messages


def _dotted(node):
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


# Every way of writing to stdout that bypasses the logger.
_STDOUT_CALLS = {"print", "builtins.print", "sys.stdout.write"}


def _stdout_calls(source):
    return [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call) and _dotted(node.func) in _STDOUT_CALLS
    ]


@pytest.mark.parametrize(
    "source",
    [
        "print('x')",
        "import builtins\nbuiltins.print('x')",
        "import sys\nsys.stdout.write('x')",
    ],
)
def test_the_guard_catches_each_way_of_writing_to_stdout(source):
    assert _stdout_calls(source) == [source.count("\n") + 1]


def test_the_guard_ignores_logger_calls_and_other_streams():
    source = (
        "import logging, sys\nlogging.getLogger().warning('x')\nsys.stderr.write('x')"
    )
    assert _stdout_calls(source) == []


@pytest.mark.parametrize("module", CONVERTED_MODULES)
def test_converted_module_has_no_print_call(module):
    calls = _stdout_calls((REPO_ROOT / module).read_text(encoding="utf-8"))
    assert not calls, f"{module} writes to stdout at line(s) {calls}; use its logger"


class TestStorageEvents:
    LOGGER = "backend.core.storage_events"

    def _emit(self, **overrides):
        kwargs = dict(
            history_store=None,
            system_listeners=[],
            events_enabled=False,
            event_dispatcher=None,
            event_type=EventType.NODE_CREATE,
            entity_kind=EntityKind.NODE,
            entity_id="n1",
            entity_type="Actor",
        )
        kwargs.update(overrides)
        emit_event(**kwargs)

    def test_a_failed_history_write_is_a_warning(self, caplog, capsys):
        class _BrokenHistory:
            def append_event(self, event):
                raise OSError("history disk full")

        caplog.set_level(logging.DEBUG, logger=self.LOGGER)
        self._emit(history_store=_BrokenHistory())

        warnings = _logged(caplog, capsys, self.LOGGER, logging.WARNING)
        assert any("history disk full" in m for m in warnings), warnings

    def test_a_failing_system_listener_is_an_error(self, caplog, capsys):
        def _listener(event):
            raise RuntimeError("listener blew up")

        caplog.set_level(logging.DEBUG, logger=self.LOGGER)
        self._emit(system_listeners=[_listener])

        errors = _logged(caplog, capsys, self.LOGGER, logging.ERROR)
        assert any("listener blew up" in m for m in errors), errors

    def test_a_failed_dispatch_is_a_warning(self, caplog, capsys):
        class _BrokenDispatcher:
            def dispatch(self, event):
                raise RuntimeError("dispatcher down")

        caplog.set_level(logging.DEBUG, logger=self.LOGGER)
        self._emit(events_enabled=True, event_dispatcher=_BrokenDispatcher())

        warnings = _logged(caplog, capsys, self.LOGGER, logging.WARNING)
        assert any("dispatcher down" in m for m in warnings), warnings

    def test_per_event_chatter_is_debug_only(self, caplog, capsys):
        caplog.set_level(logging.DEBUG, logger=self.LOGGER)
        self._emit()

        debug = _logged(caplog, capsys, self.LOGGER, logging.DEBUG)
        assert any("Skipped" in m for m in debug), debug
        assert not [
            r
            for r in caplog.records
            if r.name == self.LOGGER and r.levelno > logging.DEBUG
        ]

    def test_emitting_to_a_working_dispatcher_is_debug_only(self, caplog, capsys):
        dispatched = []

        class _Dispatcher:
            def dispatch(self, event):
                dispatched.append(event)

        caplog.set_level(logging.DEBUG, logger=self.LOGGER)
        self._emit(events_enabled=True, event_dispatcher=_Dispatcher())

        assert len(dispatched) == 1
        debug = _logged(caplog, capsys, self.LOGGER, logging.DEBUG)
        assert any("Emitting" in m and "n1" in m and "Actor" in m for m in debug), debug
        assert not [
            r
            for r in caplog.records
            if r.name == self.LOGGER and r.levelno > logging.DEBUG
        ]


class TestDispatcher:
    LOGGER = "backend.core.events.dispatcher"

    class _Node:
        def __init__(self, id, name, node_type, metadata=None):
            self.id = id
            self.name = name
            self.type = node_type
            self.metadata = metadata or {}

    class _Storage:
        def __init__(self, nodes):
            self.nodes = {n.id: n for n in nodes}

    def _dispatcher(self):
        subscription = self._Node(
            "sub-1",
            "Actor watcher",
            "EventSubscription",
            {
                "filters": {
                    "target": {"entity_kind": "node", "node_types": ["Actor"]},
                    "operations": ["create"],
                },
                "delivery": {"webhook_url": "https://example.com/hook"},
            },
        )
        delivered = []
        dispatcher = EventDispatcher(
            self._Storage([subscription]),
            on_deliver=lambda event, url: delivered.append(url),
        )
        return dispatcher, delivered

    @staticmethod
    def _event():
        return Event(
            event_type=EventType.NODE_CREATE,
            origin=EventContext(),
            entity=EntityData(kind=EntityKind.NODE, id="n1", type="Actor"),
        )

    def test_routine_dispatch_chatter_is_debug_only(self, caplog, capsys):
        dispatcher, delivered = self._dispatcher()
        caplog.set_level(logging.DEBUG, logger=self.LOGGER)

        assert dispatcher.dispatch(self._event()) == 1

        assert delivered == ["https://example.com/hook"]
        debug = _logged(caplog, capsys, self.LOGGER, logging.DEBUG)
        assert any("Loaded 1 EventSubscription" in m for m in debug), debug
        assert any("Dispatching to 1 subscription" in m for m in debug), debug
        assert any("'Actor watcher' matches=True" in m for m in debug), debug
        chatter = ("Loaded", "Dispatching", "matches=")
        louder = [
            r.getMessage()
            for r in caplog.records
            if r.name == self.LOGGER
            and r.levelno > logging.DEBUG
            and any(word in r.getMessage() for word in chatter)
        ]
        assert not louder, louder


class TestFederationConfig:
    LOGGER = "backend.federation.config"

    @pytest.fixture(autouse=True)
    def _isolated_env(self):
        keys = (
            "FEDERATION_FILE",
            "GRAPH_FEDERATION_CONFIG",
            "COMMUNITYOVERVIEW_TENANT_CONFIG_DIR",
        )
        with patch.dict(os.environ, {}, clear=False):
            for key in keys:
                os.environ.pop(key, None)
            yield

    def test_a_missing_file_is_info(self, tmp_path, caplog, capsys):
        os.environ["FEDERATION_FILE"] = str(tmp_path / "missing.json")
        caplog.set_level(logging.DEBUG, logger=self.LOGGER)

        load_federation_config()

        info = _logged(caplog, capsys, self.LOGGER, logging.INFO)
        assert any("not found" in m for m in info), info

    def test_an_invalid_file_is_a_warning(self, tmp_path, caplog, capsys):
        path = tmp_path / "federation.json"
        path.write_text("{not json", encoding="utf-8")
        os.environ["FEDERATION_FILE"] = str(path)
        caplog.set_level(logging.DEBUG, logger=self.LOGGER)

        config = load_federation_config()

        assert config.federation.enabled is False
        warnings = _logged(caplog, capsys, self.LOGGER, logging.WARNING)
        assert any("Invalid federation config" in m for m in warnings), warnings

    def test_a_loaded_file_is_info(self, tmp_path, caplog, capsys):
        path = tmp_path / "federation.json"
        path.write_text(json.dumps({}), encoding="utf-8")
        os.environ["FEDERATION_FILE"] = str(path)
        caplog.set_level(logging.DEBUG, logger=self.LOGGER)

        load_federation_config()

        info = _logged(caplog, capsys, self.LOGGER, logging.INFO)
        assert any("Loaded federation configuration" in m for m in info), info


def test_unparseable_mcp_integrations_is_a_warning(caplog, capsys):
    logger_name = "backend.agents.config"
    caplog.set_level(logging.DEBUG, logger=logger_name)
    env = {"AGENTS_ENABLED": "true", "MCP_INTEGRATIONS": "{not json"}

    with patch.dict(os.environ, env, clear=True):
        AgentsSettings.from_env()

    warnings = _logged(caplog, capsys, logger_name, logging.WARNING)
    assert any("MCP_INTEGRATIONS" in m for m in warnings), warnings


def test_unloadable_model_profiles_is_a_warning(caplog, capsys):
    logger_name = "backend.agents.config"
    caplog.set_level(logging.DEBUG, logger=logger_name)

    with (
        patch.dict(os.environ, {"AGENTS_ENABLED": "true"}, clear=True),
        patch(
            "backend.config.config_loader.get_model_profiles",
            side_effect=RuntimeError("schema unreadable"),
        ),
    ):
        settings = AgentsSettings.from_env()

    assert settings.model_profiles == []
    warnings = _logged(caplog, capsys, logger_name, logging.WARNING)
    assert any("model profiles" in m and "schema unreadable" in m for m in warnings), (
        warnings
    )


def test_a_replaced_corrupt_sidecar_is_a_warning(tmp_path, caplog, capsys):
    logger_name = "backend.core.embedding_sidecar"
    path = tmp_path / "embeddings.bin"
    path.write_bytes(b"corrupted beyond recognition")
    caplog.set_level(logging.DEBUG, logger=logger_name)

    FileEmbeddingSidecar(path, owns_path=True).save(
        {"n1": np.ones(4, dtype=np.float32)}
    )

    spoiled = tmp_path / "embeddings.bin.corrupt"
    assert spoiled.read_bytes() == b"corrupted beyond recognition"
    warnings = _logged(caplog, capsys, logger_name, logging.WARNING)
    assert any(
        f"{path} was not a readable sidecar" in m and f"moved it to {spoiled}" in m
        for m in warnings
    ), warnings
