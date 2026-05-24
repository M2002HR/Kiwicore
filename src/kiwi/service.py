from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import re
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from kiwi.config import RouteRegistry, Settings
from kiwi.dispatcher import BaleDispatcher
from kiwi.errors import GuardExecutionError, MessageTooLargeError, PlatformApiError, ScriptExecutionError
from kiwi.guard_runner import GuardRunner
from kiwi.keyword_links import KeywordLinker
from kiwi.message_monitor import MessageMonitor
from kiwi.platforms.parser import parse_telegram_channel_update, parse_telegram_private_message_update
from kiwi.script_runner import ScriptRunner
from kiwi.state import StateStore
from kiwi.storage import StorageManager
from kiwi.sync_ledger import SyncLedger, TERMINAL_STATUSES
from kiwi.sync_queue import InMemorySyncQueue, SyncQueueBackend, payload_hash_from_json
from kiwi.traffic_history import TrafficHistory
from kiwi.types import (
    AdminInboundMessage,
    ChannelRoute,
    IncomingChannelMessage,
    IncomingMedia,
    MediaKind,
    OutputMessageKind,
    ScriptOutputMessage,
    ScriptRunResult,
)

logger = logging.getLogger(__name__)


class KiwiService:
    def __init__(
        self,
        *,
        settings: Settings,
        routes: RouteRegistry,
        telegram_client,
        bale_client,
        source_client=None,
        storage: StorageManager,
        guard_runner: GuardRunner,
        script_runner: ScriptRunner,
        state_store: StateStore,
        sync_ledger: SyncLedger | None = None,
        sync_queue: SyncQueueBackend | None = None,
        admin_handler: Any | None = None,
        route_patch_callback: Any | None = None,
    ) -> None:
        self.settings = settings
        self.routes = routes
        self.telegram_client = telegram_client
        self.bale_client = bale_client
        self.source_client = source_client
        self.storage = storage
        self.guard_runner = guard_runner
        self.script_runner = script_runner
        self.state_store = state_store
        self.sync_ledger = sync_ledger or SyncLedger(str(Path(settings.storage_dir) / "sync_ledger.sqlite3"))
        self.sync_queue = sync_queue or InMemorySyncQueue()
        self.dispatcher = BaleDispatcher(self.bale_client)
        self.keyword_linker = KeywordLinker(channels_config_path=settings.channels_config_path)
        fallback_mode = os.getenv("BALE_MEDIA_UPLOAD_FALLBACK_MODE", "none").strip().lower()
        if fallback_mode not in {"none", "text", "document"}:
            fallback_mode = "none"
        self.dispatcher.media_upload_fallback_mode = fallback_mode
        self._bale_bot_user_id = self._parse_bot_user_id(settings.bale_bot_token)
        self._dispatch_permission_cache_ttl_sec = max(60.0, float(os.getenv("DISPATCH_PERMISSION_CACHE_TTL_SEC", "300")))
        self._dispatch_permission_cache: dict[str, tuple[float, bool, str | None]] = {}
        self._media_cache_enabled = os.getenv("SYNC_MEDIA_CACHE_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
        self._media_cache_dir = Path(settings.storage_dir) / "media_cache"
        self._media_cache_dir.mkdir(parents=True, exist_ok=True)
        self._media_cache_ttl_sec = self._read_bounded_int_env("SYNC_MEDIA_CACHE_TTL_SEC", default=259200, low=3600, high=1209600)
        self._media_cache_prune_interval_sec = self._read_bounded_int_env(
            "SYNC_MEDIA_CACHE_PRUNE_INTERVAL_SEC",
            default=900,
            low=60,
            high=43200,
        )
        self._media_cache_last_prune_at = 0.0
        try:
            ai_retry_cap = int(os.getenv("SYNC_AI_REQUIRED_MAX_ATTEMPTS", "6"))
        except Exception:
            ai_retry_cap = 6
        self._ai_required_max_attempts = max(1, min(50, ai_retry_cap))
        self.admin_handler = admin_handler
        self._route_patch_callback = route_patch_callback

        self._offset: int | None = self.state_store.load_offset()
        self._stop_event = asyncio.Event()
        self._pending_media_groups: dict[tuple[str, str, str], dict[str, object]] = {}
        self._retry_queue_hydrated = False
        self._sync_drain_task: asyncio.Task[int] | None = None
        self._sync_next_due_at: dict[str, float] = {}
        self._next_stale_recover_at = 0.0
        self._started_at_ts = time.time()
        self._run_id = str(uuid4())
        self._run_once_calls = 0
        self._processed_messages_total = 0
        self._last_run_once_ts: float | None = None
        self._consecutive_poll_errors = 0
        self._last_poll_error: str | None = None
        self._traffic_lock = threading.RLock()
        self._traffic_total_download_bytes = 0
        self._traffic_total_upload_bytes = 0
        self._traffic_today_download_bytes = 0
        self._traffic_today_upload_bytes = 0
        self._traffic_by_route_total: dict[str, dict[str, int]] = {}
        self._traffic_by_route_today: dict[str, dict[str, int]] = {}
        self._traffic_day = self._current_traffic_day()
        self._traffic_history = TrafficHistory(
            Path(self.settings.storage_dir) / "traffic_history.json",
            run_id=self._run_id,
            started_at_ts=self._started_at_ts,
        )
        self.message_monitor = MessageMonitor()

    @staticmethod
    def _read_bounded_int_env(name: str, *, default: int, low: int, high: int) -> int:
        raw = os.getenv(name, str(default)).strip()
        try:
            value = int(raw)
        except Exception:
            value = default
        return max(low, min(high, value))

    async def stop(self) -> None:
        self._stop_event.set()
        if self._sync_drain_task is not None and not self._sync_drain_task.done():
            self._sync_drain_task.cancel()

    async def aclose(self) -> None:
        await self.telegram_client.aclose()
        await self.bale_client.aclose()
        if self.source_client is not None:
            await self.source_client.aclose()

    async def run(self) -> None:
        logger.info("Kiwi service started")
        try:
            while not self._stop_event.is_set():
                try:
                    updates_count = await self.run_once()
                    self._run_once_calls += 1
                    self._processed_messages_total += max(0, int(updates_count))
                    self._last_run_once_ts = time.time()
                    self._consecutive_poll_errors = 0
                    self._last_poll_error = None
                    if updates_count == 0:
                        backlog = await self._sync_queue_depth_safe()
                        if backlog > 0:
                            await asyncio.sleep(min(max(0.05, self.settings.poll_idle_sleep_sec), 0.25))
                        else:
                            await asyncio.sleep(self.settings.poll_idle_sleep_sec)
                except asyncio.CancelledError:
                    raise
                except PlatformApiError as exc:
                    self._consecutive_poll_errors += 1
                    self._last_poll_error = str(exc)
                    delay = min(self.settings.poll_error_sleep_sec * self._consecutive_poll_errors, 60.0)
                    logger.warning(
                        "Polling failed; retrying",
                        extra={
                            "details": {
                                "error": str(exc),
                                "retry_in_sec": delay,
                                "consecutive_errors": self._consecutive_poll_errors,
                            }
                        },
                    )
                    await asyncio.sleep(delay)
                except Exception:
                    self._consecutive_poll_errors += 1
                    self._last_poll_error = "unexpected_error"
                    delay = min(self.settings.poll_error_sleep_sec * self._consecutive_poll_errors, 60.0)
                    logger.exception(
                        "Unexpected error in polling loop; retrying",
                        extra={
                            "details": {
                                "retry_in_sec": delay,
                                "consecutive_errors": self._consecutive_poll_errors,
                            }
                        },
                    )
                    await asyncio.sleep(delay)
        finally:
            self._close_traffic_history()
            await self.aclose()
            logger.info("Kiwi service stopped")

    async def run_once(self) -> int:
        await self._ensure_sync_baseline()
        await self._hydrate_retry_queue_once()
        processed_pre = 0
        backlog_before_poll = await self._sync_queue_depth_safe()
        if backlog_before_poll > 0:
            # Keep sync backlog moving even when Bot API long-poll waits for updates.
            processed_pre += await self._tick_sync_drain()
            backlog_before_poll = await self._sync_queue_depth_safe()

        updates: list[dict]
        poll_timeout = int(self.settings.telegram_poll_timeout_sec)
        if backlog_before_poll > 0:
            poll_timeout = 1
        try:
            updates = await self.telegram_client.get_updates(
                offset=self._offset,
                timeout=poll_timeout,
                allowed_updates=self.settings.telegram_allowed_updates,
            )
        except PlatformApiError as exc:
            can_continue_without_bot = (
                self.source_client is not None and self.settings.telegram_source_mode in {"telethon", "hybrid"}
            )
            if not can_continue_without_bot:
                raise
            logger.warning(
                "Telegram Bot API polling failed; continuing with Telethon source",
                extra={
                    "details": {
                        "error": str(exc),
                        "source_mode": self.settings.telegram_source_mode,
                    }
                },
            )
            updates = []
        telethon_messages: list[IncomingChannelMessage] = []
        if self.source_client is not None:
            try:
                telethon_messages = await self.source_client.poll_messages(self.routes.routes)
            except Exception:
                logger.exception("Telethon polling failed")
                if self.settings.telegram_source_mode == "telethon":
                    raise
        if not updates:
            telethon_pairs: list[tuple[IncomingChannelMessage, ChannelRoute]] = []
            for incoming in telethon_messages:
                routes = self.routes.match_all(incoming.source_channel_id, incoming.source_channel_username)
                for route in routes:
                    telethon_pairs.append((incoming, route))
            ready = self._collect_ready_messages(telethon_pairs)
            ready.extend(self._flush_ready_media_groups(force=False))
            ready.sort(key=lambda item: item[0].update_id)
            processed = int(processed_pre)
            for incoming, route in ready:
                if route.is_deactive():
                    continue
                await self._enqueue_sync_message(route, incoming)
            processed += await self._tick_sync_drain()
            await self._auto_activate_ready_routes()
            return processed

        matched_messages: list[tuple[IncomingChannelMessage, ChannelRoute]] = []
        for update in updates:
            update_id = int(update.get("update_id") or 0)
            if update_id:
                self._offset = update_id + 1
                self.state_store.save_offset(self._offset)

            incoming = parse_telegram_channel_update(update)
            private_incoming = parse_telegram_private_message_update(update)
            if private_incoming is not None and self.settings.admin_bot_enabled and self.admin_handler is not None:
                await self._process_admin_message(private_incoming)
                continue
            if incoming is None:
                continue

            routes = self.routes.match_all(incoming.source_channel_id, incoming.source_channel_username)
            if not routes:
                continue

            for route in routes:
                matched_messages.append((incoming, route))

        for incoming in telethon_messages:
            routes = self.routes.match_all(incoming.source_channel_id, incoming.source_channel_username)
            for route in routes:
                matched_messages.append((incoming, route))
        ready_messages = self._collect_ready_messages(matched_messages)
        ready_messages.extend(self._flush_ready_media_groups(force=False))

        deduped: list[tuple[IncomingChannelMessage, ChannelRoute]] = []
        seen_keys: set[tuple[str, str, int, str | None]] = set()
        for incoming, route in ready_messages:
            key = (route.name, incoming.source_channel_id, incoming.message_id, incoming.media_group_id)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            deduped.append((incoming, route))
        ready_messages = deduped
        ready_messages.sort(key=lambda item: item[0].update_id)

        processed = int(processed_pre)
        for incoming, route in ready_messages:
            if route.is_deactive():
                continue
            await self._enqueue_sync_message(route, incoming)

        processed += await self._tick_sync_drain()
        await self._auto_activate_ready_routes()
        return processed

    async def _sync_queue_depth_safe(self) -> int:
        try:
            return max(0, int(await self.sync_queue.depth()))
        except Exception:
            logger.exception("Failed to read sync queue depth")
            return 0

    def set_route_patch_callback(self, callback: Any | None) -> None:
        self._route_patch_callback = callback

    async def _auto_activate_ready_routes(self) -> None:
        for route in list(self.routes.routes):
            await self._maybe_auto_activate_route(route)

    async def _maybe_auto_activate_route(self, route: ChannelRoute) -> None:
        if not route.is_syncing():
            return

        pending = self.sync_ledger.active_count_for_route(route.name)
        if pending > 0:
            return

        checkpoint = int(self.sync_ledger.get_route_checkpoint(route.name) or 0)
        # Keep brand-new routes in syncing until at least one source message is confirmed.
        if checkpoint <= 0:
            return
        latest = checkpoint
        latest_func = getattr(self.source_client, "latest_message_id_for_route", None)
        if callable(latest_func):
            try:
                latest = max(0, int(await latest_func(route)))
            except Exception:
                logger.exception(
                    "Failed to fetch latest source message id for sync auto activation",
                    extra={"details": {"route": route.name}},
                )
                return

        if latest <= 0:
            return
        if latest > checkpoint:
            return

        if route.is_synced():
            return

        route.status = "synced"

        logger.info(
            "Route sync completed and marked as synced",
            extra={
                "details": {
                    "route": route.name,
                    "checkpoint": checkpoint,
                    "latest_source_message_id": latest,
                }
            },
        )
        await self._persist_route_patch(
            route.name,
            {
                "status": "synced",
                "backfill_count": int(route.sync_backfill_count),
                "interval_sec": int(route.sync_interval_sec),
                "batch_size": int(route.sync_batch_size),
                "retry_attempts": int(route.sync_retry_attempts),
            },
        )

    async def _persist_route_patch(self, route_name: str, patch: dict[str, Any]) -> None:
        callback = self._route_patch_callback
        if callback is None:
            return
        try:
            result = callback(route_name, patch)
            if inspect.isawaitable(result):
                await result
        except Exception:
            logger.exception(
                "Failed to persist route patch",
                extra={"details": {"route": route_name, "patch": patch}},
            )

    async def _tick_sync_drain(self) -> int:
        processed = 0
        task = self._sync_drain_task
        if task is not None and task.done():
            try:
                processed = int(task.result())
            except asyncio.CancelledError:
                processed = 0
            except Exception:
                logger.exception("Background sync drain task failed")
            self._sync_drain_task = None

        if self._sync_drain_task is None:
            self._sync_drain_task = asyncio.create_task(self._drain_sync_queue())
        try:
            processed += int(await self._sync_drain_task)
        finally:
            self._sync_drain_task = None
        return processed

    async def _enqueue_sync_message(self, route: ChannelRoute, incoming: IncomingChannelMessage) -> None:
        checkpoint = self.sync_ledger.get_route_checkpoint(route.name)
        if checkpoint is not None and int(incoming.message_id) <= checkpoint:
            return

        if route.is_synced():
            route.status = "syncing"
            await self._persist_route_patch(route.name, {"status": "syncing"})

        payload = self._serialize_incoming(incoming)
        created, dedupe_key, status = self.sync_ledger.register_message(
            route_name=route.name,
            source_channel_id=incoming.source_channel_id,
            message_id=incoming.message_id,
            media_group_id=incoming.media_group_id,
            payload=payload,
        )
        self.message_monitor.register_message(
            dedupe_key=dedupe_key,
            route_name=route.name,
            source_channel_id=incoming.source_channel_id,
            source_channel_username=incoming.source_channel_username,
            message_id=incoming.message_id,
            media_group_id=incoming.media_group_id,
            status=("queued" if created else status),
            payload=payload,
        )
        if not created and status in TERMINAL_STATUSES:
            return
        # Sync pacing is enforced after successful dispatch (worker-side),
        # so queue entries are admitted immediately.
        due_at = time.time()
        self.message_monitor.note_stage(
            dedupe_key=dedupe_key,
            stage="queued",
            status="queued",
            progress_pct=8.0,
            details="Queued to worker",
            trace_id=f"sync:{dedupe_key}",
        )
        await self.sync_queue.enqueue(
            dedupe_key=dedupe_key,
            route_name=route.name,
            due_at=due_at,
            payload_hash=payload_hash_from_json(payload),
        )

    @staticmethod
    def _initial_sync_jitter_sec(route_name: str, interval_sec: int) -> float:
        # Backward-compat helper kept for tests and diagnostics.
        interval = max(1, int(interval_sec))
        digest = hashlib.sha256(str(route_name or "").encode("utf-8")).digest()
        jitter_bucket = int.from_bytes(digest[:4], byteorder="big", signed=False)
        return float(jitter_bucket % interval)

    async def _ensure_sync_baseline(self) -> None:
        prime_cursor = getattr(self.source_client, "prime_cursor", None)
        route_source_key = getattr(self.source_client, "route_source_key", None)
        latest_func = getattr(self.source_client, "latest_message_id_for_route", None)
        for route in self.routes.routes:
            if route.is_deactive():
                continue

            current_checkpoint = self.sync_ledger.get_route_checkpoint(route.name)
            checkpoint_now = int(current_checkpoint or 0)
            if checkpoint_now <= 0 and route.is_synced():
                # Self-heal stale "synced" routes that have never established a valid source baseline.
                route.status = "syncing"
                await self._persist_route_patch(route.name, {"status": "syncing"})
            if current_checkpoint is None:
                latest_from_source = 0
                if callable(latest_func):
                    try:
                        latest_from_source = max(0, int(await asyncio.wait_for(latest_func(route), timeout=3.0)))
                    except asyncio.TimeoutError:
                        logger.warning(
                            "Sync baseline latest message lookup timed out",
                            extra={"details": {"route": route.name, "timeout_sec": 3.0}},
                        )
                    except Exception as exc:
                        err_text = str(exc or "").strip().lower()
                        if ("unable to resolve source entity" in err_text) or ("could not find the input entity" in err_text):
                            logger.warning(
                                "Sync baseline waiting for resolvable Telethon entity",
                                extra={"details": {"route": route.name, "error": str(exc)}},
                            )
                        else:
                            logger.exception(
                                "Failed to fetch latest source message id for sync baseline",
                                extra={"details": {"route": route.name}},
                            )
                baseline = 0
                if latest_from_source > 0:
                    # Keep at most backfill_count historical messages on first sync bootstrap.
                    baseline = max(0, latest_from_source - max(0, int(route.sync_backfill_count)))
                self.sync_ledger.set_route_checkpoint(route.name, baseline)
                current_checkpoint = baseline
                if latest_from_source > int(current_checkpoint) and route.status != "syncing":
                    route.status = "syncing"
                    await self._persist_route_patch(route.name, {"status": "syncing"})
                if latest_from_source > 0 and latest_from_source <= int(current_checkpoint) and route.status == "syncing":
                    route.status = "synced"
                    await self._persist_route_patch(route.name, {"status": "synced"})

            checkpoint = int(current_checkpoint or 0)
            if callable(prime_cursor) and callable(route_source_key):
                source_key = route_source_key(route)
                if isinstance(source_key, str) and source_key.strip():
                    prime_cursor(source_key, checkpoint)

    def _latest_message_id_from_storage(self, route: ChannelRoute) -> int:
        messages_root = Path(self.settings.storage_dir) / "messages"
        if not messages_root.exists():
            return 0
        max_seen = 0
        for raw_path in messages_root.glob("*/**/raw_update.json"):
            try:
                raw = json.loads(raw_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            incoming = parse_telegram_channel_update(raw)
            if incoming is None:
                continue
            if route.source_channel_username and incoming.source_channel_username != route.source_channel_username:
                continue
            max_seen = max(max_seen, int(incoming.message_id))
        return max_seen

    async def _hydrate_retry_queue_once(self) -> None:
        if self._retry_queue_hydrated:
            return
        stale_cutoff_sec = max(60, int(self.settings.sync_lock_ttl_sec) * 2)
        recovered = self.sync_ledger.requeue_stale_processing(older_than_sec=stale_cutoff_sec)
        if recovered > 0:
            logger.warning(
                "Recovered stale processing sync records",
                extra={
                    "details": {
                        "count": recovered,
                        "stale_cutoff_sec": stale_cutoff_sec,
                    }
                },
            )
        keys = self.sync_ledger.list_retryable_keys(limit=5000)
        for idx, key in enumerate(keys):
            record = self.sync_ledger.get_record(key)
            if record is None:
                continue
            payload_message = record.payload.get("message") if isinstance(record.payload, dict) else {}
            source_username = None
            if isinstance(payload_message, dict):
                source_username = str(payload_message.get("source_channel_username") or "") or None
            self.message_monitor.register_message(
                dedupe_key=key,
                route_name=record.route_name,
                source_channel_id=record.source_channel_id,
                source_channel_username=source_username,
                message_id=record.message_id,
                media_group_id=record.media_group_id if record.media_group_id != "-" else None,
                status=record.status,
                payload=record.payload,
            )
            # Keep tiny staggering to avoid bursty lock contention on restart.
            due_at = time.time() + (0.005 * idx)
            await self.sync_queue.enqueue(
                dedupe_key=key,
                route_name=record.route_name,
                due_at=due_at,
                payload_hash=payload_hash_from_json(record.payload),
            )
        self._retry_queue_hydrated = True

    def _find_route(self, route_name: str) -> ChannelRoute | None:
        for route in self.routes.routes:
            if route.name == route_name:
                return route
        return None

    async def _drain_sync_queue(self) -> int:
        await self._recover_stale_sync_records()
        batch_limit = max(1, int(self.settings.sync_worker_count)) * 4
        items = await self.sync_queue.pop_due(limit=batch_limit, now_ts=time.time())
        if not items:
            return 0

        semaphore = asyncio.Semaphore(max(1, int(self.settings.sync_worker_count)))
        processed = 0

        async def _run_one(item) -> int:
            async with semaphore:
                return await self._process_queued_item(item)

        results = await asyncio.gather(*[_run_one(item) for item in items], return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                logger.exception("Sync worker task failed", exc_info=result)
                continue
            processed += int(result)
        return processed

    async def _recover_stale_sync_records(self) -> None:
        now = time.time()
        if now < float(self._next_stale_recover_at):
            return

        stale_cutoff_sec = max(60, int(self.settings.sync_lock_ttl_sec) * 2)
        # Run periodically without waiting for restart, so stuck "processing" records
        # cannot stall queue progress for long periods.
        self._next_stale_recover_at = now + max(30.0, float(stale_cutoff_sec) / 2.0)
        recovered = int(self.sync_ledger.requeue_stale_processing(older_than_sec=stale_cutoff_sec) or 0)
        if recovered <= 0:
            return

        hydrated = 0
        for idx, key in enumerate(self.sync_ledger.list_retryable_keys(limit=max(2000, recovered * 4))):
            record = self.sync_ledger.get_record(key)
            if record is None:
                continue
            await self.sync_queue.enqueue(
                dedupe_key=key,
                route_name=record.route_name,
                due_at=now + (0.002 * idx),
                payload_hash=payload_hash_from_json(record.payload),
            )
            hydrated += 1

        logger.warning(
            "Recovered stale processing sync records during runtime",
            extra={
                "details": {
                    "recovered": recovered,
                    "requeued": hydrated,
                    "stale_cutoff_sec": stale_cutoff_sec,
                }
            },
        )

    async def _process_queued_item(self, item) -> int:
        record = self.sync_ledger.get_record(item.dedupe_key)
        if record is None:
            return 0
        if record.status in TERMINAL_STATUSES:
            return 0
        self.message_monitor.note_stage(
            dedupe_key=item.dedupe_key,
            stage="dequeue",
            status="processing",
            progress_pct=12.0,
            details="Picked by sync worker",
            trace_id=f"sync:{item.dedupe_key}",
        )

        route = self._find_route(record.route_name)
        if route is None:
            self.sync_ledger.mark_status(item.dedupe_key, status="blocked", last_error="route_not_found")
            self.message_monitor.note_status(
                dedupe_key=item.dedupe_key,
                status="blocked",
                error="route_not_found",
                details="Route not found for queued message",
                progress_pct=100.0,
                trace_id=f"sync:{item.dedupe_key}",
            )
            return 1
        if route.is_deactive():
            self.sync_ledger.mark_status(item.dedupe_key, status="failed", last_error="route_deactive")
            self.message_monitor.note_status(
                dedupe_key=item.dedupe_key,
                status="queued",
                error="route_deactive",
                details="Route is deactive; will resume after route start",
                progress_pct=84.0,
                trace_id=f"sync:{item.dedupe_key}",
            )
            return 1

        checkpoint = int(self.sync_ledger.get_route_checkpoint(route.name) or 0)
        # Retry-wait/failed records are intentionally retryable even if the route
        # checkpoint moved ahead, so transient AI/script outages can self-heal.
        retrying_statuses = {"failed", "retry_wait"}
        is_retrying_failed = str(record.status or "").strip().lower() in retrying_statuses
        if int(record.message_id) <= checkpoint and not is_retrying_failed:
            self.sync_ledger.mark_status(item.dedupe_key, status="blocked", last_error="older_than_checkpoint")
            self.message_monitor.note_status(
                dedupe_key=item.dedupe_key,
                status="blocked",
                error="older_than_checkpoint",
                details="Message older than route checkpoint",
                progress_pct=100.0,
                trace_id=f"sync:{item.dedupe_key}",
            )
            return 1

        # Enforce monotonic per-route processing order so higher IDs cannot advance checkpoint
        # ahead of older pending records.
        min_pending = self.sync_ledger.min_active_message_id_for_route(route.name, above_checkpoint=checkpoint)
        if min_pending is not None and int(record.message_id) > int(min_pending):
            # Self-heal queue ordering starvation: push the real lowest pending record
            # to the front so higher IDs do not spin forever in ordering_wait.
            min_key = self.sync_ledger.first_active_key_for_route_message_id(route.name, int(min_pending))
            if min_key and min_key != item.dedupe_key:
                min_record = self.sync_ledger.get_record(min_key)
                min_payload_hash = payload_hash_from_json(min_record.payload) if min_record is not None else ""
                await self.sync_queue.enqueue(
                    dedupe_key=min_key,
                    route_name=route.name,
                    due_at=0.0,
                    payload_hash=min_payload_hash,
                )
            self.message_monitor.note_stage(
                dedupe_key=item.dedupe_key,
                stage="ordering_wait",
                status="queued",
                progress_pct=10.0,
                details=f"Waiting for lower message_id={int(min_pending)} to finish",
                trace_id=f"sync:{item.dedupe_key}",
            )
            await self.sync_queue.schedule_retry(
                dedupe_key=item.dedupe_key,
                route_name=route.name,
                due_at=time.time() + 0.25,
                payload_hash=item.payload_hash,
            )
            return 0

        if route.is_syncing():
            next_due_raw = self._sync_next_due_at.get(route.name)
            if next_due_raw is not None:
                now = time.time()
                next_due = float(next_due_raw)
                if now < next_due:
                    wait_sec = max(0.0, next_due - now)
                    self.message_monitor.note_stage(
                        dedupe_key=item.dedupe_key,
                        stage="interval_wait",
                        status="queued",
                        progress_pct=10.0,
                        details=f"Sync interval gate; retry in {round(wait_sec, 2)}s",
                        trace_id=f"sync:{item.dedupe_key}",
                    )
                    await self.sync_queue.schedule_retry(
                        dedupe_key=item.dedupe_key,
                        route_name=route.name,
                        due_at=next_due,
                        payload_hash=item.payload_hash,
                    )
                    return 0

        lock_owner = str(uuid4())
        acquired = await self.sync_queue.acquire_route_lock(
            route_name=route.name,
            owner=lock_owner,
            ttl_sec=int(self.settings.sync_lock_ttl_sec),
        )
        if not acquired:
            self.message_monitor.note_stage(
                dedupe_key=item.dedupe_key,
                stage="route_lock_wait",
                status="queued",
                progress_pct=10.0,
                details="Route lock is busy; retry scheduled",
                trace_id=f"sync:{item.dedupe_key}",
            )
            await self.sync_queue.schedule_retry(
                dedupe_key=item.dedupe_key,
                route_name=route.name,
                due_at=time.time() + 0.7,
                payload_hash=item.payload_hash,
            )
            return 0

        try:
            self.sync_ledger.mark_processing(item.dedupe_key, trace_id=f"sync:{item.dedupe_key}")
            attempts_now = int((self.sync_ledger.get_record(item.dedupe_key) or record).attempt_count)
            self.message_monitor.note_stage(
                dedupe_key=item.dedupe_key,
                stage="processing",
                status="processing",
                progress_pct=18.0,
                details="Processing started",
                attempt_count=attempts_now,
                trace_id=f"sync:{item.dedupe_key}",
            )
            incoming = self._incoming_from_payload(record.payload)
            status, last_error = await self._process_route_message_with_retries(
                incoming,
                route,
                retries=route.sync_retry_attempts,
                dedupe_key=item.dedupe_key,
            )

            if status == "ok":
                self.sync_ledger.mark_status(item.dedupe_key, status="sent", trace_id=f"sync:{item.dedupe_key}")
                self.message_monitor.note_status(
                    dedupe_key=item.dedupe_key,
                    status="sent",
                    details="Message sent successfully",
                    progress_pct=100.0,
                    trace_id=f"sync:{item.dedupe_key}",
                )
                self.sync_ledger.set_route_checkpoint(
                    route.name,
                    self._checkpoint_target_with_pending_groups(route.name, int(incoming.message_id)),
                )
                if route.is_syncing():
                    self._sync_next_due_at[route.name] = time.time() + max(1, int(route.sync_interval_sec))
                return 1

            if status in {"blocked", "skipped"}:
                final_status = "blocked" if status == "skipped" else status
                self.sync_ledger.mark_status(
                    item.dedupe_key,
                    status=final_status,
                    last_error=last_error,
                    trace_id=f"sync:{item.dedupe_key}",
                )
                self.message_monitor.note_status(
                    dedupe_key=item.dedupe_key,
                    status=final_status,
                    error=last_error,
                    details="Message blocked/skipped",
                    progress_pct=100.0,
                    trace_id=f"sync:{item.dedupe_key}",
                )
                self.sync_ledger.set_route_checkpoint(
                    route.name,
                    self._checkpoint_target_with_pending_groups(route.name, int(incoming.message_id)),
                )
                return 1

            attempts = int((self.sync_ledger.get_record(item.dedupe_key) or record).attempt_count)
            if self._is_ai_generation_required_error_text(last_error) and attempts >= self._ai_required_max_attempts:
                # Keep retries bounded for transient AI outages to avoid endless loops.
                self.sync_ledger.mark_status(
                    item.dedupe_key,
                    status="blocked",
                    last_error=last_error,
                    trace_id=f"sync:{item.dedupe_key}",
                )
                self.message_monitor.note_status(
                    dedupe_key=item.dedupe_key,
                    status="blocked",
                    error=last_error,
                    details=(
                        "AI generation failed repeatedly; blocked after "
                        f"{attempts} attempt(s)"
                    ),
                    progress_pct=100.0,
                    trace_id=f"sync:{item.dedupe_key}",
                )
                self.sync_ledger.set_route_checkpoint(
                    route.name,
                    self._checkpoint_target_with_pending_groups(route.name, int(incoming.message_id)),
                )
                return 1
            self.sync_ledger.mark_status(
                item.dedupe_key,
                status="retry_wait",
                last_error=last_error,
                trace_id=f"sync:{item.dedupe_key}",
            )
            delay = self._retry_delay(attempts)
            self.message_monitor.note_status(
                dedupe_key=item.dedupe_key,
                status="queued",
                error=last_error,
                details=f"Retry scheduled in {round(delay, 2)}s",
                progress_pct=84.0,
                trace_id=f"sync:{item.dedupe_key}",
            )
            await self.sync_queue.schedule_retry(
                dedupe_key=item.dedupe_key,
                route_name=route.name,
                due_at=time.time() + delay,
                payload_hash=item.payload_hash,
            )
            return 1
        finally:
            await self.sync_queue.release_route_lock(route_name=route.name, owner=lock_owner)

    def _retry_delay(self, attempt_count: int) -> float:
        base = float(self.settings.sync_retry_base_sec)
        exp = max(0, int(attempt_count) - 1)
        return min(base * (2 ** exp), 300.0)

    def _media_cache_key(self, *, media: IncomingMedia, file_path: str) -> str:
        source = str(media.source or "bot_api").strip().lower()
        file_id = str(media.file_id or "").strip()
        file_name = str(media.file_name or "").strip().lower()
        mime = str(media.mime_type or "").strip().lower()
        size = int(media.file_size or 0)
        duration = int(media.duration or 0)
        source_ref = media.source_ref if isinstance(media.source_ref, dict) else {}
        source_ref_key = json.dumps(source_ref, ensure_ascii=False, sort_keys=True) if source_ref else ""
        material = "|".join(
            [
                source,
                media.kind.value,
                file_id,
                str(file_path or "").strip().lower(),
                file_name,
                mime,
                str(size),
                str(duration),
                source_ref_key,
            ]
        )
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    @staticmethod
    def _default_media_extension(kind: MediaKind) -> str:
        return {
            MediaKind.PHOTO: ".jpg",
            MediaKind.VIDEO: ".mp4",
            MediaKind.VOICE: ".ogg",
            MediaKind.AUDIO: ".mp3",
            MediaKind.DOCUMENT: ".bin",
            MediaKind.ANIMATION: ".mp4",
            MediaKind.STICKER: ".webp",
            MediaKind.VIDEO_NOTE: ".mp4",
        }.get(kind, ".bin")

    def _media_cache_path(self, *, cache_key: str, preferred_name: str, media_kind: MediaKind) -> Path:
        suffix = str(Path(preferred_name).suffix or "").strip().lower()
        if not suffix:
            suffix = self._default_media_extension(media_kind)
        subdir = self._media_cache_dir / media_kind.value / cache_key[:2]
        return subdir / f"{cache_key}{suffix}"

    @staticmethod
    def _materialize_local_file(*, source_path: Path, target_path: Path) -> None:
        if target_path.exists():
            target_path.unlink()
        target_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(source_path, target_path)
            return
        except Exception:
            pass
        shutil.copy2(source_path, target_path)

    def _restore_media_from_cache(self, *, cache_path: Path, target_path: Path, remaining: int) -> int | None:
        if not self._media_cache_enabled:
            return None
        if not cache_path.exists() or not cache_path.is_file():
            return None
        try:
            st = cache_path.stat()
        except Exception:
            return None
        now = time.time()
        age_sec = now - float(st.st_mtime)
        if age_sec < 0:
            age_sec = 0
        if age_sec > float(self._media_cache_ttl_sec):
            try:
                cache_path.unlink(missing_ok=True)
            except Exception:
                pass
            return None
        size = int(st.st_size)
        if size <= 0:
            return None
        if size > remaining:
            raise MessageTooLargeError(f"Message exceeded size limit ({size} > {remaining} bytes)")
        self._materialize_local_file(source_path=cache_path, target_path=target_path)
        try:
            os.utime(cache_path, None)
        except Exception:
            pass
        return size

    def _save_media_to_cache(self, *, cache_path: Path, source_path: Path) -> None:
        if not self._media_cache_enabled:
            return
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            if cache_path.exists():
                try:
                    os.utime(cache_path, None)
                except Exception:
                    pass
                return
            tmp_path = cache_path.with_name(f"{cache_path.name}.tmp-{time.time_ns()}")
            self._materialize_local_file(source_path=source_path, target_path=tmp_path)
            tmp_path.replace(cache_path)
        except Exception:
            logger.debug("Media cache store skipped", exc_info=True)
        finally:
            try:
                if 'tmp_path' in locals():
                    tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    def _prune_media_cache(self) -> None:
        if not self._media_cache_enabled:
            return
        now_mono = time.monotonic()
        if now_mono - self._media_cache_last_prune_at < float(self._media_cache_prune_interval_sec):
            return
        self._media_cache_last_prune_at = now_mono
        ttl = float(self._media_cache_ttl_sec)
        now_ts = time.time()
        try:
            files = list(self._media_cache_dir.rglob("*"))
        except Exception:
            return
        removed = 0
        for path in files:
            if not path.is_file():
                continue
            try:
                st = path.stat()
            except Exception:
                continue
            if now_ts - float(st.st_mtime) <= ttl:
                continue
            try:
                path.unlink(missing_ok=True)
                removed += 1
            except Exception:
                continue
        for path in sorted(files, reverse=True):
            if not path.is_dir():
                continue
            try:
                if path != self._media_cache_dir:
                    path.rmdir()
            except Exception:
                continue
        if removed > 0:
            logger.info(
                "Media cache prune completed",
                extra={
                    "details": {
                        "removed_files": removed,
                        "cache_dir": str(self._media_cache_dir),
                        "ttl_sec": int(ttl),
                    }
                },
            )

    async def _download_input_media_with_retries(
        self,
        *,
        media: IncomingMedia,
        file_path: str,
        target_path: Path,
        remaining: int,
        timeout_sec: float,
        route: ChannelRoute,
        incoming: IncomingChannelMessage,
        trace_id: str,
        media_index: int,
    ) -> int:
        is_telethon_media = media.source == "telethon" and isinstance(media.source_ref, dict)
        attempts = 3 if is_telethon_media else 2
        last_exc: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                if target_path.exists():
                    target_path.unlink()

                if is_telethon_media:
                    if self.source_client is None:
                        raise RuntimeError("Telethon source client is not available for media download")
                    downloaded = await asyncio.wait_for(
                        self.source_client.download_media(media.source_ref, target_path, max_bytes=remaining),
                        timeout=timeout_sec,
                    )
                else:
                    downloaded = await asyncio.wait_for(
                        self.telegram_client.download_file(file_path, target_path, max_bytes=remaining),
                        timeout=timeout_sec,
                    )
                return int(downloaded)
            except (MessageTooLargeError, FileNotFoundError, PermissionError, ValueError):
                raise
            except Exception as exc:
                last_exc = exc
                is_timeout = isinstance(exc, (TimeoutError, asyncio.TimeoutError))
                if is_telethon_media and is_timeout and self.source_client is not None:
                    try:
                        await self.source_client.aclose()
                    except Exception:
                        logger.debug(
                            "Failed to reset Telethon source client after media download timeout",
                            extra={
                                "details": {
                                    "route": route.name,
                                    "trace_id": trace_id,
                                    "message_id": incoming.message_id,
                                    "media_index": media_index,
                                }
                            },
                        )
                if attempt >= attempts:
                    break
                logger.warning(
                    "Media download attempt failed; retrying",
                    extra={
                        "details": {
                            "route": route.name,
                            "trace_id": trace_id,
                            "message_id": incoming.message_id,
                            "media_index": media_index,
                            "media_kind": media.kind.value,
                            "source": media.source or "bot_api",
                            "attempt": attempt,
                            "attempts_total": attempts,
                            "timeout_sec": timeout_sec,
                            "error": str(exc),
                        }
                    },
                )
                await asyncio.sleep(min(2.0, 0.4 * attempt))

        if isinstance(last_exc, (TimeoutError, asyncio.TimeoutError)):
            raise TimeoutError(
                f"download_timeout:source={media.source or 'bot_api'}:media_index={media_index}:attempts={attempts}"
            ) from last_exc
        if last_exc is not None:
            raise RuntimeError(
                f"download_failed:source={media.source or 'bot_api'}:media_index={media_index}:error={last_exc}"
            ) from last_exc
        raise RuntimeError(
            f"download_failed:source={media.source or 'bot_api'}:media_index={media_index}:error=unknown"
        )

    def _checkpoint_target_with_pending_groups(self, route_name: str, message_id: int) -> int:
        target = max(0, int(message_id))
        min_pending = self._min_pending_group_message_id(route_name)
        if min_pending is None:
            return target
        # Keep checkpoint behind unflushed media-group members so they are not
        # later finalized early by checkpoint-order handling.
        return min(target, max(0, int(min_pending) - 1))

    def _min_pending_group_message_id(self, route_name: str) -> int | None:
        min_seen: int | None = None
        for (pending_route_name, _source_channel_id, _group_id), bucket in self._pending_media_groups.items():
            if pending_route_name != route_name:
                continue
            messages_by_id = bucket.get("messages_by_id") if isinstance(bucket, dict) else None
            if not isinstance(messages_by_id, dict) or not messages_by_id:
                continue
            try:
                candidate = min(int(k) for k in messages_by_id.keys())
            except Exception:
                continue
            if min_seen is None or candidate < min_seen:
                min_seen = candidate
        return min_seen

    async def _process_route_message_with_retries(
        self,
        incoming: IncomingChannelMessage,
        route: ChannelRoute,
        *,
        retries: int,
        dedupe_key: str | None = None,
    ) -> tuple[str, str | None]:
        attempts = max(0, int(retries)) + 1
        last_status = "failed"
        last_error: str | None = None
        for idx in range(attempts):
            if dedupe_key:
                self.message_monitor.note_stage(
                    dedupe_key=dedupe_key,
                    stage="attempt",
                    status="processing",
                    progress_pct=20.0,
                    attempt_count=idx + 1,
                    details=f"Attempt {idx + 1}/{attempts} started",
                )
            logger.info(
                "Sync retry attempt started",
                extra={
                    "details": {
                        "route": route.name,
                        "update_id": incoming.update_id,
                        "message_id": incoming.message_id,
                        "attempt": idx + 1,
                        "attempts_total": attempts,
                    }
                },
            )
            status, err = await self._process_route_message_detailed(incoming, route, dedupe_key=dedupe_key)
            last_status = status
            last_error = err
            if dedupe_key:
                self.message_monitor.note_stage(
                    dedupe_key=dedupe_key,
                    stage="attempt_result",
                    status=status,
                    progress_pct=88.0 if status == "ok" else 62.0,
                    attempt_count=idx + 1,
                    details=f"Attempt {idx + 1}/{attempts} finished with status={status}",
                )
            logger.info(
                "Sync retry attempt finished",
                extra={
                    "details": {
                        "route": route.name,
                        "update_id": incoming.update_id,
                        "message_id": incoming.message_id,
                        "attempt": idx + 1,
                        "attempts_total": attempts,
                        "status": status,
                        "error": err,
                    }
                },
            )
            if status in {"ok", "blocked"}:
                return status, err
            if status == "failed" and self._is_permanent_channel_script_error_text(err):
                # Permanent script validation failures should not be retried forever.
                # Treat them as blocked so queue checkpoint can advance.
                return "blocked", err
            if self._is_retryable_channel_script_error_text(err):
                if idx < attempts - 1:
                    await asyncio.sleep(0.8 * (idx + 1))
                    continue
                # Exhausted route-level retries; keep this item retryable by queue policy.
                return "failed", "channel_script_retry_exhausted"
            if status == "ambiguous":
                if idx < attempts - 1:
                    await asyncio.sleep(0.8 * (idx + 1))
                    continue
                return "failed", (err or "ambiguous_result_exhausted")
            if idx < attempts - 1 and self._is_transient_error_text(err):
                await asyncio.sleep(0.8 * (idx + 1))
                continue
            break
        return last_status, last_error

    @staticmethod
    def _serialize_incoming(incoming: IncomingChannelMessage) -> dict[str, Any]:
        return {
            "update_id": int(incoming.update_id),
            "source_channel_id": incoming.source_channel_id,
            "source_channel_username": incoming.source_channel_username,
            "message_id": int(incoming.message_id),
            "date": incoming.date,
            "text": incoming.text,
            "caption": incoming.caption,
            "media_group_id": incoming.media_group_id,
            "raw": incoming.raw if isinstance(incoming.raw, dict) else {},
            "medias": [
                {
                    "kind": media.kind.value,
                    "file_id": media.file_id,
                    "file_size": media.file_size,
                    "file_name": media.file_name,
                    "mime_type": media.mime_type,
                    "duration": media.duration,
                    "source": media.source,
                    "source_ref": media.source_ref if isinstance(media.source_ref, dict) else None,
                }
                for media in incoming.medias
            ],
        }

    def _incoming_from_payload(self, payload: dict[str, Any]) -> IncomingChannelMessage:
        medias: list[IncomingMedia] = []
        for item in payload.get("medias") or []:
            if not isinstance(item, dict):
                continue
            kind_raw = str(item.get("kind") or "").strip().lower()
            try:
                kind = MediaKind(kind_raw)
            except Exception:
                continue
            medias.append(
                IncomingMedia(
                    kind=kind,
                    file_id=str(item.get("file_id") or ""),
                    file_size=int(item["file_size"]) if item.get("file_size") is not None else None,
                    file_name=str(item.get("file_name") or "") or None,
                    mime_type=str(item.get("mime_type") or "") or None,
                    duration=int(item["duration"]) if item.get("duration") is not None else None,
                    source=str(item.get("source") or "") or None,
                    source_ref=item.get("source_ref") if isinstance(item.get("source_ref"), dict) else None,
                )
            )
        return IncomingChannelMessage(
            update_id=int(payload.get("update_id") or 0),
            source_channel_id=str(payload.get("source_channel_id") or ""),
            source_channel_username=str(payload.get("source_channel_username") or "") or None,
            message_id=int(payload.get("message_id") or 0),
            date=int(payload["date"]) if payload.get("date") is not None else None,
            text=str(payload.get("text") or "") or None,
            caption=str(payload.get("caption") or "") or None,
            medias=medias,
            raw=payload.get("raw") if isinstance(payload.get("raw"), dict) else {},
            media_group_id=str(payload.get("media_group_id") or "") or None,
        )

    @staticmethod
    def _is_transient_error_text(error_text: str | None) -> bool:
        text = str(error_text or "").lower()
        if not text:
            return False
        return (
            "network error" in text
            or "connecttimeout" in text
            or "readtimeout" in text
            or "download_timeout" in text
            or "http 500" in text
            or "http 502" in text
            or "http 503" in text
            or "http 504" in text
            or "failed to upload file bytes" in text
        )

    @staticmethod
    def _is_ambiguous_dispatch_error_text(error_text: str | None) -> bool:
        text = str(error_text or "").lower()
        if not text:
            return False
        return (
            "network error" in text
            or "connecttimeout" in text
            or "readtimeout" in text
            or "timeout" in text
        )

    @staticmethod
    def _is_permission_error_text(error_text: str | None) -> bool:
        text = str(error_text or "").lower()
        if not text:
            return False
        return (
            "permission_denied" in text
            or "forbidden" in text
            or "you do not have permission" in text
        )

    @staticmethod
    def _is_request_too_large_error_text(error_text: str | None) -> bool:
        text = str(error_text or "").lower()
        if not text:
            return False
        return (
            "http 413" in text
            or "request entity too large" in text
            or "payload too large" in text
            or "entity too large" in text
        )

    @staticmethod
    def _is_permanent_channel_script_error_text(error_text: str | None) -> bool:
        text = str(error_text or "").lower().strip()
        if not text:
            return False
        return "ai_output_not_acceptable" in text

    @staticmethod
    def _is_ai_generation_required_error_text(error_text: str | None) -> bool:
        text = str(error_text or "").lower().strip()
        if not text:
            return False
        return "ai_generation_required_failed" in text

    @staticmethod
    def _is_retryable_channel_script_error_text(error_text: str | None) -> bool:
        text = str(error_text or "").lower().strip()
        if not text:
            return False
        return "channel_script_timeout" in text

    @staticmethod
    def _classify_local_processing_error(stage_name: str, exc: Exception) -> tuple[str, str] | None:
        stage = str(stage_name or "").strip().lower()
        text = str(exc or "").strip()
        lowered = text.lower()

        if stage == "download":
            if isinstance(exc, PermissionError):
                return "failed", "storage_permission_denied"
            if isinstance(exc, FileNotFoundError):
                return "blocked", "source_media_unavailable"
            if isinstance(exc, TimeoutError) or isinstance(exc, asyncio.TimeoutError):
                return "failed", "download_timeout"
            if isinstance(exc, ValueError) and "source_ref" in lowered:
                return "blocked", "invalid_source_reference"
            if "empty file_path" in lowered:
                return "blocked", "source_file_path_missing"

        return None

    async def _notify_review_alert(self, *, route: ChannelRoute, review_id: int, dedupe_key: str, error: str | None) -> None:
        target = self.settings.sync_review_alert_target or self.settings.log_channel_target
        if not target:
            return
        preview = str(error or "").strip()
        if len(preview) > 700:
            preview = preview[:697] + "..."
        text = (
            "SYNC review required\n"
            f"route: {route.name}\n"
            f"review_id: {review_id}\n"
            f"dedupe_key: {dedupe_key}\n"
            f"error: {preview or '-'}"
        )
        try:
            await self.telegram_client.send_message(target, text)
        except Exception:
            logger.exception(
                "Failed to send sync review alert",
                extra={"details": {"route": route.name, "review_id": review_id, "target": target}},
            )

    async def _process_admin_message(self, incoming: AdminInboundMessage) -> None:
        if incoming.callback_query_id:
            try:
                await self.telegram_client.answer_callback_query(incoming.callback_query_id)
            except Exception:
                logger.exception(
                    "Failed to answer admin callback query",
                    extra={
                        "details": {
                            "chat_id": incoming.chat_id,
                            "user_id": incoming.user_id,
                            "callback_query_id": incoming.callback_query_id,
                        }
                    },
                )
        try:
            response = self.admin_handler.handle(incoming)
        except Exception as exc:
            response = f"خطا در مدیریت: {exc}"
        if isinstance(response, str):
            text = response
            reply_markup = None
            delete_message_id = None
        else:
            text = str(getattr(response, "text", "") or "")
            reply_markup = getattr(response, "reply_markup", None)
            delete_message_id = getattr(response, "delete_message_id", None)
            if not text:
                text = "پاسخ خالی از مدیریت دریافت شد."
        chunks = self._split_admin_response_text(text)
        try:
            for idx, chunk in enumerate(chunks):
                markup = reply_markup if idx == 0 else None
                await self.telegram_client.send_message(incoming.chat_id, chunk, reply_markup=markup)
        except Exception:
            logger.exception(
                "Failed to send admin bot response",
                extra={
                    "details": {
                        "chat_id": incoming.chat_id,
                        "user_id": incoming.user_id,
                    }
                },
            )
            return

        if isinstance(delete_message_id, int) and delete_message_id > 0:
            try:
                await self.telegram_client.delete_message(incoming.chat_id, delete_message_id)
            except Exception:
                logger.exception(
                    "Failed to delete admin message",
                    extra={
                        "details": {
                            "chat_id": incoming.chat_id,
                            "user_id": incoming.user_id,
                            "message_id": delete_message_id,
                        }
                    },
                )

    @staticmethod
    def _split_admin_response_text(text: str, *, max_chars: int = 3500) -> list[str]:
        raw = str(text or "").strip()
        if not raw:
            return ["پاسخ خالی"]
        if len(raw) <= max_chars:
            return [raw]

        chunks: list[str] = []
        remaining = raw
        while len(remaining) > max_chars:
            cut = remaining.rfind("\n", 0, max_chars + 1)
            if cut <= 0:
                cut = remaining.rfind(" ", 0, max_chars + 1)
            if cut <= 0:
                cut = max_chars
            part = remaining[:cut].strip()
            if not part:
                part = remaining[:max_chars].strip()
                cut = max_chars
            chunks.append(part)
            remaining = remaining[cut:].lstrip()
        if remaining:
            chunks.append(remaining)
        return chunks

    async def _process_route_message(self, incoming: IncomingChannelMessage, route: ChannelRoute) -> str:
        status, _ = await self._process_route_message_detailed(incoming, route)
        return status

    async def _process_route_message_detailed(
        self,
        incoming: IncomingChannelMessage,
        route: ChannelRoute,
        *,
        dedupe_key: str | None = None,
    ) -> tuple[str, str | None]:
        expanded_incoming = await self._maybe_expand_telethon_media_group(incoming=incoming, route=route)
        if expanded_incoming is not incoming:
            # Keep caller-held incoming in sync with expanded album bounds so
            # route checkpoint advances to the full album range.
            self._sync_incoming_message(incoming, expanded_incoming)
        incoming = expanded_incoming
        trace_id = self._build_trace_id(route, incoming)
        started_at = time.monotonic()
        stage_timings_ms: dict[str, float] = {}
        stage_name = "download"

        def _monitor_stage(
            *,
            stage: str,
            status: str,
            progress_pct: float,
            details: str,
            download_bytes: int | None = None,
            upload_bytes: int | None = None,
        ) -> None:
            if not dedupe_key:
                return
            self.message_monitor.note_stage(
                dedupe_key=dedupe_key,
                stage=stage,
                status=status,
                progress_pct=progress_pct,
                details=details,
                timings_ms=stage_timings_ms,
                download_bytes=download_bytes,
                upload_bytes=upload_bytes,
                trace_id=trace_id,
            )

        logger.info(
            "Route processing started",
            extra={
                "details": {
                    "trace_id": trace_id,
                    "route": route.name,
                    "update_id": incoming.update_id,
                    "message_id": incoming.message_id,
                    "media_group_id": incoming.media_group_id,
                    "source_channel_id": incoming.source_channel_id,
                    "source_channel_username": incoming.source_channel_username,
                    "media_count": len(incoming.medias),
                    "channel_script": route.channel_script,
                    "gaurd_script": route.gaurd_script,
                }
            },
        )
        _monitor_stage(
            stage="processing_start",
            status="processing",
            progress_pct=20.0,
            details="Route processing started",
        )

        permission_block_reason = await self._check_dispatch_permission(route)
        if permission_block_reason is not None:
            stage_timings_ms["total"] = round((time.monotonic() - started_at) * 1000.0, 2)
            _monitor_stage(
                stage="permission_check",
                status="blocked",
                progress_pct=22.0,
                details=f"Destination permission denied: {permission_block_reason}",
            )
            await self._audit_log(
                stage="dispatch",
                status="blocked",
                incoming=incoming,
                route=route,
                reason=permission_block_reason,
                trace_id=trace_id,
                stage_timings_ms=stage_timings_ms,
            )
            logger.info(
                "Route blocked before processing due to destination access",
                extra={
                    "details": {
                        "route": route.name,
                        "trace_id": trace_id,
                        "destination_target": route.destination_target(),
                        "reason": permission_block_reason,
                    }
                },
            )
            return "blocked", permission_block_reason

        try:
            paths = self.storage.prepare_message_paths(incoming)
            self.storage.write_raw_update(paths, incoming.raw)
        except Exception as exc:
            classified = self._classify_local_processing_error(stage_name, exc)
            if classified is not None:
                status, error_code = classified
                stage_timings_ms["total"] = round((time.monotonic() - started_at) * 1000.0, 2)
                await self._audit_log(
                    stage=stage_name,
                    status=status,
                    incoming=incoming,
                    route=route,
                    reason=f"Storage preparation error at stage {stage_name}: {exc}",
                    trace_id=trace_id,
                    stage_timings_ms=stage_timings_ms,
                )
                logger.info(
                    "Route processing failed during storage preparation",
                    extra={
                        "details": {
                            "route": route.name,
                            "trace_id": trace_id,
                            "stage": stage_name,
                            "status": status,
                            "error_code": error_code,
                            "error": str(exc),
                            "timings_ms": stage_timings_ms,
                        }
                    },
                )
                _monitor_stage(
                    stage=stage_name,
                    status=status,
                    progress_pct=24.0,
                    details=f"Storage preparation issue: {error_code}",
                )
                return status, error_code
            stage_timings_ms["total"] = round((time.monotonic() - started_at) * 1000.0, 2)
            await self._audit_log(
                stage="processing",
                status="failed",
                incoming=incoming,
                route=route,
                reason="Unexpected error during storage preparation",
                trace_id=trace_id,
                stage_timings_ms=stage_timings_ms,
            )
            logger.exception(
                "Unexpected error during storage preparation",
                extra={
                    "details": {
                        "route": route.name,
                        "trace_id": trace_id,
                        "source_channel_id": incoming.source_channel_id,
                        "update_id": incoming.update_id,
                        "timings_ms": stage_timings_ms,
                    }
                },
            )
            _monitor_stage(
                stage="storage_prepare",
                status="failed",
                progress_pct=24.0,
                details="Unexpected storage preparation error",
            )
            return "failed", "storage_prepare_unexpected_error"

        input_dir = Path(paths.input_dir)
        output_dir = Path(paths.output_dir)

        max_mb = route.max_message_mb or self.settings.default_max_message_mb
        max_total_bytes = max_mb * 1024 * 1024

        payload: dict = {
            "route": {
                "name": route.name,
                "status": route.status,
                "source_channel_username": route.source_channel_username,
                "destination_channel_id": route.destination_channel_id,
                "destination_channel_username": route.destination_channel_username,
                "destination_target": route.destination_target(),
                "channel_script": route.channel_script,
                "backfill_count": route.sync_backfill_count,
                "interval_sec": route.sync_interval_sec,
                "batch_size": route.sync_batch_size,
                "retry_attempts": route.sync_retry_attempts,
            },
            "message": {
                "update_id": incoming.update_id,
                "message_id": incoming.message_id,
                "date": incoming.date,
                "source_channel_id": incoming.source_channel_id,
                "source_channel_username": incoming.source_channel_username,
                "text": incoming.text,
                "caption": incoming.caption,
            },
            "inputs": [],
        }

        downloaded_total = 0
        network_downloaded_total = 0
        used_local_names: set[str] = set()
        download_started = time.monotonic()
        try:
            self._prune_media_cache()
            for idx, media in enumerate(incoming.medias, start=1):
                if media.kind == MediaKind.STICKER:
                    logger.info(
                        "Sticker input blocked by policy",
                        extra={
                            "details": {
                                "route": route.name,
                                "trace_id": trace_id,
                                "source_channel_id": incoming.source_channel_id,
                                "update_id": incoming.update_id,
                                "file_id": media.file_id,
                            }
                        },
                    )
                    continue

                file_path = ""
                if media.source == "telethon" and isinstance(media.source_ref, dict):
                    file_path = media.file_name or f"{media.kind.value}_{idx}"
                else:
                    file_info = await self.telegram_client.get_file(media.file_id)
                    file_path = str(file_info.get("file_path") or "").strip()
                    if not file_path:
                        raise RuntimeError(f"Telegram get_file returned empty file_path for {media.file_id}")

                preferred_name = self._preferred_local_name(media, file_path=file_path, idx=idx)
                target_rel = self._allocate_local_name(preferred_name, idx=idx, used_names=used_local_names)
                target_path = input_dir / target_rel
                target_path.parent.mkdir(parents=True, exist_ok=True)

                remaining = max_total_bytes - downloaded_total
                if remaining <= 0:
                    raise MessageTooLargeError(f"Message exceeded size limit ({max_mb} MB)")
                cache_key = self._media_cache_key(media=media, file_path=file_path)
                cache_path = self._media_cache_path(
                    cache_key=cache_key,
                    preferred_name=str(target_rel),
                    media_kind=media.kind,
                )

                timeout_raw = os.getenv("SYNC_MEDIA_DOWNLOAD_TIMEOUT_SEC", "").strip()
                if timeout_raw:
                    try:
                        base_timeout_sec = max(30.0, min(300.0, float(timeout_raw)))
                    except Exception:
                        base_timeout_sec = max(30.0, float(self.settings.script_timeout_sec))
                else:
                    base_timeout_sec = max(30.0, float(self.settings.script_timeout_sec))

                heavy_timeout_raw = os.getenv("SYNC_MEDIA_DOWNLOAD_TIMEOUT_HEAVY_SEC", "").strip()
                if heavy_timeout_raw:
                    try:
                        heavy_timeout_sec = max(base_timeout_sec, min(600.0, float(heavy_timeout_raw)))
                    except Exception:
                        heavy_timeout_sec = max(base_timeout_sec, 180.0)
                else:
                    heavy_timeout_sec = max(base_timeout_sec, 180.0)

                if media.kind in {
                    MediaKind.VIDEO,
                    MediaKind.DOCUMENT,
                    MediaKind.ANIMATION,
                    MediaKind.AUDIO,
                    MediaKind.VOICE,
                    MediaKind.VIDEO_NOTE,
                }:
                    timeout_sec = heavy_timeout_sec
                else:
                    timeout_sec = base_timeout_sec

                cache_hit = False
                downloaded = self._restore_media_from_cache(
                    cache_path=cache_path,
                    target_path=target_path,
                    remaining=remaining,
                )
                if downloaded is not None:
                    cache_hit = True
                    logger.info(
                        "Media reused from local cache",
                        extra={
                            "details": {
                                "trace_id": trace_id,
                                "route": route.name,
                                "update_id": incoming.update_id,
                                "message_id": incoming.message_id,
                                "media_index": idx,
                                "kind": media.kind.value,
                                "bytes": int(downloaded),
                                "local_path": str(target_path),
                                "cache_path": str(cache_path),
                            }
                        },
                    )
                else:
                    downloaded = await self._download_input_media_with_retries(
                        media=media,
                        file_path=file_path,
                        target_path=target_path,
                        remaining=remaining,
                        timeout_sec=timeout_sec,
                        route=route,
                        incoming=incoming,
                        trace_id=trace_id,
                        media_index=idx,
                    )
                    network_downloaded_total += int(downloaded)
                    self._save_media_to_cache(cache_path=cache_path, source_path=target_path)
                downloaded_total += downloaded
                if not cache_hit:
                    logger.info(
                        "Media downloaded",
                        extra={
                            "details": {
                                "trace_id": trace_id,
                                "route": route.name,
                                "update_id": incoming.update_id,
                                "message_id": incoming.message_id,
                                "media_index": idx,
                                "kind": media.kind.value,
                                "source": media.source or "bot_api",
                                "bytes": downloaded,
                                "local_path": str(target_path),
                                "downloaded_total_bytes": downloaded_total,
                                "max_total_bytes": max_total_bytes,
                            }
                        },
                    )

                payload["inputs"].append(
                    {
                        "kind": media.kind.value,
                        "file_id": media.file_id,
                        "file_name": media.file_name,
                        "mime_type": media.mime_type,
                        "duration": media.duration,
                        "size_bytes": downloaded,
                        "local_path": str(target_path),
                        "local_name": str(target_rel),
                        "cache_hit": cache_hit,
                    }
                )

            payload["downloaded_total_bytes"] = downloaded_total
            payload["network_downloaded_total_bytes"] = network_downloaded_total
            payload["max_total_bytes"] = max_total_bytes
            self.storage.write_payload(paths, payload)
            self._record_traffic(route_name=route.name, download_bytes=network_downloaded_total, upload_bytes=0)
            stage_timings_ms["download"] = round((time.monotonic() - download_started) * 1000.0, 2)
            _monitor_stage(
                stage="download",
                status="processing",
                progress_pct=40.0,
                details=f"Prepared {downloaded_total} bytes (network {network_downloaded_total} bytes)",
                download_bytes=network_downloaded_total,
            )
            logger.info(
                "Inputs prepared for scripts",
                extra={
                    "details": {
                        "trace_id": trace_id,
                        "route": route.name,
                        "payload_path": paths.payload_path,
                        "input_dir": paths.input_dir,
                        "output_dir": paths.output_dir,
                        "inputs_count": len(payload["inputs"]),
                        "downloaded_total_bytes": downloaded_total,
                        "network_downloaded_total_bytes": network_downloaded_total,
                        "download_ms": stage_timings_ms["download"],
                    }
                },
            )

            guard_started = time.monotonic()
            stage_name = "guard"
            is_allowed = True
            if route.gaurd_script:
                is_allowed = await self.guard_runner.run(
                    route,
                    payload_path=Path(paths.payload_path),
                    input_dir=input_dir,
                    output_dir=output_dir,
                    trace_id=trace_id,
                )
            stage_timings_ms["guard"] = round((time.monotonic() - guard_started) * 1000.0, 2)
            if not is_allowed:
                block_reason = self.guard_runner.last_reason or f"guard_denied:{route.gaurd_script}"
                await self._audit_log(
                    stage="guard",
                    status="blocked",
                    incoming=incoming,
                    route=route,
                    reason=block_reason,
                    trace_id=trace_id,
                    stage_timings_ms=stage_timings_ms,
                )
                logger.info(
                    "Message blocked by guard script",
                    extra={
                        "details": {
                            "route": route.name,
                            "trace_id": trace_id,
                            "source_channel_id": incoming.source_channel_id,
                            "update_id": incoming.update_id,
                            "gaurd_script": route.gaurd_script,
                            "guard_ms": stage_timings_ms["guard"],
                            "reason": block_reason,
                        }
                    },
                )
                _monitor_stage(
                    stage="guard",
                    status="blocked",
                    progress_pct=56.0,
                    details=block_reason,
                )
                return "blocked", block_reason
            await self._audit_log(
                stage="guard",
                status="ok",
                incoming=incoming,
                route=route,
                reason=f"Guard passed ({route.gaurd_script})" if route.gaurd_script else "No guard configured; passed through",
                trace_id=trace_id,
                stage_timings_ms=stage_timings_ms,
                stage_output={
                    "token": self.guard_runner.last_token,
                    "stdout": self.guard_runner.last_stdout,
                    "stderr": self.guard_runner.last_stderr,
                    "duration_ms": self.guard_runner.last_duration_ms,
                }
                if route.gaurd_script
                else {"not_configured": True},
            )
            _monitor_stage(
                stage="guard",
                status="processing",
                progress_pct=56.0,
                details="Guard stage passed",
            )

            channel_started = time.monotonic()
            stage_name = "channel_script"
            if route.channel_script:
                timeout_override: int | None = None
                raw_timeout = os.getenv("CHANNEL_SCRIPT_TIMEOUT_SEC", "").strip()
                if raw_timeout:
                    try:
                        timeout_override = max(30, min(600, int(raw_timeout)))
                    except Exception:
                        timeout_override = None
                run_result = await self.script_runner.run(
                    route,
                    payload_path=Path(paths.payload_path),
                    input_dir=input_dir,
                    output_dir=output_dir,
                    script_name=route.channel_script,
                    stage_name="channel_script",
                    trace_id=trace_id,
                    timeout_sec_override=timeout_override,
                )
            else:
                run_result = ScriptRunResult(
                    messages=self._build_passthrough_messages(incoming=incoming, payload=payload),
                    stdout="",
                    stderr="",
                )
            stage_timings_ms["channel_script"] = round((time.monotonic() - channel_started) * 1000.0, 2)
            logger.info(
                "Channel script completed",
                extra={
                    "details": {
                        "trace_id": trace_id,
                        "route": route.name,
                        "channel_script": route.channel_script,
                        "output_messages_count": len(run_result.messages),
                        "stage_ms": stage_timings_ms["channel_script"],
                        "passthrough": route.channel_script is None,
                    }
                },
            )
            _monitor_stage(
                stage="channel_script",
                status="processing",
                progress_pct=74.0,
                details=f"Channel script produced {len(run_result.messages)} message(s)",
            )

            if not run_result.messages:
                await self._audit_log(
                    stage="channel_script",
                    status="blocked",
                    incoming=incoming,
                    route=route,
                    reason="Channel script output was empty",
                    trace_id=trace_id,
                    stage_timings_ms=stage_timings_ms,
                )
                logger.info(
                    "Channel script generated no output messages",
                    extra={
                        "details": {
                            "route": route.name,
                            "trace_id": trace_id,
                            "source_channel_id": incoming.source_channel_id,
                            "update_id": incoming.update_id,
                            "channel_script": route.channel_script,
                            "stage_ms": stage_timings_ms["channel_script"],
                        }
                    },
                )
                _monitor_stage(
                    stage="channel_script",
                    status="blocked",
                    progress_pct=74.0,
                    details="Channel script output was empty",
                )
                return "blocked", "channel_script_empty_output"
            await self._audit_log(
                stage="channel_script",
                status="ok",
                incoming=incoming,
                route=route,
                reason=f"Channel script executed ({len(run_result.messages)} output messages)"
                if route.channel_script
                else f"No channel script configured; direct passthrough from input message ({len(run_result.messages)} messages)",
                trace_id=trace_id,
                stage_timings_ms=stage_timings_ms,
                stage_output={
                    "stdout": run_result.stdout,
                    "stderr": run_result.stderr,
                    "messages": [self._script_message_to_dict(msg) for msg in run_result.messages],
                },
            )

            filtered_messages, dropped_output_media = self._filter_oversized_output_messages(
                run_result.messages,
                output_dir=output_dir,
                input_dir=input_dir,
                max_total_bytes=max_total_bytes,
                extra_input_dirs=[],
            )
            if dropped_output_media:
                logger.warning(
                    "Script output media dropped due to size limits",
                    extra={
                        "details": {
                            "trace_id": trace_id,
                            "route": route.name,
                            "dropped_count": len(dropped_output_media),
                            "max_total_bytes": max_total_bytes,
                            "dropped": dropped_output_media,
                        }
                    },
                )
            if not filtered_messages:
                await self._audit_log(
                    stage="channel_script",
                    status="blocked",
                    incoming=incoming,
                    route=route,
                    reason="All channel script outputs were dropped due to size limits",
                    trace_id=trace_id,
                    stage_timings_ms=stage_timings_ms,
                )
                _monitor_stage(
                    stage="channel_script",
                    status="blocked",
                    progress_pct=74.0,
                    details="All outputs dropped by size limit",
                )
                return "blocked", "channel_script_output_size_limit"

            final_result = ScriptRunResult(
                messages=self.keyword_linker.apply(route.destination_target(), filtered_messages),
                stdout=run_result.stdout,
                stderr=run_result.stderr,
            )
            if self._is_obvious_promotional_output(final_result.messages):
                block_reason = "output_promo_blocked"
                await self._audit_log(
                    stage="channel_script",
                    status="blocked",
                    incoming=incoming,
                    route=route,
                    reason=block_reason,
                    trace_id=trace_id,
                    stage_timings_ms=stage_timings_ms,
                )
                logger.info(
                    "Message blocked by output promotional policy",
                    extra={
                        "details": {
                            "trace_id": trace_id,
                            "route": route.name,
                            "reason": block_reason,
                            "output_messages_count": len(final_result.messages),
                        }
                    },
                )
                _monitor_stage(
                    stage="channel_script",
                    status="blocked",
                    progress_pct=74.0,
                    details=block_reason,
                )
                return "blocked", block_reason

            upload_total_bytes = self._estimate_output_messages_bytes(
                final_result.messages,
                output_dir=output_dir,
                input_dir=input_dir,
                extra_input_dirs=[],
            )
            (output_dir / "channel_messages.json").write_text(
                json.dumps(
                    {"messages": [self._script_message_to_dict(msg) for msg in final_result.messages]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            dispatch_started = time.monotonic()
            stage_name = "dispatch"
            _monitor_stage(
                stage="dispatch",
                status="processing",
                progress_pct=86.0,
                details="Dispatching output messages",
            )
            await self.dispatcher.dispatch(
                route.destination_target(),
                final_result.messages,
                output_dir=output_dir,
                input_dir=input_dir,
                extra_input_dirs=[],
            )
            self._record_traffic(route_name=route.name, download_bytes=0, upload_bytes=upload_total_bytes)
            stage_timings_ms["dispatch"] = round((time.monotonic() - dispatch_started) * 1000.0, 2)
            stage_timings_ms["total"] = round((time.monotonic() - started_at) * 1000.0, 2)
            _monitor_stage(
                stage="dispatch",
                status="processing",
                progress_pct=96.0,
                details=f"Dispatched {len(final_result.messages)} message(s)",
                upload_bytes=upload_total_bytes,
            )
            await self._audit_log(
                stage="dispatch",
                status="ok",
                incoming=incoming,
                route=route,
                reason=f"Dispatched ({len(final_result.messages)} output messages)",
                trace_id=trace_id,
                stage_timings_ms=stage_timings_ms,
            )
            logger.info(
                "Route processing completed",
                extra={
                    "details": {
                        "trace_id": trace_id,
                        "route": route.name,
                        "status": "ok",
                        "timings_ms": stage_timings_ms,
                        "output_messages_count": len(final_result.messages),
                    }
                },
            )
            return "ok", None

        except MessageTooLargeError as exc:
            stage_timings_ms["total"] = round((time.monotonic() - started_at) * 1000.0, 2)
            await self._audit_log(
                stage="download",
                status="blocked",
                incoming=incoming,
                route=route,
                reason=f"Message size exceeded allowed limit: {exc}",
                trace_id=trace_id,
                stage_timings_ms=stage_timings_ms,
            )
            logger.warning(
                "Message blocked: size limit exceeded",
                extra={
                    "details": {
                        "route": route.name,
                        "trace_id": trace_id,
                        "source_channel_id": incoming.source_channel_id,
                        "update_id": incoming.update_id,
                        "max_mb": max_mb,
                        "error": str(exc),
                        "timings_ms": stage_timings_ms,
                    }
                },
            )
            _monitor_stage(
                stage="download",
                status="blocked",
                progress_pct=100.0,
                details=f"Message too large: {exc}",
            )
            return "blocked", str(exc)
        except PlatformApiError as exc:
            stage_timings_ms["total"] = round((time.monotonic() - started_at) * 1000.0, 2)
            error_text = str(exc)
            is_permission = stage_name == "dispatch" and self._is_permission_error_text(error_text)
            is_request_too_large = stage_name == "dispatch" and self._is_request_too_large_error_text(error_text)
            if is_permission or is_request_too_large:
                status = "blocked"
            else:
                status = "failed"
            await self._audit_log(
                stage=stage_name,
                status=status,
                incoming=incoming,
                route=route,
                reason=(
                    "Destination permission denied (permission_denied)"
                    if is_permission
                    else (
                        "Destination payload too large (request_too_large)"
                        if is_request_too_large
                        else f"Platform error: {exc}"
                    )
                ),
                trace_id=trace_id,
                stage_timings_ms=stage_timings_ms,
            )
            log_fn = logger.info if status == "blocked" else logger.warning
            log_fn(
                "Route processing platform error",
                extra={
                    "details": {
                        "route": route.name,
                        "trace_id": trace_id,
                        "stage": stage_name,
                        "status": status,
                        "error": str(exc),
                        "timings_ms": stage_timings_ms,
                    }
                },
            )
            _monitor_stage(
                stage=stage_name,
                status=status,
                progress_pct=86.0 if stage_name == "dispatch" else 60.0,
                details=f"Platform error: {exc}",
            )
            return status, str(exc)
        except ScriptExecutionError as exc:
            error_text = "script_execution_error"
            status = "failed"
            if stage_name == "channel_script":
                msg = ""
                try:
                    msg = str(exc)
                except Exception:
                    msg = ""
                lowered = msg.lower()
                if "ai_generation_required_failed" in lowered:
                    status = "failed"
                    error_text = "ai_generation_required_failed"
                elif "ai_output_not_acceptable" in lowered:
                    status = "failed"
                    error_text = "ai_output_not_acceptable"
                elif "timeout after" in lowered:
                    status = "failed"
                    error_text = "channel_script_timeout"
            stage_timings_ms["total"] = round((time.monotonic() - started_at) * 1000.0, 2)
            await self._audit_log(
                stage="processing",
                status=status,
                incoming=incoming,
                route=route,
                reason=(
                    "Channel script required AI generation but no AI output was produced"
                    if error_text == "ai_generation_required_failed"
                    else "Channel script execution failed"
                ),
                trace_id=trace_id,
                stage_timings_ms=stage_timings_ms,
            )
            logger.exception(
                "Script execution failed",
                extra={
                    "details": {
                        "route": route.name,
                        "trace_id": trace_id,
                        "source_channel_id": incoming.source_channel_id,
                        "update_id": incoming.update_id,
                        "timings_ms": stage_timings_ms,
                    }
                },
            )
            _monitor_stage(
                stage="channel_script",
                status=status,
                progress_pct=72.0,
                details=error_text,
            )
            return status, error_text
        except GuardExecutionError:
            stage_timings_ms["total"] = round((time.monotonic() - started_at) * 1000.0, 2)
            await self._audit_log(
                stage="guard",
                status="failed",
                incoming=incoming,
                route=route,
                reason=f"Guard execution failed ({route.gaurd_script})",
                trace_id=trace_id,
                stage_timings_ms=stage_timings_ms,
            )
            logger.exception(
                "Guard script execution failed",
                extra={
                    "details": {
                        "route": route.name,
                        "trace_id": trace_id,
                        "source_channel_id": incoming.source_channel_id,
                        "update_id": incoming.update_id,
                        "gaurd_script": route.gaurd_script,
                        "timings_ms": stage_timings_ms,
                    }
                },
            )
            _monitor_stage(
                stage="guard",
                status="failed",
                progress_pct=56.0,
                details="guard_execution_error",
            )
            return "failed", "guard_execution_error"
        except Exception as exc:
            classified = self._classify_local_processing_error(stage_name, exc)
            if classified is not None:
                status, error_code = classified
                stage_timings_ms["total"] = round((time.monotonic() - started_at) * 1000.0, 2)
                reason = f"Source/input error at stage {stage_name}: {exc}"
                await self._audit_log(
                    stage=stage_name,
                    status=status,
                    incoming=incoming,
                    route=route,
                    reason=reason,
                    trace_id=trace_id,
                    stage_timings_ms=stage_timings_ms,
                )
                logger.info(
                    "Route processing blocked due to source input issue",
                    extra={
                        "details": {
                            "route": route.name,
                            "trace_id": trace_id,
                            "stage": stage_name,
                            "status": status,
                            "error_code": error_code,
                            "error": str(exc),
                            "timings_ms": stage_timings_ms,
                        }
                    },
                )
                _monitor_stage(
                    stage=stage_name,
                    status=status,
                    progress_pct=50.0,
                    details=error_code,
                )
                return status, error_code
            stage_timings_ms["total"] = round((time.monotonic() - started_at) * 1000.0, 2)
            await self._audit_log(
                stage="processing",
                status="failed",
                incoming=incoming,
                route=route,
                reason="Unexpected processing error",
                trace_id=trace_id,
                stage_timings_ms=stage_timings_ms,
            )
            logger.exception(
                "Unexpected error in route processing",
                extra={
                    "details": {
                        "route": route.name,
                        "trace_id": trace_id,
                        "source_channel_id": incoming.source_channel_id,
                        "update_id": incoming.update_id,
                        "timings_ms": stage_timings_ms,
                    }
                },
            )
            _monitor_stage(
                stage=stage_name,
                status="failed",
                progress_pct=52.0,
                details=f"{stage_name}_unexpected_error",
            )
            return "failed", f"{stage_name}_unexpected_error"

    async def _maybe_expand_telethon_media_group(
        self,
        *,
        incoming: IncomingChannelMessage,
        route: ChannelRoute,
    ) -> IncomingChannelMessage:
        if not incoming.media_group_id:
            return incoming
        if len(incoming.medias) > 1:
            return incoming
        if not any(
            str(media.source or "").strip().lower() == "telethon" and isinstance(media.source_ref, dict)
            for media in incoming.medias
        ):
            return incoming
        if self.source_client is None:
            return incoming

        expand_func = getattr(self.source_client, "expand_media_group", None)
        if not callable(expand_func):
            return incoming

        try:
            expanded = await expand_func(incoming, source_username=route.source_channel_username)
        except Exception:
            logger.exception(
                "Failed to expand telethon media group; continuing with original incoming message",
                extra={
                    "details": {
                        "route": route.name,
                        "source_channel_id": incoming.source_channel_id,
                        "message_id": incoming.message_id,
                        "media_group_id": incoming.media_group_id,
                    }
                },
            )
            return incoming

        if not isinstance(expanded, IncomingChannelMessage):
            return incoming
        if len(expanded.medias) <= len(incoming.medias):
            return incoming

        logger.info(
            "Expanded telethon media group before processing",
            extra={
                "details": {
                    "route": route.name,
                    "source_channel_id": incoming.source_channel_id,
                    "media_group_id": incoming.media_group_id,
                    "original_message_id": incoming.message_id,
                    "expanded_message_id": expanded.message_id,
                    "original_media_count": len(incoming.medias),
                    "expanded_media_count": len(expanded.medias),
                }
            },
        )
        return expanded

    def _collect_ready_messages(
        self,
        matched_messages: list[tuple[IncomingChannelMessage, ChannelRoute]],
    ) -> list[tuple[IncomingChannelMessage, ChannelRoute]]:
        ready: list[tuple[IncomingChannelMessage, ChannelRoute]] = []
        now = asyncio.get_running_loop().time()

        for incoming, route in matched_messages:
            if not incoming.media_group_id:
                ready.append((incoming, route))
                continue

            group_key = (route.name, incoming.source_channel_id, incoming.media_group_id)
            bucket = self._pending_media_groups.get(group_key)
            if bucket is None:
                bucket = {
                    "route": route,
                    "messages_by_id": {},
                    "first_seen": now,
                    "last_seen": now,
                }
                self._pending_media_groups[group_key] = bucket

            messages_by_id = bucket.get("messages_by_id")
            if not isinstance(messages_by_id, dict):
                messages_by_id = {}
                bucket["messages_by_id"] = messages_by_id

            existing = messages_by_id.get(int(incoming.message_id))
            if isinstance(existing, IncomingChannelMessage):
                messages_by_id[int(incoming.message_id)] = self._merge_group_member(existing, incoming)
            else:
                messages_by_id[int(incoming.message_id)] = incoming
            bucket["last_seen"] = now

            # Telegram albums are at most 10 media items; once we have 10 unique members,
            # flush immediately because no more media can belong to this group.
            if len(messages_by_id) >= 10:
                group_messages = self._group_bucket_messages(bucket)
                if group_messages:
                    ready.append((self._merge_group_messages(group_messages), route))
                self._pending_media_groups.pop(group_key, None)

        return ready

    def _flush_ready_media_groups(self, *, force: bool) -> list[tuple[IncomingChannelMessage, ChannelRoute]]:
        if not self._pending_media_groups:
            return []

        now = asyncio.get_running_loop().time()
        ready: list[tuple[IncomingChannelMessage, ChannelRoute]] = []
        keys_to_remove: list[tuple[str, str, str]] = []

        for key, bucket in self._pending_media_groups.items():
            last_seen = float(bucket.get("last_seen") or now)
            if not force and (now - last_seen) < self.settings.media_group_wait_sec:
                continue

            route = bucket.get("route")
            group_messages = self._group_bucket_messages(bucket)
            if isinstance(route, ChannelRoute) and group_messages:
                ready.append((self._merge_group_messages(group_messages), route))
            keys_to_remove.append(key)

        for key in keys_to_remove:
            self._pending_media_groups.pop(key, None)
        return ready

    def set_routes(self, routes: RouteRegistry) -> None:
        self.routes = routes

    async def force_sync_route_runtime_reset(self, route_name: str) -> dict[str, int | bool]:
        target = str(route_name or "").strip()
        if not target:
            return {
                "removed_pending_groups": 0,
                "cleared_next_due": False,
                "removed_monitor_messages": 0,
                "removed_monitor_events": 0,
            }

        pending_keys = [key for key in self._pending_media_groups.keys() if str(key[0]) == target]
        for key in pending_keys:
            self._pending_media_groups.pop(key, None)
        had_next_due = target in self._sync_next_due_at
        self._sync_next_due_at.pop(target, None)
        monitor = self.message_monitor.clear_route(target)
        return {
            "removed_pending_groups": int(len(pending_keys)),
            "cleared_next_due": bool(had_next_due),
            "removed_monitor_messages": int(monitor.get("removed_messages") or 0),
            "removed_monitor_events": int(monitor.get("removed_events") or 0),
        }

    async def force_sync_route_backfill(self, route_name: str, *, limit: int | None = None) -> dict[str, int | bool | str]:
        target = str(route_name or "").strip()
        if not target:
            return {"seed_supported": False, "seeded_messages": 0, "enqueued_messages": 0, "processed_now": 0}

        route = self._find_route(target)
        if route is None:
            raise ValueError("route_not_found")
        if route.is_deactive():
            return {"seed_supported": False, "seeded_messages": 0, "enqueued_messages": 0, "processed_now": 0, "note": "route_deactive"}
        if self.source_client is None:
            return {"seed_supported": False, "seeded_messages": 0, "enqueued_messages": 0, "processed_now": 0, "note": "source_client_missing"}

        seed_recent = getattr(self.source_client, "seed_recent_messages", None)
        if not callable(seed_recent):
            return {"seed_supported": False, "seeded_messages": 0, "enqueued_messages": 0, "processed_now": 0, "note": "seed_recent_not_supported"}

        take = max(0, int(route.sync_backfill_count if limit is None else limit))
        seeded: list[IncomingChannelMessage] | None = None
        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                out = await seed_recent(route, take)
                seeded = out if isinstance(out, list) else []
                break
            except Exception as exc:
                last_exc = exc
                if attempt >= 3:
                    break
                logger.warning(
                    "Force sync seed attempt failed; retrying",
                    extra={
                        "details": {
                            "route": target,
                            "attempt": attempt,
                            "attempts_total": 3,
                            "error": str(exc),
                        }
                    },
                )
                await asyncio.sleep(0.4 * attempt)
        if seeded is None:
            raise RuntimeError(f"force_sync_seed_failed:{last_exc}") from last_exc
        if not isinstance(seeded, list):
            seeded = []
        seeded_sorted = sorted(
            [item for item in seeded if isinstance(item, IncomingChannelMessage)],
            key=lambda item: int(item.message_id),
        )
        for incoming in seeded_sorted:
            await self._enqueue_sync_message(route, incoming)
        processed_now = await self._tick_sync_drain() if seeded_sorted else 0
        return {
            "seed_supported": True,
            "seeded_messages": int(len(seeded_sorted)),
            "enqueued_messages": int(len(seeded_sorted)),
            "processed_now": int(processed_now),
        }

    def message_monitor_snapshot(
        self,
        *,
        limit: int = 200,
        route_name: str | None = None,
        status: str | None = None,
        active_only: bool = False,
        search: str | None = None,
    ) -> dict[str, Any]:
        return self.message_monitor.list_messages(
            limit=limit,
            route_name=route_name,
            status=status,
            active_only=active_only,
            search=search,
        )

    def message_monitor_events(self, *, after_seq: int = 0, limit: int = 200) -> dict[str, Any]:
        return self.message_monitor.list_events(after_seq=after_seq, limit=limit)

    def runtime_snapshot(self) -> dict[str, object]:
        monitor = self.message_monitor.list_messages(limit=1)
        monitor_summary = monitor.get("summary") if isinstance(monitor, dict) else {}
        return {
            "started_at_ts": self._started_at_ts,
            "uptime_sec": max(0.0, time.time() - self._started_at_ts),
            "run_once_calls": int(self._run_once_calls),
            "processed_messages_total": int(self._processed_messages_total),
            "last_run_once_ts": self._last_run_once_ts,
            "consecutive_poll_errors": int(self._consecutive_poll_errors),
            "last_poll_error": self._last_poll_error,
            "stop_requested": bool(self._stop_event.is_set()),
            "route_count": len(self.routes.routes),
            "pending_media_groups": len(self._pending_media_groups),
            "retry_queue_hydrated": bool(self._retry_queue_hydrated),
            "sync_drain_task_running": bool(self._sync_drain_task is not None and not self._sync_drain_task.done()),
            "monitor": {
                "messages_tracked": int((monitor_summary or {}).get("total", 0) or 0),
                "latest_seq": int(monitor.get("latest_seq", 0) if isinstance(monitor, dict) else 0),
            },
            "traffic": self.traffic_snapshot(),
        }

    async def sync_runtime_status_snapshot(self) -> dict[str, dict[str, object]]:
        now = time.time()
        out: dict[str, dict[str, object]] = {}
        for route in self.routes.routes:
            base_status = str(route.status or "").strip().lower() or "deactive"
            item: dict[str, object] = {
                "status": base_status,
                "runtime_status": base_status,
                "interval_gate_active": False,
                "wait_remaining_sec": 0.0,
                "next_due_at_ts": None,
            }
            if route.is_syncing():
                item["runtime_status"] = "syncing"
                next_due_raw = self._sync_next_due_at.get(route.name)
                if next_due_raw is not None:
                    next_due = float(next_due_raw)
                    wait_remaining = max(0.0, next_due - now)
                    item["next_due_at_ts"] = next_due
                    item["wait_remaining_sec"] = wait_remaining
                    if wait_remaining > 0.0:
                        item["runtime_status"] = "sync_waiting"
                        item["interval_gate_active"] = True
            out[str(route.name)] = item
        return out

    def traffic_snapshot(self) -> dict[str, object]:
        with self._traffic_lock:
            self._ensure_traffic_day()
            by_route: list[dict[str, object]] = []
            all_route_names = set(self._traffic_by_route_total.keys()) | set(self._traffic_by_route_today.keys())
            for route_name in sorted(all_route_names):
                total_obj = self._traffic_by_route_total.get(route_name) or {}
                today_obj = self._traffic_by_route_today.get(route_name) or {}
                by_route.append(
                    {
                        "route": route_name,
                        "total_download_bytes": int(total_obj.get("download_bytes", 0) or 0),
                        "total_upload_bytes": int(total_obj.get("upload_bytes", 0) or 0),
                        "today_download_bytes": int(today_obj.get("download_bytes", 0) or 0),
                        "today_upload_bytes": int(today_obj.get("upload_bytes", 0) or 0),
                    }
                )
            history = self._traffic_history.snapshot()
            return {
                "day": self._traffic_day,
                "run_id": self._run_id,
                "total_download_bytes": int(self._traffic_total_download_bytes),
                "total_upload_bytes": int(self._traffic_total_upload_bytes),
                "today_download_bytes": int(self._traffic_today_download_bytes),
                "today_upload_bytes": int(self._traffic_today_upload_bytes),
                "by_route": by_route,
                "current_run": history.get("current_run"),
                "runs": history.get("runs", []),
                "previous_runs": history.get("previous_runs", []),
                "runs_count": int(history.get("runs_count", 0) or 0),
                "previous_runs_count": int(history.get("previous_runs_count", 0) or 0),
                "history_total_download_bytes": int(history.get("history_total_download_bytes", 0) or 0),
                "history_total_upload_bytes": int(history.get("history_total_upload_bytes", 0) or 0),
                "previous_total_download_bytes": int(history.get("previous_total_download_bytes", 0) or 0),
                "previous_total_upload_bytes": int(history.get("previous_total_upload_bytes", 0) or 0),
            }

    def _current_traffic_day(self) -> str:
        tz_name = str(self.settings.tz or "").strip() or "UTC"
        try:
            tz = ZoneInfo(tz_name)
        except Exception:
            tz = timezone.utc
        return datetime.now(tz=tz).date().isoformat()

    def _ensure_traffic_day(self) -> None:
        day = self._current_traffic_day()
        if day == self._traffic_day:
            return
        self._traffic_day = day
        self._traffic_today_download_bytes = 0
        self._traffic_today_upload_bytes = 0
        self._traffic_by_route_today = {}

    def _record_traffic(self, *, route_name: str, download_bytes: int = 0, upload_bytes: int = 0) -> None:
        with self._traffic_lock:
            self._ensure_traffic_day()
            d = max(0, int(download_bytes))
            u = max(0, int(upload_bytes))
            if d <= 0 and u <= 0:
                return
            self._traffic_total_download_bytes += d
            self._traffic_total_upload_bytes += u
            self._traffic_today_download_bytes += d
            self._traffic_today_upload_bytes += u

            total_obj = self._traffic_by_route_total.setdefault(route_name, {"download_bytes": 0, "upload_bytes": 0})
            total_obj["download_bytes"] = int(total_obj.get("download_bytes", 0) or 0) + d
            total_obj["upload_bytes"] = int(total_obj.get("upload_bytes", 0) or 0) + u

            today_obj = self._traffic_by_route_today.setdefault(route_name, {"download_bytes": 0, "upload_bytes": 0})
            today_obj["download_bytes"] = int(today_obj.get("download_bytes", 0) or 0) + d
            today_obj["upload_bytes"] = int(today_obj.get("upload_bytes", 0) or 0) + u
            self._traffic_history.add_traffic(route_name=route_name, download_bytes=d, upload_bytes=u)

    def _close_traffic_history(self) -> None:
        try:
            self._traffic_history.close_current_run(stopped_at_ts=time.time())
        except Exception:
            logger.exception("Failed to close traffic history run")

    async def _audit_log(
        self,
        *,
        stage: str,
        status: str,
        incoming: IncomingChannelMessage | None = None,
        route: ChannelRoute | None = None,
        reason: str | None = None,
        trace_id: str | None = None,
        stage_timings_ms: dict[str, float] | None = None,
        stage_output: object | None = None,
    ) -> None:
        normalized_status = str(status or "").strip().lower()
        if normalized_status == "skipped":
            normalized_status = "blocked"
        stage_output_text = self._format_stage_output(stage_output)
        logger.info(
            "audit_event",
            extra={
                "details": {
                    "trace_id": trace_id,
                    "stage": stage,
                    "status": normalized_status,
                    "reason": reason,
                    "route": route.name if route else None,
                    "source_channel_id": incoming.source_channel_id if incoming else None,
                    "source_channel_username": incoming.source_channel_username if incoming else None,
                    "message_id": incoming.message_id if incoming else None,
                    "update_id": incoming.update_id if incoming else None,
                    "media_group_id": incoming.media_group_id if incoming else None,
                    "media_count": len(incoming.medias) if incoming else None,
                    "timings_ms": stage_timings_ms or {},
                    "stage_output": stage_output_text,
                }
            },
        )

        if not self.settings.log_channel_target:
            return

        if normalized_status not in {"ok", "failed", "blocked"}:
            return
        if normalized_status == "ok" and stage != "dispatch":
            return

        stage_title = {
            "dispatch": "🚀 Dispatch",
            "guard": "🛡 Guard Check",
            "script": "🧩 Script",
            "channel_script": "⚙️ Channel Script",
            "download": "📥 Download",
            "processing": "🧠 Processing",
            "route_match": "🧭 Route Match",
            "media_group_merge": "🖼 Media Group Merge",
        }.get(stage, stage)
        status_title = {
            "ok": "✅ Success",
            "failed": "❌ Failed",
            "blocked": "⛔ Blocked",
        }.get(normalized_status, normalized_status)

        lines = [f"🧾 Kiwi (کیوی) | {stage_title} | {status_title}"]
        if route:
            lines.append(f"🛣 Route: {route.name}")
        if incoming:
            lines.extend(
                [
                    f"🆔 update_id: {incoming.update_id}",
                    f"💬 message_id: {incoming.message_id}",
                    f"🔎 trace_id: {trace_id or '-'}",
                    f"📡 Source: {incoming.source_channel_username or incoming.source_channel_id}",
                    f"🧷 Media Count: {len(incoming.medias)}",
                ]
            )
            source_link = self._source_message_link(incoming)
            if source_link:
                lines.append(f"🔗 Message Link: {source_link}")
                lines.append(f"لینک پیام: {source_link}")
            if incoming.media_group_id:
                lines.append(f"🗂 media_group_id: {incoming.media_group_id}")
        if reason:
            lines.append(f"📝 Details: {reason}")
            lines.append(f"توضیح: {reason}")
        if stage_timings_ms:
            lines.append(f"⏱ timings_ms: {json.dumps(stage_timings_ms, ensure_ascii=False)}")
        if stage_output_text:
            lines.append(f"📤 stage_output: {stage_output_text}")

        message = "\n".join(lines)
        if len(message) > 3900:
            message = message[:3897] + "..."

        try:
            await self.telegram_client.send_message(self.settings.log_channel_target, message)
        except Exception:
            logger.exception(
                "Failed to send audit log to Telegram channel",
                extra={
                    "details": {
                        "stage": stage,
                        "status": status,
                        "log_channel_target": self.settings.log_channel_target,
                    }
                },
            )

    @staticmethod
    def _format_stage_output(stage_output: object, *, limit: int = 2500) -> str | None:
        if stage_output is None:
            return None
        try:
            if isinstance(stage_output, str):
                raw = stage_output
            else:
                raw = json.dumps(stage_output, ensure_ascii=False)
        except Exception:
            raw = str(stage_output)
        compact = " ".join(raw.split())
        if not compact:
            return None
        if len(compact) <= limit:
            return compact
        return compact[: limit - 3] + "..."

    @staticmethod
    def _source_message_link(incoming: IncomingChannelMessage) -> str | None:
        message_id = int(incoming.message_id or 0)
        if message_id <= 0:
            return None

        username = str(incoming.source_channel_username or "").strip()
        if username.startswith("@") and len(username) > 1:
            return f"https://t.me/{username[1:]}/{message_id}"

        channel_id = str(incoming.source_channel_id or "").strip()
        if channel_id.startswith("-100") and channel_id[4:].isdigit():
            return f"https://t.me/c/{channel_id[4:]}/{message_id}"
        return None

    @staticmethod
    def _build_trace_id(route: ChannelRoute, incoming: IncomingChannelMessage) -> str:
        group = incoming.media_group_id or "-"
        return f"{route.name}:{incoming.update_id}:{incoming.message_id}:{group}"

    def _check_cached_dispatch_permission(self, destination_target: str) -> tuple[bool, str | None] | None:
        key = str(destination_target or "").strip()
        if not key:
            return None
        item = self._dispatch_permission_cache.get(key)
        if item is None:
            return None
        expires_at, allowed, reason = item
        now = time.monotonic()
        if now >= float(expires_at):
            self._dispatch_permission_cache.pop(key, None)
            return None
        return bool(allowed), reason

    async def _check_dispatch_permission(self, route: ChannelRoute) -> str | None:
        destination_target = route.destination_target()
        key = str(destination_target or "").strip()
        if not key:
            return "destination_target_empty"

        cached = self._check_cached_dispatch_permission(key)
        if cached is not None:
            allowed, reason = cached
            return None if allowed else (reason or "destination_permission_denied")

        if self._bale_bot_user_id is None:
            return None

        try:
            await self.bale_client.get_chat_member(key, self._bale_bot_user_id)
            self._dispatch_permission_cache[key] = (
                time.monotonic() + self._dispatch_permission_cache_ttl_sec,
                True,
                None,
            )
            return None
        except PlatformApiError as exc:
            error_text = str(exc)
            if not self._is_permission_error_text(error_text):
                return None
            reason = f"destination_permission_denied:{key}"
            self._dispatch_permission_cache[key] = (
                time.monotonic() + self._dispatch_permission_cache_ttl_sec,
                False,
                reason,
            )
            return reason

    @staticmethod
    def _parse_bot_user_id(token: str | None) -> int | None:
        raw = str(token or "").strip()
        if not raw or ":" not in raw:
            return None
        head = raw.split(":", 1)[0].strip()
        if not head.isdigit():
            return None
        return int(head)

    @staticmethod
    def _merge_group_messages(messages: list[IncomingChannelMessage]) -> IncomingChannelMessage:
        deduped_by_message_id: dict[int, IncomingChannelMessage] = {}
        for msg in messages:
            msg_id = int(msg.message_id)
            existing = deduped_by_message_id.get(msg_id)
            if existing is None:
                deduped_by_message_id[msg_id] = msg
                continue
            deduped_by_message_id[msg_id] = KiwiService._merge_group_member(existing, msg)

        ordered = sorted(deduped_by_message_id.values(), key=lambda m: (m.message_id, m.update_id))
        first = ordered[0]
        last = ordered[-1]
        text = next((msg.text for msg in ordered if msg.text), None)
        caption = next((msg.caption for msg in ordered if msg.caption), None)
        merged_medias: list[IncomingMedia] = []
        for msg in ordered:
            merged_medias.extend(msg.medias)
        raw_updates: list[dict] = []
        for msg in ordered:
            if isinstance(msg.raw, dict):
                raw_updates.append(msg.raw)

        return IncomingChannelMessage(
            # Advance merged groups using the highest message id so checkpoint/progress
            # does not get stuck behind trailing members of the same album.
            update_id=max(msg.update_id for msg in ordered),
            source_channel_id=first.source_channel_id,
            source_channel_username=first.source_channel_username,
            message_id=max(msg.message_id for msg in ordered),
            date=last.date if last.date is not None else first.date,
            text=text,
            caption=caption,
            medias=merged_medias,
            raw={
                "group_updates": raw_updates,
                "group_message_ids": [int(msg.message_id) for msg in ordered],
                "group_message_start_id": int(first.message_id),
                "group_message_end_id": int(max(msg.message_id for msg in ordered)),
            },
            media_group_id=first.media_group_id,
        )

    @staticmethod
    def _group_bucket_messages(bucket: dict[str, object]) -> list[IncomingChannelMessage]:
        messages_by_id = bucket.get("messages_by_id")
        if not isinstance(messages_by_id, dict):
            return []
        out: list[IncomingChannelMessage] = []
        for key in sorted(messages_by_id.keys()):
            msg = messages_by_id.get(key)
            if isinstance(msg, IncomingChannelMessage):
                out.append(msg)
        return out

    @staticmethod
    def _merge_group_member(existing: IncomingChannelMessage, incoming: IncomingChannelMessage) -> IncomingChannelMessage:
        def _media_score(msg: IncomingChannelMessage) -> tuple[int, int]:
            has_resolved_ref = 0
            for media in msg.medias:
                if str(media.source or "").strip().lower() == "telethon" and isinstance(media.source_ref, dict):
                    has_resolved_ref = 1
                    break
            return (has_resolved_ref, len(msg.medias))

        if existing.medias and incoming.medias:
            medias = incoming.medias if _media_score(incoming) > _media_score(existing) else existing.medias
        elif incoming.medias:
            medias = incoming.medias
        else:
            medias = existing.medias

        raw: dict[str, Any]
        if isinstance(existing.raw, dict) and isinstance(incoming.raw, dict):
            if existing.raw == incoming.raw:
                raw = existing.raw
            else:
                raw = {"merged_variants": [existing.raw, incoming.raw]}
        elif isinstance(existing.raw, dict):
            raw = existing.raw
        elif isinstance(incoming.raw, dict):
            raw = incoming.raw
        else:
            raw = {}

        return IncomingChannelMessage(
            update_id=min(int(existing.update_id), int(incoming.update_id)),
            source_channel_id=existing.source_channel_id or incoming.source_channel_id,
            source_channel_username=existing.source_channel_username or incoming.source_channel_username,
            message_id=min(int(existing.message_id), int(incoming.message_id)),
            date=existing.date if existing.date is not None else incoming.date,
            text=existing.text or incoming.text,
            caption=existing.caption or incoming.caption,
            medias=medias,
            raw=raw,
            media_group_id=existing.media_group_id or incoming.media_group_id,
        )

    @staticmethod
    def _sync_incoming_message(target: IncomingChannelMessage, source: IncomingChannelMessage) -> None:
        target.update_id = int(source.update_id)
        target.source_channel_id = source.source_channel_id
        target.source_channel_username = source.source_channel_username
        target.message_id = int(source.message_id)
        target.date = source.date
        target.text = source.text
        target.caption = source.caption
        target.medias = list(source.medias)
        target.raw = source.raw if isinstance(source.raw, dict) else {}
        target.media_group_id = source.media_group_id

    @staticmethod
    def _script_message_to_dict(msg) -> dict:
        out: dict[str, object] = {"type": msg.type.value}
        if msg.text:
            out["text"] = msg.text
        if msg.path:
            out["path"] = msg.path
        if msg.caption:
            out["caption"] = msg.caption
        return out

    @staticmethod
    def _media_kind_to_output_kind(media_kind: str) -> OutputMessageKind:
        mapping = {
            MediaKind.PHOTO.value: OutputMessageKind.PHOTO,
            MediaKind.VIDEO.value: OutputMessageKind.VIDEO,
            MediaKind.VOICE.value: OutputMessageKind.VOICE,
            MediaKind.AUDIO.value: OutputMessageKind.AUDIO,
            MediaKind.DOCUMENT.value: OutputMessageKind.DOCUMENT,
            MediaKind.ANIMATION.value: OutputMessageKind.ANIMATION,
            MediaKind.STICKER.value: OutputMessageKind.STICKER,
            MediaKind.VIDEO_NOTE.value: OutputMessageKind.VIDEO_NOTE,
        }
        return mapping.get(media_kind, OutputMessageKind.DOCUMENT)

    def _build_passthrough_messages(self, *, incoming: IncomingChannelMessage, payload: dict) -> list[ScriptOutputMessage]:
        messages: list[ScriptOutputMessage] = []
        caption = (incoming.caption or incoming.text or "").strip() or None
        raw_inputs = payload.get("inputs")
        inputs = raw_inputs if isinstance(raw_inputs, list) else []

        for idx, raw_item in enumerate(inputs):
            if not isinstance(raw_item, dict):
                continue
            local_name = str(raw_item.get("local_name") or "").strip()
            if not local_name:
                continue
            kind_raw = str(raw_item.get("kind") or "").strip().lower()
            out_kind = self._media_kind_to_output_kind(kind_raw)
            if out_kind == OutputMessageKind.STICKER:
                continue
            messages.append(
                ScriptOutputMessage(
                    type=out_kind,
                    path=local_name,
                    caption=caption if idx == 0 else None,
                )
            )

        if messages:
            return messages

        text = (incoming.text or incoming.caption or "").strip()
        if text:
            return [ScriptOutputMessage(type=OutputMessageKind.TEXT, text=text)]
        return []

    @staticmethod
    def _preferred_local_name(media: IncomingMedia, *, file_path: str, idx: int) -> str:
        safe = KiwiService._sanitize_file_name(media.file_name)
        if safe:
            return safe

        file_name = Path(file_path).name
        safe = KiwiService._sanitize_file_name(file_name)
        if safe:
            return safe

        suffix = Path(file_path).suffix
        return f"input_{idx}_{media.kind.value}{suffix}"

    @staticmethod
    def _sanitize_file_name(name: str | None) -> str | None:
        if not name:
            return None
        normalized = Path(name.replace("\x00", "")).name.strip()
        if normalized in {"", ".", ".."}:
            return None
        return normalized

    @staticmethod
    def _allocate_local_name(preferred_name: str, *, idx: int, used_names: set[str]) -> Path:
        candidate = Path(preferred_name)
        candidate_key = str(candidate)
        if candidate_key not in used_names:
            used_names.add(candidate_key)
            return candidate

        n = 1
        while True:
            prefixed = Path(f"{idx}_{n}") / preferred_name
            key = str(prefixed)
            if key not in used_names:
                used_names.add(key)
                return prefixed
            n += 1

    @staticmethod
    def _resolve_output_message_path(
        value: str,
        *,
        output_dir: Path,
        input_dir: Path,
        extra_input_dirs: list[Path] | None = None,
    ) -> Path | None:
        candidate = Path(value)
        if candidate.is_absolute():
            return candidate if candidate.exists() else None

        out_path = output_dir / candidate
        if out_path.exists():
            return out_path

        in_path = input_dir / candidate
        if in_path.exists():
            return in_path

        for extra_dir in extra_input_dirs or []:
            extra_path = extra_dir / candidate
            if extra_path.exists():
                return extra_path
        return None

    @staticmethod
    def _is_promotional_text(text: str) -> bool:
        combined = str(text or "").strip().lower()
        if not combined:
            return False
        has_link = bool(re.search(r"(https?://|t\.me/|telegram\.me/|bit\.ly/)", combined))
        has_handle = bool(re.search(r"(^|\s)@\w{3,}", combined))
        cta_terms = (
            "buy now",
            "shop now",
            "order now",
            "join",
            "join now",
            "register",
            "sign up",
            "subscribe",
            "don't miss",
            "don’t miss",
            "deal",
            "vip",
            "exclusive",
            "خرید",
            "ثبت نام",
            "عضویت",
            "فرصت",
            "همین حالا",
            "ویژه",
        )
        cta_hits = sum(1 for token in cta_terms if token in combined)
        promo_terms = (
            "#ad",
            "sponsored",
            "affiliate",
            "referral",
            "تبلیغ",
            "اسپانسر",
            "پروموشن",
            "signal",
            "signals",
            "profit",
            "profit margin",
            "100%",
            "100 %",
            "high throughput",
            "win rate",
            "trading",
            "forex",
            "crypto signal",
            "premium channel",
            "community",
            "سیگنال",
            "سود",
            "درصد سود",
            "وین ریت",
        )
        promo_hits = sum(1 for token in promo_terms if token in combined)
        if promo_hits >= 2 and (has_link or has_handle):
            return True
        if promo_hits >= 3:
            return True
        if cta_hits >= 3 and (has_link or has_handle):
            return True
        return False

    def _is_obvious_promotional_output(self, messages: list[ScriptOutputMessage]) -> bool:
        for msg in messages:
            text = str(msg.text or "").strip()
            caption = str(msg.caption or "").strip()
            if text and self._is_promotional_text(text):
                return True
            if caption and self._is_promotional_text(caption):
                return True
        return False

    def _filter_oversized_output_messages(
        self,
        messages: list[ScriptOutputMessage],
        *,
        output_dir: Path,
        input_dir: Path,
        max_total_bytes: int,
        extra_input_dirs: list[Path] | None = None,
    ) -> tuple[list[ScriptOutputMessage], list[dict[str, object]]]:
        kept: list[ScriptOutputMessage] = []
        dropped: list[dict[str, object]] = []
        remaining = max(0, int(max_total_bytes))

        for idx, message in enumerate(messages):
            if message.type == OutputMessageKind.TEXT:
                kept.append(message)
                continue
            raw_path = str(message.path or "").strip()
            if not raw_path:
                kept.append(message)
                continue

            resolved = self._resolve_output_message_path(
                raw_path,
                output_dir=output_dir,
                input_dir=input_dir,
                extra_input_dirs=extra_input_dirs,
            )
            if resolved is None:
                kept.append(message)
                continue

            try:
                size_bytes = int(resolved.stat().st_size)
            except OSError:
                kept.append(message)
                continue

            if size_bytes > max_total_bytes:
                dropped.append(
                    {
                        "index": idx,
                        "type": message.type.value,
                        "path": raw_path,
                        "size_bytes": size_bytes,
                        "reason": "file_exceeds_limit",
                    }
                )
                continue
            if size_bytes > remaining:
                dropped.append(
                    {
                        "index": idx,
                        "type": message.type.value,
                        "path": raw_path,
                        "size_bytes": size_bytes,
                        "reason": "total_exceeds_limit",
                    }
                )
                continue

            remaining -= size_bytes
            kept.append(message)

        return kept, dropped

    def _estimate_output_messages_bytes(
        self,
        messages: list[ScriptOutputMessage],
        *,
        output_dir: Path,
        input_dir: Path,
        extra_input_dirs: list[Path] | None = None,
    ) -> int:
        total = 0
        for message in messages:
            if message.type == OutputMessageKind.TEXT:
                continue
            raw_path = str(message.path or "").strip()
            if not raw_path:
                continue
            resolved = self._resolve_output_message_path(
                raw_path,
                output_dir=output_dir,
                input_dir=input_dir,
                extra_input_dirs=extra_input_dirs,
            )
            if resolved is None:
                continue
            try:
                total += max(0, int(resolved.stat().st_size))
            except OSError:
                continue
        return int(total)
