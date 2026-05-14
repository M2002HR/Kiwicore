from __future__ import annotations

import json
import logging
import os
import sys
import threading
import traceback
from collections import deque
from datetime import datetime, timezone

_LOG_LOCK = threading.Lock()
_LOG_BUFFER: deque[dict[str, object]] = deque(maxlen=4000)


class JsonLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "service.name": os.getenv("SERVICE_NAME", "kiwi-bridge"),
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        details = getattr(record, "details", None)
        if details is not None:
            payload["details"] = details
        return json.dumps(payload, ensure_ascii=False)


class InMemoryLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            payload: dict[str, object] = {
                "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
            }
            details = getattr(record, "details", None)
            if details is not None:
                payload["details"] = details
            if record.exc_info:
                payload["exception"] = "".join(traceback.format_exception(*record.exc_info)).strip()
            with _LOG_LOCK:
                _LOG_BUFFER.append(payload)
        except Exception:
            # logging must never fail the caller
            return


def recent_logs(
    *,
    limit: int = 200,
    level: str | None = None,
    logger_name: str | None = None,
    message_contains: str | None = None,
) -> list[dict[str, object]]:
    take = max(1, min(2000, int(limit)))
    level_filter = str(level or "").strip().upper()
    logger_filter = str(logger_name or "").strip().lower()
    message_filter = str(message_contains or "").strip().lower()
    with _LOG_LOCK:
        items = list(_LOG_BUFFER)
    if level_filter:
        items = [item for item in items if str(item.get("level") or "").upper() == level_filter]
    if logger_filter:
        items = [item for item in items if logger_filter in str(item.get("logger") or "").lower()]
    if message_filter:
        items = [item for item in items if message_filter in str(item.get("message") or "").lower()]
    return items[-take:]


def configure_logging(level: str, log_format: str | None = None) -> None:
    resolved_level = getattr(logging, level.upper(), logging.INFO)
    resolved_format = (log_format or os.getenv("LOG_FORMAT", "json")).strip().lower()
    httpx_level = getattr(logging, os.getenv("HTTPX_LOG_LEVEL", "WARNING").strip().upper(), logging.WARNING)

    handler = logging.StreamHandler(stream=sys.stdout)
    if resolved_format == "plain":
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    else:
        handler.setFormatter(JsonLogFormatter())

    memory_handler = InMemoryLogHandler(level=logging.DEBUG)
    logging.basicConfig(level=resolved_level, handlers=[handler, memory_handler], force=True)
    logging.getLogger("httpx").setLevel(httpx_level)
    logging.getLogger("httpcore").setLevel(httpx_level)
