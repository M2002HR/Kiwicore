from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import os
import time
from pathlib import Path
from typing import Any
from uuid import uuid4

from kiwi.config import RouteRegistry, Settings
from kiwi.dispatcher import BaleDispatcher
from kiwi.errors import GuardExecutionError, MessageTooLargeError, PlatformApiError, ScriptExecutionError
from kiwi.guard_runner import GuardRunner
from kiwi.keyword_links import KeywordLinker
from kiwi.platforms.parser import parse_telegram_channel_update, parse_telegram_private_message_update
from kiwi.script_runner import ScriptRunner
from kiwi.state import StateStore
from kiwi.storage import StorageManager
from kiwi.sync_ledger import SyncLedger, TERMINAL_STATUSES
from kiwi.sync_queue import InMemorySyncQueue, SyncQueueBackend, payload_hash_from_json
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
        self.admin_handler = admin_handler
        self._route_patch_callback = route_patch_callback

        self._offset: int | None = self.state_store.load_offset()
        self._stop_event = asyncio.Event()
        self._pending_media_groups: dict[tuple[str, str, str], dict[str, object]] = {}
        self._sync_seeded_routes: set[str] = set()
        self._retry_queue_hydrated = False
        self._sync_drain_task: asyncio.Task[int] | None = None
        self._sync_next_due_at: dict[str, float] = {}

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
        consecutive_poll_errors = 0
        try:
            while not self._stop_event.is_set():
                try:
                    updates_count = await self.run_once()
                    consecutive_poll_errors = 0
                    if updates_count == 0:
                        await asyncio.sleep(self.settings.poll_idle_sleep_sec)
                except asyncio.CancelledError:
                    raise
                except PlatformApiError as exc:
                    consecutive_poll_errors += 1
                    delay = min(self.settings.poll_error_sleep_sec * consecutive_poll_errors, 60.0)
                    logger.warning(
                        "Polling failed; retrying",
                        extra={
                            "details": {
                                "error": str(exc),
                                "retry_in_sec": delay,
                                "consecutive_errors": consecutive_poll_errors,
                            }
                        },
                    )
                    await asyncio.sleep(delay)
                except Exception:
                    consecutive_poll_errors += 1
                    delay = min(self.settings.poll_error_sleep_sec * consecutive_poll_errors, 60.0)
                    logger.exception(
                        "Unexpected error in polling loop; retrying",
                        extra={
                            "details": {
                                "retry_in_sec": delay,
                                "consecutive_errors": consecutive_poll_errors,
                            }
                        },
                    )
                    await asyncio.sleep(delay)
        finally:
            await self.aclose()
            logger.info("Kiwi service stopped")

    async def run_once(self) -> int:
        await self._ensure_sync_baseline()
        await self._hydrate_retry_queue_once()

        updates = await self.telegram_client.get_updates(
            offset=self._offset,
            timeout=self.settings.telegram_poll_timeout_sec,
            allowed_updates=self.settings.telegram_allowed_updates,
        )
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
            processed = 0
            for incoming, route in ready:
                if route.is_syncing():
                    await self._enqueue_sync_message(route, incoming)
                    continue
                await self._process_route_message(incoming, route)
                processed += 1
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

        processed = 0
        for incoming, route in ready_messages:
            if route.is_syncing():
                await self._enqueue_sync_message(route, incoming)
                continue
            await self._process_route_message(incoming, route)
            processed += 1

        processed += await self._tick_sync_drain()
        await self._auto_activate_ready_routes()
        return processed

    def set_route_patch_callback(self, callback: Any | None) -> None:
        self._route_patch_callback = callback

    async def _auto_activate_ready_routes(self) -> None:
        for route in list(self.routes.routes):
            await self._maybe_auto_activate_route(route)

    async def _maybe_auto_activate_route(self, route: ChannelRoute) -> None:
        if not route.is_syncing():
            return

        pending = self.sync_ledger.active_count_for_route(route.name)
        route.sync_pending_count = max(0, int(pending))
        if pending > 0:
            return

        checkpoint = int(self.sync_ledger.get_route_checkpoint(route.name) or 0)
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

        if latest > checkpoint:
            return

        if route.sync_status == "active" and route.enabled:
            return

        route.sync_status = "active"
        route.sync_enabled = False
        route.sync_seeded = True
        route.sync_pending_count = 0
        route.enabled = True

        logger.info(
            "Route sync completed and auto-activated",
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
                "enabled": True,
                "sync": {
                    "enabled": False,
                    "status": "active",
                    "seeded": True,
                    "pending_count": 0,
                },
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
        return processed

    async def _enqueue_sync_message(self, route: ChannelRoute, incoming: IncomingChannelMessage) -> None:
        checkpoint = self.sync_ledger.get_route_checkpoint(route.name)
        if checkpoint is not None and int(incoming.message_id) <= checkpoint:
            return

        payload = self._serialize_incoming(incoming)
        created, dedupe_key, status = self.sync_ledger.register_message(
            route_name=route.name,
            source_channel_id=incoming.source_channel_id,
            message_id=incoming.message_id,
            media_group_id=incoming.media_group_id,
            payload=payload,
        )
        if not created and status in TERMINAL_STATUSES:
            return
        if status == "ambiguous":
            return
        due_at = self._reserve_sync_due_at(route.name, route.sync_interval_sec)
        await self.sync_queue.enqueue(
            dedupe_key=dedupe_key,
            route_name=route.name,
            due_at=due_at,
            payload_hash=payload_hash_from_json(payload),
        )

    def _reserve_sync_due_at(self, route_name: str, interval_sec: int) -> float:
        now = time.time()
        interval = max(1, int(interval_sec))
        next_due_raw = self._sync_next_due_at.get(route_name)
        if next_due_raw is None:
            jitter_sec = self._initial_sync_jitter_sec(route_name, interval)
            self._sync_next_due_at[route_name] = now + jitter_sec + interval
            return now

        next_due = float(next_due_raw)
        due_at = max(now, next_due)
        self._sync_next_due_at[route_name] = due_at + interval
        return due_at

    @staticmethod
    def _initial_sync_jitter_sec(route_name: str, interval_sec: int) -> float:
        # Deterministic per-route jitter prevents startup stampede while staying stable across restarts.
        interval = max(1, int(interval_sec))
        digest = hashlib.sha256(str(route_name or "").encode("utf-8")).digest()
        jitter_bucket = int.from_bytes(digest[:4], byteorder="big", signed=False)
        return float(jitter_bucket % interval)

    async def _ensure_sync_baseline(self) -> None:
        prime_cursor = getattr(self.source_client, "prime_cursor", None)
        route_source_key = getattr(self.source_client, "route_source_key", None)
        latest_func = getattr(self.source_client, "latest_message_id_for_route", None)
        for route in self.routes.routes:
            if not route.is_syncing():
                continue
            if route.name in self._sync_seeded_routes:
                continue

            current_checkpoint = self.sync_ledger.get_route_checkpoint(route.name)
            latest_from_source = 0
            if callable(latest_func):
                try:
                    latest_from_source = max(0, int(await latest_func(route)))
                except Exception:
                    logger.exception(
                        "Failed to fetch latest source message id for sync baseline",
                        extra={"details": {"route": route.name}},
                    )

            if current_checkpoint is None:
                baseline = 0
                if latest_from_source > 0:
                    # Keep at most backfill_count historical messages on first sync bootstrap.
                    baseline = max(0, latest_from_source - max(0, int(route.sync_backfill_count)))
                if baseline <= 0:
                    baseline = self._latest_message_id_from_storage(route)
                self.sync_ledger.set_route_checkpoint(route.name, baseline)
                current_checkpoint = baseline
            elif latest_from_source > 0 and not bool(route.sync_seeded):
                # If route was explicitly restarted in syncing mode, avoid replaying very old history.
                desired_floor = max(0, latest_from_source - max(0, int(route.sync_backfill_count)))
                if int(current_checkpoint) < desired_floor:
                    self.sync_ledger.set_route_checkpoint(route.name, desired_floor)
                    current_checkpoint = desired_floor

            checkpoint = int(self.sync_ledger.get_route_checkpoint(route.name) or 0)
            if callable(prime_cursor) and callable(route_source_key):
                source_key = route_source_key(route)
                if isinstance(source_key, str) and source_key.strip():
                    prime_cursor(source_key, checkpoint)
            route.sync_seeded = True
            self._sync_seeded_routes.add(route.name)

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
            if route.source_channel_id and incoming.source_channel_id != route.source_channel_id:
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
        for key in keys:
            record = self.sync_ledger.get_record(key)
            if record is None:
                continue
            route = self._find_route(record.route_name)
            interval = route.sync_interval_sec if route is not None else 1
            due_at = self._reserve_sync_due_at(record.route_name, interval)
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

    async def _process_queued_item(self, item) -> int:
        record = self.sync_ledger.get_record(item.dedupe_key)
        if record is None:
            return 0
        if record.status in TERMINAL_STATUSES or record.status == "ambiguous":
            return 0

        route = self._find_route(record.route_name)
        if route is None:
            self.sync_ledger.mark_status(item.dedupe_key, status="skipped", last_error="route_not_found")
            return 1
        if not route.sync_enabled:
            self.sync_ledger.mark_status(item.dedupe_key, status="skipped", last_error="route_sync_disabled")
            return 1

        checkpoint = int(self.sync_ledger.get_route_checkpoint(route.name) or 0)
        if int(record.message_id) <= checkpoint:
            self.sync_ledger.mark_status(item.dedupe_key, status="skipped", last_error="older_than_checkpoint")
            return 1

        lock_owner = str(uuid4())
        acquired = await self.sync_queue.acquire_route_lock(
            route_name=route.name,
            owner=lock_owner,
            ttl_sec=int(self.settings.sync_lock_ttl_sec),
        )
        if not acquired:
            await self.sync_queue.schedule_retry(
                dedupe_key=item.dedupe_key,
                route_name=route.name,
                due_at=time.time() + 0.7,
                payload_hash=item.payload_hash,
            )
            return 0

        try:
            self.sync_ledger.mark_processing(item.dedupe_key, trace_id=f"sync:{item.dedupe_key}")
            incoming = self._incoming_from_payload(record.payload)
            status, last_error = await self._process_route_message_with_retries(
                incoming,
                route,
                retries=route.sync_retry_attempts,
            )

            if status == "ok":
                self.sync_ledger.mark_status(item.dedupe_key, status="sent", trace_id=f"sync:{item.dedupe_key}")
                self.sync_ledger.set_route_checkpoint(route.name, int(incoming.message_id))
                return 1

            if status in {"blocked", "skipped"}:
                self.sync_ledger.mark_status(
                    item.dedupe_key,
                    status=status,
                    last_error=last_error,
                    trace_id=f"sync:{item.dedupe_key}",
                )
                self.sync_ledger.set_route_checkpoint(route.name, int(incoming.message_id))
                return 1

            if status == "ambiguous":
                self.sync_ledger.mark_status(
                    item.dedupe_key,
                    status="ambiguous",
                    last_error=last_error,
                    trace_id=f"sync:{item.dedupe_key}",
                )
                review_id = self.sync_ledger.add_review(
                    dedupe_key=item.dedupe_key,
                    route_name=route.name,
                    reason="dispatch_ambiguous",
                    last_error=last_error,
                )
                await self._notify_review_alert(route=route, review_id=review_id, dedupe_key=item.dedupe_key, error=last_error)
                return 1

            attempts = int((self.sync_ledger.get_record(item.dedupe_key) or record).attempt_count)
            self.sync_ledger.mark_status(
                item.dedupe_key,
                status="failed",
                last_error=last_error,
                trace_id=f"sync:{item.dedupe_key}",
            )
            delay = self._retry_delay(attempts)
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

    async def _process_route_message_with_retries(
        self,
        incoming: IncomingChannelMessage,
        route: ChannelRoute,
        *,
        retries: int,
    ) -> tuple[str, str | None]:
        attempts = max(0, int(retries)) + 1
        last_status = "failed"
        last_error: str | None = None
        for idx in range(attempts):
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
            status, err = await self._process_route_message_detailed(incoming, route)
            last_status = status
            last_error = err
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
            if status in {"ok", "blocked", "skipped"}:
                return status, err
            if status == "ambiguous":
                if idx < attempts - 1 and self._is_transient_error_text(err):
                    await asyncio.sleep(0.8 * (idx + 1))
                    continue
                return status, err
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
            or "http 500" in text
            or "http 502" in text
            or "http 503" in text
            or "http 504" in text
            or "failed to upload file bytes" in text
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
    def _classify_local_processing_error(stage_name: str, exc: Exception) -> tuple[str, str] | None:
        stage = str(stage_name or "").strip().lower()
        text = str(exc or "").strip()
        lowered = text.lower()

        if stage == "download":
            if isinstance(exc, FileNotFoundError):
                return "skipped", "source_media_unavailable"
            if isinstance(exc, ValueError) and "source_ref" in lowered:
                return "skipped", "invalid_source_reference"
            if "empty file_path" in lowered:
                return "skipped", "source_file_path_missing"

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
    ) -> tuple[str, str | None]:
        trace_id = self._build_trace_id(route, incoming)
        started_at = time.monotonic()
        stage_timings_ms: dict[str, float] = {}
        stage_name = "download"

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

        permission_block_reason = await self._check_dispatch_permission(route)
        if permission_block_reason is not None:
            stage_timings_ms["total"] = round((time.monotonic() - started_at) * 1000.0, 2)
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

        paths = self.storage.prepare_message_paths(incoming)
        self.storage.write_raw_update(paths, incoming.raw)

        input_dir = Path(paths.input_dir)
        output_dir = Path(paths.output_dir)

        max_mb = route.max_message_mb or self.settings.default_max_message_mb
        max_total_bytes = max_mb * 1024 * 1024

        payload: dict = {
            "route": {
                "name": route.name,
                "source_channel_id": route.source_channel_id,
                "source_channel_username": route.source_channel_username,
                "destination_channel_id": route.destination_channel_id,
                "destination_channel_username": route.destination_channel_username,
                "destination_target": route.destination_target(),
                "channel_script": route.channel_script,
                "sync": {
                    "enabled": route.sync_enabled,
                    "status": route.sync_status,
                    "backfill_count": route.sync_backfill_count,
                    "interval_sec": route.sync_interval_sec,
                    "batch_size": route.sync_batch_size,
                    "retry_attempts": route.sync_retry_attempts,
                    "pending_count": route.sync_pending_count,
                    "processed_count": route.sync_processed_count,
                },
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
        used_local_names: set[str] = set()
        download_started = time.monotonic()
        try:
            for idx, media in enumerate(incoming.medias, start=1):
                if media.kind == MediaKind.STICKER:
                    logger.info(
                        "Sticker input skipped by policy",
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

                if media.source == "telethon" and isinstance(media.source_ref, dict):
                    if self.source_client is None:
                        raise RuntimeError("Telethon source client is not available for media download")
                    downloaded = await self.source_client.download_media(media.source_ref, target_path, max_bytes=remaining)
                else:
                    downloaded = await self.telegram_client.download_file(file_path, target_path, max_bytes=remaining)
                downloaded_total += downloaded
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
                    }
                )

            payload["downloaded_total_bytes"] = downloaded_total
            payload["max_total_bytes"] = max_total_bytes
            self.storage.write_payload(paths, payload)
            stage_timings_ms["download"] = round((time.monotonic() - download_started) * 1000.0, 2)
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
                return "blocked", block_reason
            await self._audit_log(
                stage="guard",
                status="ok",
                incoming=incoming,
                route=route,
                reason=f"گارد عبور داد ({route.gaurd_script})" if route.gaurd_script else "گارد تنظیم نشده بود؛ عبور مستقیم",
                trace_id=trace_id,
                stage_timings_ms=stage_timings_ms,
                stage_output={
                    "token": self.guard_runner.last_token,
                    "stdout": self.guard_runner.last_stdout,
                    "stderr": self.guard_runner.last_stderr,
                    "duration_ms": self.guard_runner.last_duration_ms,
                }
                if route.gaurd_script
                else {"skipped": True},
            )

            channel_started = time.monotonic()
            stage_name = "channel_script"
            if route.channel_script:
                run_result = await self.script_runner.run(
                    route,
                    payload_path=Path(paths.payload_path),
                    input_dir=input_dir,
                    output_dir=output_dir,
                    script_name=route.channel_script,
                    stage_name="channel_script",
                    trace_id=trace_id,
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

            if not run_result.messages:
                await self._audit_log(
                    stage="channel_script",
                    status="skipped",
                    incoming=incoming,
                    route=route,
                    reason="خروجی channel script خالی بود",
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
                return "skipped", "channel_script_empty_output"
            await self._audit_log(
                stage="channel_script",
                status="ok",
                incoming=incoming,
                route=route,
                reason=f"channel script اجرا شد ({len(run_result.messages)} پیام خروجی)"
                if route.channel_script
                else f"channel script تنظیم نشده بود؛ خروجی مستقیم از پیام ورودی ({len(run_result.messages)} پیام)",
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
                    status="skipped",
                    incoming=incoming,
                    route=route,
                    reason="همه خروجی‌های channel script به‌دلیل سقف حجم حذف شدند",
                    trace_id=trace_id,
                    stage_timings_ms=stage_timings_ms,
                )
                return "skipped", "channel_script_output_size_limit"

            final_result = ScriptRunResult(
                messages=self.keyword_linker.apply(route.destination_target(), filtered_messages),
                stdout=run_result.stdout,
                stderr=run_result.stderr,
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
            await self.dispatcher.dispatch(
                route.destination_target(),
                final_result.messages,
                output_dir=output_dir,
                input_dir=input_dir,
                extra_input_dirs=[],
            )
            stage_timings_ms["dispatch"] = round((time.monotonic() - dispatch_started) * 1000.0, 2)
            stage_timings_ms["total"] = round((time.monotonic() - started_at) * 1000.0, 2)
            await self._audit_log(
                stage="dispatch",
                status="ok",
                incoming=incoming,
                route=route,
                reason=f"ارسال شد ({len(final_result.messages)} پیام خروجی)",
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
                status="failed",
                incoming=incoming,
                route=route,
                reason=f"حجم پیام از حد مجاز بیشتر بود: {exc}",
                trace_id=trace_id,
                stage_timings_ms=stage_timings_ms,
            )
            logger.warning(
                "Message skipped: size limit exceeded",
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
            return "failed", str(exc)
        except PlatformApiError as exc:
            stage_timings_ms["total"] = round((time.monotonic() - started_at) * 1000.0, 2)
            error_text = str(exc)
            is_ambiguous = stage_name == "dispatch" and self._is_transient_error_text(error_text)
            is_permission = stage_name == "dispatch" and self._is_permission_error_text(error_text)
            if is_ambiguous:
                status = "ambiguous"
            elif is_permission:
                status = "blocked"
            else:
                status = "failed"
            await self._audit_log(
                stage=stage_name,
                status=status,
                incoming=incoming,
                route=route,
                reason=("عدم دسترسی ارسال به مقصد (permission_denied)" if is_permission else f"خطای پلتفرم: {exc}"),
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
                if "football_ai_generation_required_failed" in lowered:
                    status = "ambiguous"
                    error_text = "football_ai_generation_required_failed"
            stage_timings_ms["total"] = round((time.monotonic() - started_at) * 1000.0, 2)
            await self._audit_log(
                stage="processing",
                status=status,
                incoming=incoming,
                route=route,
                reason=(
                    "channel script نیازمند تولید AI بود اما تولید نشد (برای retry نگه داشته شد)"
                    if status == "ambiguous"
                    else "اجرای channel script خطا داد"
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
            return status, error_text
        except GuardExecutionError:
            stage_timings_ms["total"] = round((time.monotonic() - started_at) * 1000.0, 2)
            await self._audit_log(
                stage="guard",
                status="failed",
                incoming=incoming,
                route=route,
                reason=f"اجرای گارد خطا داد ({route.gaurd_script})",
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
            return "failed", "guard_execution_error"
        except Exception as exc:
            classified = self._classify_local_processing_error(stage_name, exc)
            if classified is not None:
                status, error_code = classified
                stage_timings_ms["total"] = round((time.monotonic() - started_at) * 1000.0, 2)
                reason = f"خطای منبع در مرحله {stage_name}: {exc}"
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
                    "Route processing skipped due to source input issue",
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
                return status, error_code
            stage_timings_ms["total"] = round((time.monotonic() - started_at) * 1000.0, 2)
            await self._audit_log(
                stage="processing",
                status="failed",
                incoming=incoming,
                route=route,
                reason="خطای غیرمنتظره در پردازش",
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
            return "failed", f"{stage_name}_unexpected_error"

    def _collect_ready_messages(
        self,
        matched_messages: list[tuple[IncomingChannelMessage, ChannelRoute]],
    ) -> list[tuple[IncomingChannelMessage, ChannelRoute]]:
        ready: list[tuple[IncomingChannelMessage, ChannelRoute]] = []
        now = asyncio.get_running_loop().time()
        seen_in_batch: dict[tuple[str, str, str], int] = {}

        for incoming, route in matched_messages:
            if not incoming.media_group_id:
                ready.append((incoming, route))
                continue

            group_key = (route.name, incoming.source_channel_id, incoming.media_group_id)
            seen_in_batch[group_key] = seen_in_batch.get(group_key, 0) + 1
            bucket = self._pending_media_groups.get(group_key)
            if bucket is None:
                bucket = {
                    "route": route,
                    "messages": [],
                    "first_seen": now,
                    "last_seen": now,
                }
                self._pending_media_groups[group_key] = bucket

            group_messages = bucket["messages"]
            assert isinstance(group_messages, list)
            group_messages.append(incoming)
            bucket["last_seen"] = now

            # Telegram albums are at most 10 media items.
            if len(group_messages) >= 10:
                ready.append((self._merge_group_messages(group_messages), route))
                self._pending_media_groups.pop(group_key, None)

        # If multiple items of one album arrived in this poll response,
        # flush it immediately to avoid unnecessary delay.
        for group_key, count in seen_in_batch.items():
            if count < 2:
                continue
            bucket = self._pending_media_groups.get(group_key)
            if bucket is None:
                continue
            route = bucket.get("route")
            group_messages = bucket.get("messages")
            if isinstance(route, ChannelRoute) and isinstance(group_messages, list) and group_messages:
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
            group_messages = bucket.get("messages")
            if isinstance(route, ChannelRoute) and isinstance(group_messages, list) and group_messages:
                ready.append((self._merge_group_messages(group_messages), route))
            keys_to_remove.append(key)

        for key in keys_to_remove:
            self._pending_media_groups.pop(key, None)
        return ready

    def set_routes(self, routes: RouteRegistry) -> None:
        self.routes = routes

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
        stage_output_text = self._format_stage_output(stage_output)
        logger.info(
            "audit_event",
            extra={
                "details": {
                    "trace_id": trace_id,
                    "stage": stage,
                    "status": status,
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

        if status not in {"ok", "failed", "blocked", "skipped"}:
            return
        if status == "ok" and stage != "dispatch":
            return

        stage_title = {
            "dispatch": "ارسال به مقصد",
            "guard": "بررسی گارد",
            "script": "اجرای اسکریپت",
            "channel_script": "اجرای channel script",
            "download": "دانلود فایل",
            "processing": "پردازش پیام",
            "route_match": "تطبیق مسیر",
            "media_group_merge": "تجمیع آلبوم",
        }.get(stage, stage)
        status_title = {
            "ok": "موفق",
            "failed": "خطا",
            "blocked": "مسدود",
            "skipped": "متوقف",
        }.get(status, status)

        lines = [
            f"کیوی | {stage_title} | {status_title}",
        ]
        if route:
            lines.append(f"مسیر: {route.name}")
        if incoming:
            lines.extend(
                [
                    f"update_id: {incoming.update_id}",
                    f"message_id: {incoming.message_id}",
                    f"trace_id: {trace_id or '-'}",
                    f"مبدا: {incoming.source_channel_username or incoming.source_channel_id}",
                    f"تعداد مدیا: {len(incoming.medias)}",
                ]
            )
            source_link = self._source_message_link(incoming)
            if source_link:
                lines.append(f"لینک پیام: {source_link}")
            if incoming.media_group_id:
                lines.append(f"media_group_id: {incoming.media_group_id}")
        if reason:
            lines.append(f"توضیح: {reason}")
        if stage_timings_ms:
            lines.append(f"timings_ms: {json.dumps(stage_timings_ms, ensure_ascii=False)}")
        if stage_output_text:
            lines.append(f"stage_output: {stage_output_text}")

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
        ordered = sorted(messages, key=lambda m: (m.message_id, m.update_id))
        first = ordered[0]
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
            update_id=min(msg.update_id for msg in ordered),
            source_channel_id=first.source_channel_id,
            source_channel_username=first.source_channel_username,
            message_id=first.message_id,
            date=first.date,
            text=text,
            caption=caption,
            medias=merged_medias,
            raw={"group_updates": raw_updates},
            media_group_id=first.media_group_id,
        )

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
