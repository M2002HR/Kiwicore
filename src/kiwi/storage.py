from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from kiwi.types import IncomingChannelMessage, PreparedMessagePaths


class StorageManager:
    def __init__(self, base_dir: str) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def prepare_message_paths(self, message: IncomingChannelMessage) -> PreparedMessagePaths:
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        channel_dir = message.source_channel_id.replace("/", "_")
        base = self.base_dir / "messages" / channel_dir / f"{message.update_id}_{ts}"
        input_dir = base / "input"
        output_dir = base / "output"
        input_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)

        return PreparedMessagePaths(
            base_dir=str(base),
            input_dir=str(input_dir),
            output_dir=str(output_dir),
            payload_path=str(base / "payload.json"),
            raw_update_path=str(base / "raw_update.json"),
        )

    def write_raw_update(self, paths: PreparedMessagePaths, raw_update: dict) -> None:
        Path(paths.raw_update_path).write_text(json.dumps(raw_update, ensure_ascii=False, indent=2), encoding="utf-8")

    def write_payload(self, paths: PreparedMessagePaths, payload: dict) -> None:
        Path(paths.payload_path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
