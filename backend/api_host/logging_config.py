"""Logging configuration for the app host server."""

import json
import logging
import sys
from datetime import datetime, timezone

_UVICORN_LOGGERS = (
    "uvicorn",
    "uvicorn.error",
    "uvicorn.access",
)


class StructuredJsonFormatter(logging.Formatter):
    """Emit log records as single-line JSON objects."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=timezone.utc
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, default=str)


def _apply_formatter(logger: logging.Logger, formatter: logging.Formatter) -> None:
    if not logger.handlers:
        return
    for handler in logger.handlers:
        handler.setFormatter(formatter)


def configure_root_logging(log_format: str) -> None:
    """Configure the app and Uvicorn loggers according to the requested format."""
    if log_format != "structured_json":
        return

    formatter = StructuredJsonFormatter()
    if logging.root.handlers:
        _apply_formatter(logging.root, formatter)
    else:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(formatter)
        logging.root.addHandler(handler)

    logging.root.setLevel(logging.INFO)

    for logger_name in _UVICORN_LOGGERS:
        _apply_formatter(logging.getLogger(logger_name), formatter)
