from __future__ import annotations

import json
from pathlib import Path


class StateStore:
    def __init__(self, state_path: str) -> None:
        self.path = Path(state_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load_state_data(self) -> dict:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if not isinstance(raw, dict):
            return {}
        return dict(raw)

    def _save_state_data(self, payload: dict) -> None:
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def load_offset(self) -> int | None:
        data = self._load_state_data()
        value = data.get("offset")
        if value is None:
            return None
        try:
            return int(value)
        except Exception:
            return None

    def save_offset(self, offset: int) -> None:
        payload = self._load_state_data()
        payload["offset"] = int(offset)
        self._save_state_data(payload)

    def load_sync_next_due_map(self) -> dict[str, float]:
        data = self._load_state_data()
        raw_map = data.get("sync_next_due_at")
        if not isinstance(raw_map, dict):
            return {}
        out: dict[str, float] = {}
        for key, value in raw_map.items():
            route_name = str(key or "").strip()
            if not route_name:
                continue
            try:
                ts = float(value)
            except Exception:
                continue
            if ts <= 0:
                continue
            out[route_name] = ts
        return out

    def save_sync_next_due_map(self, due_map: dict[str, float]) -> None:
        payload = self._load_state_data()
        cleaned: dict[str, float] = {}
        for key, value in (due_map or {}).items():
            route_name = str(key or "").strip()
            if not route_name:
                continue
            try:
                ts = float(value)
            except Exception:
                continue
            if ts <= 0:
                continue
            cleaned[route_name] = ts
        payload["sync_next_due_at"] = cleaned
        self._save_state_data(payload)
