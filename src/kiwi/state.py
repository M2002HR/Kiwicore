from __future__ import annotations

import json
from pathlib import Path


class StateStore:
    def __init__(self, state_path: str) -> None:
        self.path = Path(state_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load_offset(self) -> int | None:
        if not self.path.exists():
            return None
        data = json.loads(self.path.read_text(encoding="utf-8"))
        value = data.get("offset")
        if value is None:
            return None
        return int(value)

    def save_offset(self, offset: int) -> None:
        payload = {"offset": int(offset)}
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
