from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any


class TrafficHistory:
    def __init__(self, path: Path, *, run_id: str, started_at_ts: float) -> None:
        self.path = Path(path)
        self.run_id = str(run_id)
        self._lock = threading.RLock()
        self._data = self._load()
        self._ensure_current_run(started_at_ts)

    def add_traffic(self, *, route_name: str, download_bytes: int, upload_bytes: int, now_ts: float | None = None) -> None:
        d = max(0, int(download_bytes))
        u = max(0, int(upload_bytes))
        if d <= 0 and u <= 0:
            return
        with self._lock:
            run = self._get_run(self.run_id)
            if run is None:
                run = self._new_run(self.run_id, float(now_ts or time.time()))
                self._data["runs"].append(run)
            run["total_download_bytes"] = int(run.get("total_download_bytes", 0) or 0) + d
            run["total_upload_bytes"] = int(run.get("total_upload_bytes", 0) or 0) + u
            by_route = run.get("by_route")
            if not isinstance(by_route, dict):
                by_route = {}
                run["by_route"] = by_route
            route_obj = by_route.get(route_name)
            if not isinstance(route_obj, dict):
                route_obj = {"download_bytes": 0, "upload_bytes": 0}
                by_route[route_name] = route_obj
            route_obj["download_bytes"] = int(route_obj.get("download_bytes", 0) or 0) + d
            route_obj["upload_bytes"] = int(route_obj.get("upload_bytes", 0) or 0) + u
            self._touch_run(run, float(now_ts or time.time()))
            self._save_locked()

    def close_current_run(self, *, stopped_at_ts: float | None = None) -> None:
        with self._lock:
            run = self._get_run(self.run_id)
            if run is None:
                return
            now_ts = float(stopped_at_ts or time.time())
            run["stopped_at_ts"] = now_ts
            run["stopped_at"] = _iso_utc(now_ts)
            self._touch_run(run, now_ts)
            self._save_locked()

    def snapshot(self, *, now_ts: float | None = None) -> dict[str, Any]:
        with self._lock:
            now = float(now_ts or time.time())
            runs_raw = list(self._data.get("runs") or [])
            normalized = [self._normalize_run_obj(item, now_ts=now) for item in runs_raw if isinstance(item, dict)]
            normalized.sort(key=lambda item: float(item.get("started_at_ts") or 0.0), reverse=True)

            all_download = sum(int(item.get("total_download_bytes", 0) or 0) for item in normalized)
            all_upload = sum(int(item.get("total_upload_bytes", 0) or 0) for item in normalized)

            current = next((item for item in normalized if str(item.get("run_id") or "") == self.run_id), None)
            previous = [item for item in normalized if str(item.get("run_id") or "") != self.run_id]

            prev_download = sum(int(item.get("total_download_bytes", 0) or 0) for item in previous)
            prev_upload = sum(int(item.get("total_upload_bytes", 0) or 0) for item in previous)
            return {
                "run_id": self.run_id,
                "current_run": current,
                "runs": normalized,
                "previous_runs": previous,
                "runs_count": len(normalized),
                "previous_runs_count": len(previous),
                "history_total_download_bytes": int(all_download),
                "history_total_upload_bytes": int(all_upload),
                "previous_total_download_bytes": int(prev_download),
                "previous_total_upload_bytes": int(prev_upload),
            }

    def _ensure_current_run(self, started_at_ts: float) -> None:
        with self._lock:
            runs = self._data.get("runs")
            if not isinstance(runs, list):
                runs = []
                self._data["runs"] = runs

            # Recover from abrupt shutdown: close any stale open run.
            for item in runs:
                if not isinstance(item, dict):
                    continue
                if item.get("stopped_at_ts") is not None:
                    continue
                if str(item.get("run_id") or "") == self.run_id:
                    continue
                item["stopped_at_ts"] = float(started_at_ts)
                item["stopped_at"] = _iso_utc(float(started_at_ts))
                self._touch_run(item, float(started_at_ts))

            current = self._get_run(self.run_id)
            if current is None:
                runs.append(self._new_run(self.run_id, float(started_at_ts)))
            else:
                self._touch_run(current, float(started_at_ts))
            self._save_locked()

    def _new_run(self, run_id: str, started_at_ts: float) -> dict[str, Any]:
        return {
            "run_id": str(run_id),
            "started_at_ts": float(started_at_ts),
            "started_at": _iso_utc(float(started_at_ts)),
            "stopped_at_ts": None,
            "stopped_at": None,
            "updated_at_ts": float(started_at_ts),
            "updated_at": _iso_utc(float(started_at_ts)),
            "total_download_bytes": 0,
            "total_upload_bytes": 0,
            "by_route": {},
        }

    def _touch_run(self, run: dict[str, Any], now_ts: float) -> None:
        run["updated_at_ts"] = float(now_ts)
        run["updated_at"] = _iso_utc(float(now_ts))

    def _get_run(self, run_id: str) -> dict[str, Any] | None:
        for item in self._data.get("runs") or []:
            if isinstance(item, dict) and str(item.get("run_id") or "") == run_id:
                return item
        return None

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"schema_version": 1, "runs": []}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {"schema_version": 1, "runs": []}
        if not isinstance(raw, dict):
            return {"schema_version": 1, "runs": []}
        runs = raw.get("runs")
        if not isinstance(runs, list):
            runs = []
        return {
            "schema_version": int(raw.get("schema_version", 1) or 1),
            "runs": runs,
        }

    def _save_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(prefix=self.path.name, suffix=".tmp", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(json.dumps(self._data, ensure_ascii=False, indent=2))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(temp_path, self.path)
        finally:
            try:
                if os.path.exists(temp_path):
                    os.unlink(temp_path)
            except Exception:
                pass

    def _normalize_run_obj(self, run: dict[str, Any], *, now_ts: float) -> dict[str, Any]:
        started_at_ts = float(run.get("started_at_ts") or 0.0)
        stopped_at_raw = run.get("stopped_at_ts")
        stopped_at_ts = float(stopped_at_raw) if stopped_at_raw is not None else None
        duration_sec = max(0.0, (stopped_at_ts if stopped_at_ts is not None else now_ts) - started_at_ts)
        by_route = run.get("by_route")
        route_count = len(by_route) if isinstance(by_route, dict) else 0
        return {
            "run_id": str(run.get("run_id") or ""),
            "started_at_ts": started_at_ts,
            "started_at": str(run.get("started_at") or ""),
            "stopped_at_ts": stopped_at_ts,
            "stopped_at": run.get("stopped_at"),
            "updated_at_ts": float(run.get("updated_at_ts") or started_at_ts),
            "updated_at": str(run.get("updated_at") or ""),
            "duration_sec": duration_sec,
            "is_active": stopped_at_ts is None,
            "total_download_bytes": int(run.get("total_download_bytes", 0) or 0),
            "total_upload_bytes": int(run.get("total_upload_bytes", 0) or 0),
            "route_count": int(route_count),
        }


def _iso_utc(ts: float) -> str:
    import datetime as _dt

    return _dt.datetime.fromtimestamp(float(ts), tz=_dt.timezone.utc).isoformat()

