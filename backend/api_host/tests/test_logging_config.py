import json
import logging
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
