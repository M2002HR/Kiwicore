from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone


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


def configure_logging(level: str, log_format: str | None = None) -> None:
    resolved_level = getattr(logging, level.upper(), logging.INFO)
    resolved_format = (log_format or os.getenv("LOG_FORMAT", "json")).strip().lower()
    httpx_level = getattr(logging, os.getenv("HTTPX_LOG_LEVEL", "WARNING").strip().upper(), logging.WARNING)

    handler = logging.StreamHandler(stream=sys.stdout)
    if resolved_format == "plain":
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    else:
        handler.setFormatter(JsonLogFormatter())

    logging.basicConfig(level=resolved_level, handlers=[handler], force=True)
    logging.getLogger("httpx").setLevel(httpx_level)
    logging.getLogger("httpcore").setLevel(httpx_level)
