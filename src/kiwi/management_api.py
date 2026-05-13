from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

from kiwi.config import RouteRegistry, load_routes
from kiwi.sync_ledger import SyncLedger
from kiwi.sync_queue import SyncQueueBackend
from kiwi.utils import dump_json

_ROUTE_KEYS = {
    "name",
    "enabled",
    "source_channel_id",
    "source_channel_username",
    "destination_channel_id",
    "destination_channel_username",
    "channel_script",
    "gaurd_script",
    "max_message_mb",
    "sync",
}


class ManagementApi:
    def __init__(
        self,
        *,
        channels_config_path: str,
        scripts_dir: str,
        gaurd_scripts_dir: str,
        sync_ledger: SyncLedger | None = None,
        sync_queue: SyncQueueBackend | None = None,
        on_routes_reloaded: Callable[[RouteRegistry], None],
    ) -> None:
        self.channels_path = Path(channels_config_path)
        self.scripts_dir = Path(scripts_dir)
        self.guards_dir = Path(gaurd_scripts_dir)
        self.sync_ledger = sync_ledger
        self.sync_queue = sync_queue
        self.on_routes_reloaded = on_routes_reloaded
        self._write_lock = threading.Lock()
        self._lock_file_path = self.channels_path.with_suffix(self.channels_path.suffix + ".lock")
        self.channels_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.channels_path.exists():
            dump_json(self.channels_path, [])

    def list_routes(self) -> list[dict]:
        return self._load_routes_raw()

    def add_route(self, route_obj: dict) -> dict:
        if not isinstance(route_obj, dict):
            raise ValueError("route payload must be object")
        route_obj = self._normalize_route_payload(route_obj)
        routes = self._load_routes_raw()
        name = str(route_obj.get("name") or "").strip()
        if not name:
            raise ValueError("route name is required")
        if any(str(item.get("name") or "").strip() == name for item in routes):
            raise ValueError("route name already exists")
        routes.append(route_obj)
        self._save_and_reload(routes)
        return route_obj

    def update_route(self, name: str, patch: dict) -> dict:
        if not isinstance(patch, dict):
            raise ValueError("patch must be object")
        patch = self._normalize_route_payload(patch)
        routes = self._load_routes_raw()
        for idx, item in enumerate(routes):
            if str(item.get("name") or "").strip() != name:
                continue
            updated = dict(item)
            updated.update(patch)
            if "name" in patch and str(patch.get("name") or "").strip() != name:
                raise ValueError("renaming routes is not supported")
            routes[idx] = updated
            self._save_and_reload(routes)
            return updated
        raise ValueError("route not found")

    def get_route(self, name: str) -> dict:
        for item in self._load_routes_raw():
            if str(item.get("name") or "").strip() == name:
                return item
        raise ValueError("route not found")

    def delete_route(self, name: str) -> None:
        routes = self._load_routes_raw()
        filtered = [item for item in routes if str(item.get("name") or "").strip() != name]
        if len(filtered) == len(routes):
            raise ValueError("route not found")
        self._save_and_reload(filtered)

    def set_route_enabled(self, name: str, enabled: bool) -> dict:
        return self.update_route(name, {"enabled": bool(enabled)})

    def update_route_sync(self, name: str, sync_patch: dict) -> dict:
        if not isinstance(sync_patch, dict):
            raise ValueError("sync patch must be object")
        route = self.get_route(name)
        sync_obj = route.get("sync")
        if not isinstance(sync_obj, dict):
            sync_obj = {}
        updated_sync = dict(sync_obj)
        updated_sync.update(sync_patch)
        return self.update_route(name, {"sync": updated_sync})

    def start_route_sync(self, name: str) -> dict:
        route = self.get_route(name)
        sync_obj = route.get("sync") if isinstance(route.get("sync"), dict) else {}
        sync_patch = {
            "enabled": True,
            "status": "syncing",
            "seeded": False,
        }
        return self.update_route_sync(name, sync_patch)

    def stop_route_sync(self, name: str) -> dict:
        return self.update_route_sync(name, {"enabled": False, "status": "active"})

    def list_channel_script_files(self) -> list[str]:
        return sorted(p.name for p in self.scripts_dir.glob("*.py") if p.is_file())

    def list_script_files(self) -> list[str]:
        # Backward compatibility with older call-sites.
        return self.list_channel_script_files()

    def list_guard_files(self) -> list[str]:
        return sorted(p.name for p in self.guards_dir.glob("*.py") if p.is_file())

    def sync_stats_snapshot(self) -> dict:
        queue_depth: int | None
        if self.sync_queue is None:
            queue_depth = 0
        else:
            queue_depth = None
            try:
                import asyncio

                asyncio.get_running_loop()
            except RuntimeError:
                import asyncio

                queue_depth = int(asyncio.run(self.sync_queue.depth()))
        status_counts = self.sync_ledger.get_status_counts() if self.sync_ledger is not None else {}
        open_reviews = self.sync_ledger.open_review_count() if self.sync_ledger is not None else 0
        return {
            "queue_depth": queue_depth,
            "open_reviews": int(open_reviews),
            "status_counts": status_counts,
        }

    async def sync_stats(self) -> dict:
        status_counts = self.sync_ledger.get_status_counts() if self.sync_ledger is not None else {}
        queue_depth = await self.sync_queue.depth() if self.sync_queue is not None else 0
        open_reviews = self.sync_ledger.open_review_count() if self.sync_ledger is not None else 0
        per_route: dict[str, dict[str, int]] = {}
        for route in self._load_routes_raw():
            name = str(route.get("name") or "").strip()
            if not name:
                continue
            if self.sync_ledger is None:
                per_route[name] = {}
            else:
                per_route[name] = self.sync_ledger.get_route_status_counts(name)
        return {
            "queue_depth": int(queue_depth),
            "open_reviews": int(open_reviews),
            "status_counts": status_counts,
            "routes": per_route,
        }

    def sync_review_list(self, *, limit: int = 50, only_open: bool = True) -> list[dict]:
        if self.sync_ledger is None:
            return []
        return self.sync_ledger.list_review(limit=limit, only_open=only_open)

    def sync_review_retry(self, review_id: int) -> dict:
        if self.sync_ledger is None:
            raise ValueError("sync ledger is not available")
        result = self.sync_ledger.resolve_review(int(review_id), resolution="retry")
        if result is None:
            raise ValueError("review not found")
        if self.sync_queue is not None:
            dedupe_key = str(result.get("dedupe_key") or "")
            route_name = str(result.get("route_name") or "")
            if dedupe_key and route_name:
                import asyncio

                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(
                        self.sync_queue.enqueue(
                            dedupe_key=dedupe_key,
                            route_name=route_name,
                            due_at=time.time(),
                            payload_hash="",
                        )
                    )
                except RuntimeError:
                    asyncio.run(
                        self.sync_queue.enqueue(
                            dedupe_key=dedupe_key,
                            route_name=route_name,
                            due_at=time.time(),
                            payload_hash="",
                        )
                    )
        return result

    def sync_review_skip(self, review_id: int) -> dict:
        if self.sync_ledger is None:
            raise ValueError("sync ledger is not available")
        result = self.sync_ledger.resolve_review(int(review_id), resolution="skip")
        if result is None:
            raise ValueError("review not found")
        return result

    def reload_routes(self) -> None:
        registry = load_routes(str(self.channels_path))
        self.on_routes_reloaded(registry)

    def _save_and_reload(self, routes: list[dict]) -> None:
        with self._atomic_file_lock():
            _atomic_dump_json(self.channels_path, routes)
        self.reload_routes()

    def _load_routes_raw(self) -> list[dict]:
        raw = json.loads(self.channels_path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("channels config must be list")
        out: list[dict] = []
        for item in raw:
            if isinstance(item, dict):
                out.append(item)
        return out

    @staticmethod
    def _normalize_route_payload(obj: dict) -> dict:
        out = {k: v for k, v in dict(obj).items() if k in _ROUTE_KEYS or k == "script"}
        if "channel_script" not in out and "script" in out:
            out["channel_script"] = out.get("script")
        out.pop("script", None)
        for key in ("channel_script", "gaurd_script"):
            if key not in out:
                continue
            value = out.get(key)
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                out[key] = None
        return out

    @contextmanager
    def _atomic_file_lock(self):
        self._lock_file_path.parent.mkdir(parents=True, exist_ok=True)
        with self._write_lock, self._lock_file_path.open("a+", encoding="utf-8") as lock_fh:
            try:
                import fcntl

                fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX)
            except Exception:
                pass
            try:
                yield
            finally:
                try:
                    import fcntl

                    fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)
                except Exception:
                    pass


def _atomic_dump_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, indent=2))
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(temp_path, path)
    finally:
        try:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        except Exception:
            pass
