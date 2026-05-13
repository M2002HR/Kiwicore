from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def normalize_channel_id(value: str | int | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.startswith("@"):
        return None
    if text.lstrip("-").isdigit():
        return str(int(text))
    return text


def normalize_channel_username(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    lowered = text.lower()
    # Preserve Telegram invite/public links as-is for Telethon source resolution.
    if lowered.startswith("https://t.me/") or lowered.startswith("http://t.me/") or lowered.startswith("t.me/"):
        if lowered.startswith("t.me/"):
            return f"https://{text}"
        return text
    text = lowered
    if not text.startswith("@"):
        text = f"@{text}"
    return text


def safe_script_name(name: str) -> str:
    file_name = Path(name).name
    if file_name != name:
        raise ValueError(f"Invalid script name: {name}")
    if not file_name.endswith(".py"):
        raise ValueError("Script file name must end with .py")
    return file_name


def dump_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
