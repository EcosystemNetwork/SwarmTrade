"""Structured JSON logging for production operation.

Phase 4: Machine-parseable logs for searching, alerting, and monitoring.

Usage:
    from swarmtrader.logging_config import setup_logging
    setup_logging(json_mode=True, level="INFO", log_file="swarm.log")
"""
from __future__ import annotations
import json, logging, sys, time
from typing import Any


class JSONFormatter(logging.Formatter):
    """Outputs log records as single-line JSON for machine parsing.

    Fields: ts, level, logger, message, plus any extras from record.__dict__.
    """

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": record.created,
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Add extras that aren't standard LogRecord attributes
        _standard = {
            "name", "msg", "args", "created", "relativeCreated", "exc_info",
            "exc_text", "stack_info", "lineno", "funcName", "pathname",
            "filename", "module", "levelno", "levelname", "msecs",
            "processName", "process", "threadName", "thread", "taskName",
            "message", "asctime",
        }
        for key, val in record.__dict__.items():
            if key not in _standard and not key.startswith("_"):
                try:
                    json.dumps(val)  # only include JSON-serializable values
                    entry[key] = val
                except (TypeError, ValueError):
                    entry[key] = str(val)

        if record.exc_info and record.exc_info[1]:
            entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(entry, default=str)


class TradeLogFilter(logging.Filter):
    """Adds trade-specific context fields to log records.

    Enriches logs with intent_id, asset, side when available in the message.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        # Always allow the record through
        return True


def setup_logging(
    json_mode: bool = False,
    level: str = "INFO",
    log_file: str | None = None,
):
    """Configure logging for the trading system.

    Args:
        json_mode: True for structured JSON output (production),
                   False for human-readable (development)
        level: Log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)
        log_file: Optional file path to write logs to (in addition to stderr)
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # Clear existing handlers
    root.handlers.clear()

    if json_mode:
        formatter = JSONFormatter()
    else:
        formatter = logging.Formatter(
            "%(asctime)s %(name)-16s %(levelname)-5s %(message)s"
        )

    # Console handler (stderr)
    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    root.addHandler(console)

    # Optional file handler
    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    # Quiet down noisy third-party loggers
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
