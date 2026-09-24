import json
import logging
import re
import sys

from backend.api_host.config import AppConfig
from backend.api_host.logging_config import (
    StructuredJsonFormatter,
    configure_root_logging,
)


def test_app_config_reads_log_format_from_env(monkeypatch):
    monkeypatch.setenv("LOG_FORMAT", "structured_json")

    config = AppConfig.from_env()

    assert config.log_format == "structured_json"


def test_configure_root_logging_sets_structured_json_formatter():
    previous_handlers = logging.root.handlers[:]
    previous_level = logging.root.level
    previous_uvicorn_handlers = {
        name: logging.getLogger(name).handlers[:]
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access")
    }
    try:
        logging.root.handlers = [logging.StreamHandler()]
        for name in previous_uvicorn_handlers:
            logging.getLogger(name).handlers = [logging.StreamHandler()]

        configure_root_logging("structured_json")

        assert len(logging.root.handlers) == 1
        assert isinstance(logging.root.handlers[0].formatter, StructuredJsonFormatter)
        for name in previous_uvicorn_handlers:
            assert isinstance(
                logging.getLogger(name).handlers[0].formatter,
                StructuredJsonFormatter,
            )
    finally:
        logging.root.handlers = previous_handlers
        logging.root.setLevel(previous_level)
        for name, handlers in previous_uvicorn_handlers.items():
            logging.getLogger(name).handlers = handlers


def test_structured_json_formatter_emits_json_log_line():
    formatter = StructuredJsonFormatter()
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        record = logging.LogRecord(
            name="backend.api_host.server",
            level=logging.INFO,
            pathname=__file__,
            lineno=42,
            msg="startup complete",
            args=(),
            exc_info=sys.exc_info(),
        )

    payload = json.loads(formatter.format(record))

    assert payload["message"] == "startup complete"
    assert payload["logger"] == "backend.api_host.server"
    assert payload["level"] == "INFO"
    assert payload["timestamp"]
    assert "RuntimeError: boom" in payload["exception"]


def test_text_format_labels_app_logs_from_boot_at_info():
    previous_handlers = logging.root.handlers[:]
    previous_level = logging.root.level
    try:
        logging.root.handlers = []
        logging.root.setLevel(logging.WARNING)

        configure_root_logging("text")

        assert len(logging.root.handlers) == 1
        assert logging.root.level == logging.INFO
        record = logging.LogRecord(
            name="backend.core.postgres_backend",
            level=logging.WARNING,
            pathname=__file__,
            lineno=1,
            msg="graph checkpoint failed, will retry: disk full",
            args=(),
            exc_info=None,
        )
        line = logging.root.handlers[0].format(record)
        assert re.fullmatch(
            r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} WARNING "
            r"backend\.core\.postgres_backend: "
            r"graph checkpoint failed, will retry: disk full",
            line,
        ), line
    finally:
        for handler in logging.root.handlers:
            if handler not in previous_handlers:
                handler.close()
        logging.root.handlers = previous_handlers
        logging.root.setLevel(previous_level)


def test_text_format_is_not_replaced_by_fastmcp_logging_setup():
    """FastMCP's constructor configures logging with basicConfig; once the
    text-mode handler exists that must be a no-op, not a second handler."""
    from mcp.server.fastmcp import FastMCP

    previous_handlers = logging.root.handlers[:]
    previous_level = logging.root.level
    try:
        logging.root.handlers = []

        configure_root_logging("text")
        installed = logging.root.handlers[:]
        FastMCP("probe")

        assert logging.root.handlers == installed
        assert logging.root.level == logging.INFO
    finally:
        for handler in logging.root.handlers:
            if handler not in previous_handlers:
                handler.close()
        logging.root.handlers = previous_handlers
        logging.root.setLevel(previous_level)


def test_text_format_leaves_an_existing_root_handler_and_uvicorn_alone():
    uvicorn_names = ("uvicorn", "uvicorn.error", "uvicorn.access")
    previous_handlers = logging.root.handlers[:]
    previous_level = logging.root.level
    existing = logging.StreamHandler()
    try:
        logging.root.handlers = [existing]
        uvicorn_before = {
            name: (
                logging.getLogger(name).level,
                logging.getLogger(name).propagate,
                logging.getLogger(name).handlers[:],
            )
            for name in uvicorn_names
        }

        configure_root_logging("text")

        assert logging.root.handlers == [existing]
        assert existing.formatter is None
        for name in uvicorn_names:
            logger = logging.getLogger(name)
            assert (
                logger.level,
                logger.propagate,
                logger.handlers,
            ) == uvicorn_before[name]
    finally:
        logging.root.handlers = previous_handlers
        logging.root.setLevel(previous_level)
