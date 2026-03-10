from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import UTC, datetime

_SECRET_PATTERN = re.compile(
    r"(ghp_[A-Za-z0-9]{30,}|ghs_[A-Za-z0-9]{30,}|gho_[A-Za-z0-9]{30,}"
    r"|github_pat_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9]{20,}|xoxb-[A-Za-z0-9-]{20,})"
)


def _mask_secrets(text: str) -> str:
    return _SECRET_PATTERN.sub("***", text)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        message = _mask_secrets(record.getMessage())
        entry = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": message,
        }
        if record.exc_info and record.exc_info[1]:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, ensure_ascii=False)


class _TextFormatter(logging.Formatter):
    def __init__(self) -> None:
        super().__init__(fmt="%(asctime)s %(levelname)-8s [%(name)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        record.msg = _mask_secrets(str(record.msg))
        return super().format(record)


def configure_logging() -> None:
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    log_format = os.environ.get("LOG_FORMAT", "text").lower()

    root = logging.getLogger()
    root.setLevel(getattr(logging, log_level, logging.INFO))

    # Remove existing handlers
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stderr)
    if log_format == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(_TextFormatter())

    root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
