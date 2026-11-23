"""Structured logging with secret redaction."""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

_SECRETS: set[str] = set()
_RESERVED = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {"message", "asctime"}


def register_secret(value: str | None) -> None:
    """Make sure `value` can never appear in log output."""
    if value:
        _SECRETS.add(value)


def redact(text: str) -> str:
    for secret in _SECRETS:
        text = text.replace(secret, "***")
    return text


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="seconds"),
            "level": record.levelname.lower(),
            "event": record.getMessage(),
        }
        payload.update({key: value for key, value in record.__dict__.items() if key not in _RESERVED})
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        # Redact each value before encoding: JSON escaping would otherwise disguise a secret
        # that contains quotes or backslashes.
        return json.dumps({key: _clean(value) for key, value in payload.items()})


def _clean(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact(str(value))


class _TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        extras = " ".join(
            f"{key}={value}" for key, value in record.__dict__.items() if key not in _RESERVED
        )
        text = f"{record.levelname.lower():7} {record.getMessage()}"
        return redact(f"{text} {extras}".rstrip())


def configure(fmt: str = "text", verbose: bool = False) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(_JsonFormatter() if fmt == "json" else _TextFormatter())
    root = logging.getLogger("schemaops")
    root.handlers[:] = [handler]
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.propagate = False


def get(name: str) -> logging.Logger:
    return logging.getLogger(f"schemaops.{name}")
