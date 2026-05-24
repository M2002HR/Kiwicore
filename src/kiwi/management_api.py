from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import asyncio
import inspect
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable
from uuid import uuid4

from kiwi.config import RouteRegistry, load_routes
from kiwi.sync_ledger import SyncLedger
from kiwi.sync_queue import SyncQueueBackend
from kiwi.utils import dump_json, safe_script_name

_ROUTE_KEYS = {
    "name",
    "status",
    "source_channel_id",
    "source_channel_username",
    "destination_channel_id",
    "destination_channel_username",
    "channel_script",
    "gaurd_script",
    "max_message_mb",
    "backfill_count",
    "interval_sec",
    "batch_size",
    "retry_attempts",
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
        source_client: object | None = None,
        on_routes_reloaded: Callable[[RouteRegistry], None],
    ) -> None:
        self.channels_path = Path(channels_config_path)
        self.scripts_dir = Path(scripts_dir)
        self.guards_dir = Path(gaurd_scripts_dir)
        self.sync_ledger = sync_ledger
        self.sync_queue = sync_queue
        self.source_client = source_client
        self.on_routes_reloaded = on_routes_reloaded
        self._write_lock = threading.Lock()
        self._lock_file_path = self.channels_path.with_suffix(self.channels_path.suffix + ".lock")
        self.channels_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.channels_path.exists():
            dump_json(self.channels_path, [])

    def list_routes(self) -> list[dict]:
        return [self._canonicalize_route_obj(item) for item in self._load_routes_raw()]

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
        routes.append(self._canonicalize_route_obj(route_obj))
        self._save_and_reload(routes)
        return self._canonicalize_route_obj(route_obj)

    def update_route(self, name: str, patch: dict) -> dict:
        if not isinstance(patch, dict):
            raise ValueError("patch must be object")
        patch = self._normalize_route_payload(patch)
        routes = self._load_routes_raw()
        for idx, item in enumerate(routes):
            if str(item.get("name") or "").strip() != name:
                continue
            updated = self._canonicalize_route_obj(item)
            updated.update(patch)
            if "name" in patch and str(patch.get("name") or "").strip() != name:
                raise ValueError("renaming routes is not supported")
            routes[idx] = self._canonicalize_route_obj(updated)
            self._save_and_reload(routes)
            return routes[idx]
        raise ValueError("route not found")

    def get_route(self, name: str) -> dict:
        for item in self._load_routes_raw():
            if str(item.get("name") or "").strip() == name:
                return self._canonicalize_route_obj(item)
        raise ValueError("route not found")

    def delete_route(self, name: str) -> None:
        routes = self._load_routes_raw()
        filtered = [item for item in routes if str(item.get("name") or "").strip() != name]
        if len(filtered) == len(routes):
            raise ValueError("route not found")
        self._save_and_reload(filtered)

    def set_route_status(self, name: str, status: str) -> dict:
        normalized = str(status or "").strip().lower()
        if normalized not in {"deactive", "syncing", "synced"}:
            raise ValueError("status must be one of: deactive, syncing, synced")
        return self.update_route(name, {"status": normalized})

    def set_route_enabled(self, name: str, enabled: bool) -> dict:
        # Backward compatibility.
        return self.set_route_status(name, "synced" if bool(enabled) else "deactive")

    def update_route_sync(self, name: str, sync_patch: dict) -> dict:
        if not isinstance(sync_patch, dict):
            raise ValueError("sync patch must be object")
        patch: dict[str, object] = {}
        if "backfill_count" in sync_patch:
            patch["backfill_count"] = int(sync_patch.get("backfill_count") or 0)
        if "interval_sec" in sync_patch:
            patch["interval_sec"] = int(sync_patch.get("interval_sec") or 1)
        if "batch_size" in sync_patch:
            patch["batch_size"] = int(sync_patch.get("batch_size") or 1)
        if "retry_attempts" in sync_patch:
            patch["retry_attempts"] = int(sync_patch.get("retry_attempts") or 0)
        return self.update_route(name, patch)

    def start_route_sync(self, name: str) -> dict:
        if self.sync_ledger is not None:
            # Recover records that were paused while route was deactive, so start truly resumes.
            self.sync_ledger.reactivate_route_deactive_blocks(name)
        route = self.get_route(name)
        status = self._determine_start_status(name, route)
        updated = self.set_route_status(name, status)
        enqueued = self._enqueue_route_retryables(name)
        if enqueued > 0 and str(updated.get("status") or "").strip().lower() != "syncing":
            updated = self.set_route_status(name, "syncing")
        return updated

    async def start_route_sync_async(self, name: str) -> dict:
        if self.sync_ledger is not None:
            # Recover records that were paused while route was deactive, so start truly resumes.
            self.sync_ledger.reactivate_route_deactive_blocks(name)
        route = self.get_route(name)
        status = self._determine_start_status(name, route)
        updated = self.set_route_status(name, status)
        enqueued = await self._enqueue_route_retryables_async(name)
        if enqueued > 0 and str(updated.get("status") or "").strip().lower() != "syncing":
            updated = self.set_route_status(name, "syncing")
        return updated

    def stop_route_sync(self, name: str) -> dict:
        return self.set_route_status(name, "deactive")

    async def force_route_sync(self, name: str, *, lock_timeout_sec: float = 10.0) -> dict[str, Any]:
        route_name = str(name or "").strip()
        if not route_name:
            raise ValueError("route name is required")

        route_raw = self.get_route(route_name)
        route_obj = _route_dict_to_channel_route(route_raw)
        lock_owner = f"force-sync:{route_name}:{uuid4()}"

        lock_acquired = False
        queue_purged = 0
        if self.sync_queue is not None:
            deadline = time.monotonic() + max(1.0, float(lock_timeout_sec))
            while time.monotonic() < deadline:
                lock_acquired = bool(
                    await self.sync_queue.acquire_route_lock(
                        route_name=route_name,
                        owner=lock_owner,
                        ttl_sec=30,
                    )
                )
                if lock_acquired:
                    break
                await asyncio.sleep(0.2)
            if not lock_acquired:
                raise RuntimeError("route_sync_busy")

        try:
            if self.sync_queue is not None:
                queue_purged = int(await self.sync_queue.purge_route(route_name=route_name) or 0)

            if self.sync_ledger is not None:
                ledger_reset = self.sync_ledger.clear_route_sync_state(route_name)
            else:
                ledger_reset = {
                    "deleted_checkpoint_rows": 0,
                    "deleted_ledger_rows": 0,
                    "deleted_review_rows": 0,
                }

            source_cursor_reset: dict[str, Any] | None = None
            reset_func = getattr(self.source_client, "reset_route_cursor", None)
            if callable(reset_func):
                out = reset_func(route_obj)
                if inspect.isawaitable(out):
                    out = await out
                if isinstance(out, dict):
                    source_cursor_reset = dict(out)
                else:
                    source_cursor_reset = {"result": bool(out)}

            route = self.update_route(route_name, {"status": "syncing"})
            return {
                "route": route,
                "reset": {
                    **ledger_reset,
                    "purged_queue_items": int(queue_purged),
                    "source_cursor_reset": source_cursor_reset,
                },
            }
        finally:
            if lock_acquired and self.sync_queue is not None:
                await self.sync_queue.release_route_lock(route_name=route_name, owner=lock_owner)

    def start_all_routes(self) -> dict:
        routes = self._load_routes_raw()
        normalized_routes: list[dict] = []
        syncing = 0
        synced = 0
        for item in routes:
            route = self._canonicalize_route_obj(item)
            name = str(route.get("name") or "").strip()
            if not name:
                continue
            next_status = self._determine_start_status(name, route)
            route["status"] = next_status
            if next_status == "syncing":
                syncing += 1
            elif next_status == "synced":
                synced += 1
            normalized_routes.append(route)
        self._save_and_reload(normalized_routes)
        return {"total": len(normalized_routes), "syncing": syncing, "synced": synced}

    def stop_all_routes(self) -> dict:
        routes = self._load_routes_raw()
        normalized_routes: list[dict] = []
        for item in routes:
            route = self._canonicalize_route_obj(item)
            route["status"] = "deactive"
            normalized_routes.append(route)
        self._save_and_reload(normalized_routes)
        return {"total": len(normalized_routes), "deactive": len(normalized_routes)}

    def list_channel_script_files(self) -> list[str]:
        return sorted(p.name for p in self.scripts_dir.glob("*.py") if p.is_file())

    def list_script_files(self) -> list[str]:
        # Backward compatibility with older call-sites.
        return self.list_channel_script_files()

    def list_guard_files(self) -> list[str]:
        return sorted(p.name for p in self.guards_dir.glob("*.py") if p.is_file())

    def read_channel_script_file(self, name: str) -> str:
        file_name = safe_script_name(str(name or "").strip())
        path = self.scripts_dir / file_name
        if not path.exists():
            raise ValueError("channel script not found")
        return path.read_text(encoding="utf-8")

    def write_channel_script_file(self, name: str, content: str) -> dict[str, object]:
        file_name = safe_script_name(str(name or "").strip())
        path = self.scripts_dir / file_name
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(path, str(content or ""))
        return {
            "name": file_name,
            "path": str(path),
            "size_bytes": path.stat().st_size,
            "updated_at": int(path.stat().st_mtime),
        }

    def read_guard_script_file(self, name: str) -> str:
        file_name = safe_script_name(str(name or "").strip())
        path = self.guards_dir / file_name
        if not path.exists():
            raise ValueError("guard script not found")
        return path.read_text(encoding="utf-8")

    def write_guard_script_file(self, name: str, content: str) -> dict[str, object]:
        file_name = safe_script_name(str(name or "").strip())
        path = self.guards_dir / file_name
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(path, str(content or ""))
        return {
            "name": file_name,
            "path": str(path),
            "size_bytes": path.stat().st_size,
            "updated_at": int(path.stat().st_mtime),
        }

    def keyword_links_path(self) -> Path:
        raw = os.getenv("KEYWORD_LINKS_CONFIG_PATH", "").strip()
        if raw:
            return Path(raw)
        return self.channels_path.parent / "keyword_links.json"

    def get_keyword_links(self) -> list[dict]:
        path = self.keyword_links_path()
        if not path.exists():
            return []
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError("keyword links config must be list")
        out: list[dict[str, Any]] = []
        for item in raw:
            normalized = self._normalize_keyword_link_item(item)
            if normalized is not None:
                out.append(normalized)
        return out

    def save_keyword_links(self, payload: list[dict]) -> dict[str, object]:
        if not isinstance(payload, list):
            raise ValueError("keyword links payload must be list")
        cleaned: list[dict[str, Any]] = []
        for item in payload:
            normalized = self._normalize_keyword_link_item(item)
            if normalized is not None:
                cleaned.append(normalized)
        path = self.keyword_links_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_dump_json(path, cleaned)
        return {
            "path": str(path),
            "count": len(cleaned),
            "updated_at": int(path.stat().st_mtime),
        }

    @staticmethod
    def _normalize_keyword_link_item(item: object) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        destination = str(item.get("destination") or "").strip()
        link = str(item.get("link") or "").strip()
        if not destination:
            return None

        entry_priority_raw = item.get("priority")
        try:
            entry_priority = int(entry_priority_raw or 0)
        except Exception:
            entry_priority = 0

        keywords_raw = item.get("keywords")
        if not isinstance(keywords_raw, list):
            return None

        keywords: list[dict[str, object]] = []
        seen: set[str] = set()
        for kw in keywords_raw:
            keyword_text = ""
            keyword_priority = entry_priority
            if isinstance(kw, str):
                keyword_text = kw.strip()
            elif isinstance(kw, dict):
                keyword_text = str(kw.get("keyword") or kw.get("text") or kw.get("term") or "").strip()
                try:
                    keyword_priority = int(kw.get("priority") if kw.get("priority") is not None else entry_priority)
                except Exception:
                    keyword_priority = entry_priority
            if not keyword_text:
                continue
            dedupe_key = keyword_text.casefold()
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            keywords.append({"keyword": keyword_text, "priority": int(keyword_priority)})

        if not keywords:
            return None

        keywords.sort(key=lambda obj: (-int(obj.get("priority") or 0), -len(str(obj.get("keyword") or ""))))
        return {
            "destination": destination,
            "link": link,
            "keywords": keywords,
        }

    def sync_stats_snapshot(self) -> dict:
        status_counts = self.sync_ledger.get_status_counts() if self.sync_ledger is not None else {}
        per_route, per_route_metrics = self._route_sync_stats_from_ledger()
        # Snapshot path is synchronous and may be called from non-service threads (web admin handler).
        # Avoid touching async queue clients here to prevent cross-event-loop failures; derive a stable estimate.
        if self.sync_queue is None:
            queue_depth = 0
        else:
            queue_depth = int(status_counts.get("queued", 0) or 0)
        open_reviews = self.sync_ledger.open_review_count() if self.sync_ledger is not None else 0
        return {
            "queue_depth": int(queue_depth),
            "open_reviews": int(open_reviews),
            "status_counts": status_counts,
            "routes": per_route,
            "route_metrics": per_route_metrics,
        }

    async def sync_stats(self, *, include_source_probe: bool = False, source_probe_timeout_sec: float = 0.9) -> dict:
        status_counts = self.sync_ledger.get_status_counts() if self.sync_ledger is not None else {}
        queue_depth = await self.sync_queue.depth() if self.sync_queue is not None else 0
        open_reviews = self.sync_ledger.open_review_count() if self.sync_ledger is not None else 0
        per_route, per_route_metrics = self._route_sync_stats_from_ledger()

        if include_source_probe and self.sync_ledger is not None:
            latest_func = getattr(self.source_client, "latest_message_id_for_route", None)
            if callable(latest_func):
                for route in self._load_routes_raw():
                    name = str(route.get("name") or "").strip()
                    if not name:
                        continue
                    fallback = per_route_metrics.get(name) or _build_route_sync_metrics({})
                    try:
                        latest = int(
                            await asyncio.wait_for(
                                latest_func(_route_dict_to_channel_route(route)),
                                timeout=max(0.1, float(source_probe_timeout_sec)),
                            )
                        )
                        checkpoint = int(self.sync_ledger.get_route_checkpoint(name) or 0)
                        backfill_count = int(route.get("backfill_count", 100) or 100)
                        per_route_metrics[name] = _build_route_sync_metrics_from_checkpoint(
                            latest=latest,
                            checkpoint=checkpoint,
                            backfill_count=backfill_count,
                            fallback=fallback,
                        )
                    except Exception:
                        per_route_metrics[name] = fallback
        return {
            "queue_depth": int(queue_depth),
            "open_reviews": int(open_reviews),
            "status_counts": status_counts,
            "routes": per_route,
            "route_metrics": per_route_metrics,
        }

    def _route_sync_stats_from_ledger(self) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int | float]]]:
        per_route: dict[str, dict[str, int]] = {}
        per_route_metrics: dict[str, dict[str, int | float]] = {}
        for route in self._load_routes_raw():
            name = str(route.get("name") or "").strip()
            if not name:
                continue
            if self.sync_ledger is None:
                counts: dict[str, int] = {}
            else:
                counts = self.sync_ledger.get_route_status_counts(name)
            per_route[name] = counts
            per_route_metrics[name] = _build_route_sync_metrics(counts)
        return per_route, per_route_metrics

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

    def _enqueue_route_retryables(self, route_name: str, *, limit: int = 5000) -> int:
        if self.sync_ledger is None or self.sync_queue is None:
            return 0
        keys = self.sync_ledger.list_retryable_keys_for_route(route_name, limit=max(1, int(limit)))
        if not keys:
            return 0

        async def _enqueue_all() -> None:
            now = time.time()
            for idx, key in enumerate(keys):
                await self.sync_queue.enqueue(
                    dedupe_key=key,
                    route_name=route_name,
                    due_at=now + (0.002 * idx),
                    payload_hash="",
                )

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_enqueue_all())
        except RuntimeError:
            asyncio.run(_enqueue_all())
        return len(keys)

    async def _enqueue_route_retryables_async(self, route_name: str, *, limit: int = 5000) -> int:
        if self.sync_ledger is None or self.sync_queue is None:
            return 0
        keys = self.sync_ledger.list_retryable_keys_for_route(route_name, limit=max(1, int(limit)))
        if not keys:
            return 0
        now = time.time()
        for idx, key in enumerate(keys):
            await self.sync_queue.enqueue(
                dedupe_key=key,
                route_name=route_name,
                due_at=now + (0.002 * idx),
                payload_hash="",
            )
        return len(keys)

    def reload_routes(self) -> None:
        registry = load_routes(str(self.channels_path))
        self.on_routes_reloaded(registry)

    def _save_and_reload(self, routes: list[dict]) -> None:
        cleaned = [self._canonicalize_route_obj(item) for item in routes if isinstance(item, dict)]
        with self._atomic_file_lock():
            _atomic_dump_json(self.channels_path, cleaned)
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
        if "enabled" in obj and "status" not in out:
            out["status"] = "synced" if bool(obj.get("enabled")) else "deactive"
        if "channel_script" not in out and "script" in out:
            out["channel_script"] = out.get("script")
        out.pop("script", None)
        if "source_channel_username" in out:
            source_username = str(out.get("source_channel_username") or "").strip()
            out["source_channel_username"] = source_username or None
        if "source_channel_id" in out:
            source_id = str(out.get("source_channel_id") or "").strip()
            out["source_channel_id"] = source_id or None
        legacy_sync_obj = obj.get("sync")
        if isinstance(legacy_sync_obj, dict):
            if "backfill_count" not in out and "backfill_count" in legacy_sync_obj:
                out["backfill_count"] = legacy_sync_obj.get("backfill_count")
            if "interval_sec" not in out and "interval_sec" in legacy_sync_obj:
                out["interval_sec"] = legacy_sync_obj.get("interval_sec")
            if "batch_size" not in out and "batch_size" in legacy_sync_obj:
                out["batch_size"] = legacy_sync_obj.get("batch_size")
            if "retry_attempts" not in out and "retry_attempts" in legacy_sync_obj:
                out["retry_attempts"] = legacy_sync_obj.get("retry_attempts")
        for key in ("channel_script", "gaurd_script"):
            if key not in out:
                continue
            value = out.get(key)
            if value is None:
                continue
            if isinstance(value, str) and not value.strip():
                out[key] = None
        if "status" in out:
            normalized = str(out.get("status") or "").strip().lower()
            if normalized not in {"deactive", "syncing", "synced"}:
                raise ValueError("status must be one of: deactive, syncing, synced")
            out["status"] = normalized
        if "backfill_count" in out:
            out["backfill_count"] = max(0, int(out.get("backfill_count") or 0))
        if "interval_sec" in out:
            out["interval_sec"] = max(1, int(out.get("interval_sec") or 1))
        if "batch_size" in out:
            out["batch_size"] = max(1, int(out.get("batch_size") or 1))
        if "retry_attempts" in out:
            out["retry_attempts"] = max(0, int(out.get("retry_attempts") or 0))
        return out

    @staticmethod
    def _canonicalize_route_obj(route: dict) -> dict:
        obj = dict(route)
        normalized = ManagementApi._normalize_route_payload(obj)
        out = dict(obj)
        out.update(normalized)
        status = str(out.get("status") or "").strip().lower()
        if status not in {"deactive", "syncing", "synced"}:
            legacy_enabled = bool(out.get("enabled", True))
            legacy_sync = out.get("sync") if isinstance(out.get("sync"), dict) else {}
            if bool(legacy_sync.get("enabled", False)):
                status = "syncing"
            elif legacy_enabled:
                status = "synced"
            else:
                status = "deactive"
        out["status"] = status
        out["backfill_count"] = max(0, int(out.get("backfill_count", 100) or 100))
        out["interval_sec"] = max(1, int(out.get("interval_sec", 1) or 1))
        out["batch_size"] = max(1, int(out.get("batch_size", 1) or 1))
        out["retry_attempts"] = max(0, int(out.get("retry_attempts", 2) or 2))
        out.pop("sync", None)
        out.pop("enabled", None)
        return out

    def _determine_start_status(self, route_name: str, route: dict) -> str:
        pending = 0
        remaining_from_ledger = 0
        checkpoint = 0
        if self.sync_ledger is not None:
            pending = int(self.sync_ledger.active_count_for_route(route_name) or 0)
            checkpoint = int(self.sync_ledger.get_route_checkpoint(route_name) or 0)
            counts = self.sync_ledger.get_route_status_counts(route_name)
            remaining_from_ledger = (
                int(counts.get("queued", 0) or 0)
                + int(counts.get("processing", 0) or 0)
                + int(counts.get("failed", 0) or 0)
                + int(counts.get("ambiguous", 0) or 0)
            )
        if pending > 0 or remaining_from_ledger > 0:
            return "syncing"
        # This method can run from the web-admin thread. Do not probe Telethon here:
        # source client methods must run only on the service event loop.
        if self.sync_ledger is None:
            return "syncing"
        return "synced" if checkpoint > 0 else "syncing"

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
    prev_stat = None
    try:
        if path.exists():
            prev_stat = path.stat()
    except Exception:
        prev_stat = None
    fd, temp_path = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, indent=2))
            fh.flush()
            os.fsync(fh.fileno())
        if prev_stat is not None:
            try:
                os.chown(temp_path, int(prev_stat.st_uid), int(prev_stat.st_gid))
            except Exception:
                pass
            try:
                os.chmod(temp_path, int(prev_stat.st_mode) & 0o777)
            except Exception:
                pass
        else:
            try:
                os.chmod(temp_path, 0o664)
            except Exception:
                pass
        os.replace(temp_path, path)
    finally:
        try:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        except Exception:
            pass


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    prev_stat = None
    try:
        if path.exists():
            prev_stat = path.stat()
    except Exception:
        prev_stat = None
    fd, temp_path = tempfile.mkstemp(prefix=path.name, suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(str(content))
            fh.flush()
            os.fsync(fh.fileno())
        if prev_stat is not None:
            try:
                os.chown(temp_path, int(prev_stat.st_uid), int(prev_stat.st_gid))
            except Exception:
                pass
            try:
                os.chmod(temp_path, int(prev_stat.st_mode) & 0o777)
            except Exception:
                pass
        else:
            try:
                os.chmod(temp_path, 0o664)
            except Exception:
                pass
        os.replace(temp_path, path)
    finally:
        try:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
        except Exception:
            pass


def _build_route_sync_metrics(counts: dict[str, int]) -> dict[str, int | float]:
    sent = int(counts.get("sent", 0) or 0)
    blocked = int(counts.get("blocked", 0) or 0)
    skipped = int(counts.get("skipped", 0) or 0)
    queued = int(counts.get("queued", 0) or 0)
    processing = int(counts.get("processing", 0) or 0)
    failed = int(counts.get("failed", 0) or 0)
    ambiguous = int(counts.get("ambiguous", 0) or 0)

    done = sent + blocked + skipped
    remaining = queued + processing + failed + ambiguous
    total_seen = done + remaining
    progress_pct = 100.0 if total_seen <= 0 else round((done * 100.0) / total_seen, 1)

    return {
        "total_seen": int(total_seen),
        "done": int(done),
        "remaining_unsynced": int(remaining),
        "progress_pct": float(progress_pct),
        "sent": int(sent),
        "blocked": int(blocked),
        "skipped": int(skipped),
        "queued": int(queued),
        "processing": int(processing),
        "failed": int(failed),
        "ambiguous": int(ambiguous),
    }


def _build_route_sync_metrics_from_checkpoint(
    *,
    latest: int,
    checkpoint: int,
    backfill_count: int,
    fallback: dict[str, int | float],
) -> dict[str, int | float]:
    latest_n = max(0, int(latest))
    checkpoint_n = max(0, int(checkpoint))
    backfill_n = max(0, int(backfill_count))
    if latest_n <= 0:
        return fallback

    if backfill_n > 0:
        window_start = max(0, latest_n - backfill_n)
    else:
        window_start = checkpoint_n
    total_window = max(0, latest_n - window_start)
    done = min(total_window, max(0, checkpoint_n - window_start))
    remaining = max(0, total_window - done)
    progress_pct = 100.0 if total_window <= 0 else round((done * 100.0) / total_window, 1)

    out = dict(fallback)
    out["total_seen"] = int(total_window)
    out["done"] = int(done)
    out["remaining_unsynced"] = int(remaining)
    out["progress_pct"] = float(progress_pct)
    return out


def _route_dict_to_channel_route(route: dict) -> "ChannelRoute":
    from kiwi.types import ChannelRoute

    sync_obj = route.get("sync") if isinstance(route.get("sync"), dict) else {}
    return ChannelRoute(
        name=str(route.get("name") or ""),
        status=str(route.get("status") or "deactive"),
        source_channel_id=str(route.get("source_channel_id") or "") or None,
        source_channel_username=str(route.get("source_channel_username") or "") or None,
        destination_channel_id=str(route.get("destination_channel_id") or "") or None,
        destination_channel_username=str(route.get("destination_channel_username") or "") or None,
        channel_script=str(route.get("channel_script") or "") or None,
        max_message_mb=int(route.get("max_message_mb")) if route.get("max_message_mb") is not None else None,
        gaurd_script=str(route.get("gaurd_script") or "") or None,
        sync_backfill_count=max(0, int(route.get("backfill_count", sync_obj.get("backfill_count", 100)))),
        sync_interval_sec=max(1, int(route.get("interval_sec", sync_obj.get("interval_sec", 1)))),
        sync_batch_size=max(1, int(route.get("batch_size", sync_obj.get("batch_size", 1)))),
        sync_retry_attempts=max(0, int(route.get("retry_attempts", sync_obj.get("retry_attempts", 2)))),
    )
