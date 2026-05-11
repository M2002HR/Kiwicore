from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from kiwi.config import RouteRegistry, Settings, load_routes
from kiwi.dispatcher import BaleDispatcher
from kiwi.errors import GuardExecutionError, MessageTooLargeError, PlatformApiError, ScriptExecutionError
from kiwi.guard_runner import GuardRunner
from kiwi.platforms.parser import parse_telegram_channel_update, parse_telegram_private_message_update
from kiwi.script_runner import ScriptRunner
from kiwi.state import StateStore
from kiwi.storage import StorageManager
from kiwi.types import AdminInboundMessage, ChannelRoute, IncomingChannelMessage, IncomingMedia, MediaKind

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
        final_script_runner: ScriptRunner | None = None,
        state_store: StateStore,
        admin_handler: Any | None = None,
    ) -> None:
        self.settings = settings
        self.routes = routes
        self.telegram_client = telegram_client
        self.bale_client = bale_client
        self.source_client = source_client
        self.storage = storage
        self.guard_runner = guard_runner
        self.script_runner = script_runner
        self.final_script_runner = final_script_runner or script_runner
        self.state_store = state_store
        self.dispatcher = BaleDispatcher(self.bale_client)
        self.admin_handler = admin_handler

        self._offset: int | None = self.state_store.load_offset()
        self._stop_event = asyncio.Event()
        self._pending_media_groups: dict[tuple[str, str, str], dict[str, object]] = {}
        self._sync_queue: dict[str, list[IncomingChannelMessage]] = defaultdict(list)
        self._sync_queue_ids: dict[str, set[tuple[int, int]]] = defaultdict(set)
        self._sync_last_tick_at: dict[str, float] = {}
        self._sync_meta_lock = asyncio.Lock()

    async def stop(self) -> None:
        self._stop_event.set()

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
            ready = self._flush_ready_media_groups(force=False)
            for incoming in telethon_messages:
                routes = self.routes.match_all(incoming.source_channel_id, incoming.source_channel_username)
                for route in routes:
                    ready.append((incoming, route))
            ready.sort(key=lambda item: item[0].update_id)
            processed = 0
            for incoming, route in ready:
                if route.is_syncing():
                    await self._enqueue_sync_message(route, incoming)
                    continue
                await self._process_route_message(incoming, route)
                processed += 1
            await self._finalize_sync_seeding()
            processed += await self._run_sync_tick()
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

        ready_messages = self._collect_ready_messages(matched_messages)
        for incoming in telethon_messages:
            routes = self.routes.match_all(incoming.source_channel_id, incoming.source_channel_username)
            for route in routes:
                ready_messages.append((incoming, route))
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

        await self._finalize_sync_seeding()
        processed += await self._run_sync_tick()
        return processed

    async def _enqueue_sync_message(self, route: ChannelRoute, incoming: IncomingChannelMessage) -> None:
        queue = self._sync_queue[route.name]
        dedupe = self._sync_queue_ids[route.name]
        message_key = (incoming.update_id, incoming.message_id)
        if message_key in dedupe:
            return

        queue.append(incoming)
        dedupe.add(message_key)
        await self._sync_patch_route(
            route.name,
            {
                "sync.pending_count": len(queue),
                "sync.status": "syncing",
                "sync.enabled": True,
            },
            reload_routes=False,
        )

    async def _finalize_sync_seeding(self) -> None:
        for route in self.routes.routes:
            if not route.is_syncing() or route.sync_seeded:
                continue

            seeded_from_source = await self._seed_sync_from_source(route)
            if not seeded_from_source:
                await self._seed_sync_from_storage(route)
            queue = self._sync_queue.get(route.name) or []
            limit = max(0, int(route.sync_backfill_count))
            if limit > 0 and len(queue) > limit:
                drop_count = len(queue) - limit
                for _ in range(drop_count):
                    dropped = queue.pop(0)
                    self._sync_queue_ids[route.name].discard((dropped.update_id, dropped.message_id))

            await self._sync_patch_route(
                route.name,
                {
                    "sync.seeded": True,
                    "sync.pending_count": len(queue),
                    "sync.status": "syncing",
                    "sync.enabled": True,
                },
                reload_routes=False,
            )

    async def _seed_sync_from_source(self, route: ChannelRoute) -> bool:
        if self.source_client is None:
            return False
        seed_func = getattr(self.source_client, "seed_recent_messages", None)
        if not callable(seed_func):
            return False

        limit = max(0, int(route.sync_backfill_count))
        try:
            selected = await seed_func(route, limit)
        except Exception:
            logger.exception(
                "Sync seed from source failed; falling back to storage",
                extra={"details": {"route": route.name, "source": "telethon"}},
            )
            return False

        if not isinstance(selected, list):
            return False

        for incoming in selected:
            if not isinstance(incoming, IncomingChannelMessage):
                continue
            queue = self._sync_queue[route.name]
            dedupe = self._sync_queue_ids[route.name]
            key = (incoming.update_id, incoming.message_id)
            if key in dedupe:
                continue
            queue.append(incoming)
            dedupe.add(key)
        return True

    async def _seed_sync_from_storage(self, route: ChannelRoute) -> None:
        limit = max(0, int(route.sync_backfill_count))
        if limit <= 0:
            return

        messages_root = Path(self.settings.storage_dir) / "messages"
        if not messages_root.exists():
            return

        source_candidates: list[IncomingChannelMessage] = []
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

            source_candidates.append(incoming)

        if not source_candidates:
            return

        source_candidates.sort(key=lambda item: (item.update_id, item.message_id))
        selected = source_candidates[-limit:]
        for incoming in selected:
            queue = self._sync_queue[route.name]
            dedupe = self._sync_queue_ids[route.name]
            key = (incoming.update_id, incoming.message_id)
            if key in dedupe:
                continue
            queue.append(incoming)
            dedupe.add(key)

    async def _run_sync_tick(self) -> int:
        now = asyncio.get_running_loop().time()
        processed = 0

        for route in self.routes.routes:
            if not route.is_syncing():
                continue

            queue = self._sync_queue.get(route.name) or []
            if not queue:
                # Keep config in sync with in-memory queue and auto-finish.
                if route.sync_pending_count > 0:
                    await self._sync_patch_route(route.name, {"sync.pending_count": 0}, reload_routes=False)
                await self._maybe_finish_sync(route.name)
                continue

            last_tick = float(self._sync_last_tick_at.get(route.name) or 0.0)
            if now - last_tick < float(route.sync_interval_sec):
                continue

            self._sync_last_tick_at[route.name] = now
            take = min(max(1, int(route.sync_batch_size)), len(queue))

            for _ in range(take):
                item = queue.pop(0)
                self._sync_queue_ids[route.name].discard((item.update_id, item.message_id))
                status = await self._process_route_message_with_retries(item, route, retries=route.sync_retry_attempts)
                processed += 1

                patch: dict[str, object] = {"sync.pending_count": len(queue)}
                if status in {"ok", "blocked", "skipped"}:
                    patch["sync.processed_count"] = route.sync_processed_count + 1
                await self._sync_patch_route(route.name, patch, reload_routes=False)

            await self._maybe_finish_sync(route.name)

        return processed

    async def _maybe_finish_sync(self, route_name: str) -> None:
        route = self._find_route(route_name)
        if route is None or not route.is_syncing():
            return
        queue = self._sync_queue.get(route_name) or []
        if queue:
            return
        if route.sync_pending_count > 0:
            return
        await self._sync_patch_route(
            route_name,
            {
                "enabled": True,
                "sync.status": "active",
            },
            reload_routes=True,
        )

    def _find_route(self, route_name: str) -> ChannelRoute | None:
        for route in self.routes.routes:
            if route.name == route_name:
                return route
        return None

    async def _sync_patch_route(self, route_name: str, patch: dict[str, object], *, reload_routes: bool) -> None:
        async with self._sync_meta_lock:
            path = Path(self.settings.channels_config_path)
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, list):
                raise ValueError("channels config must be list")

            updated = False
            for item in raw:
                if not isinstance(item, dict):
                    continue
                if str(item.get("name") or "").strip() != route_name:
                    continue

                for key, value in patch.items():
                    if key.startswith("sync."):
                        _, sub = key.split(".", 1)
                        sync_obj = item.get("sync")
                        if not isinstance(sync_obj, dict):
                            sync_obj = {}
                        sync_obj[sub] = value
                        item["sync"] = sync_obj
                    else:
                        item[key] = value
                updated = True
                break

            if not updated:
                return

            path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")

            if reload_routes:
                self.routes = load_routes(str(path))
            else:
                # Update in-memory route meta without full reload to avoid churn.
                route = self._find_route(route_name)
                if route is not None:
                    for key, value in patch.items():
                        if key == "enabled":
                            route.enabled = bool(value)
                        elif key == "sync.status":
                            route.sync_status = str(value).strip().lower()
                        elif key == "sync.enabled":
                            route.sync_enabled = bool(value)
                        elif key == "sync.pending_count":
                            route.sync_pending_count = max(0, int(value))
                        elif key == "sync.processed_count":
                            route.sync_processed_count = max(0, int(value))
                        elif key == "sync.seeded":
                            route.sync_seeded = bool(value)

    async def _process_route_message_with_retries(
        self,
        incoming: IncomingChannelMessage,
        route: ChannelRoute,
        *,
        retries: int,
    ) -> str:
        attempts = max(0, int(retries)) + 1
        last_status = "failed"
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
            status = await self._process_route_message(incoming, route)
            last_status = status
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
                    }
                },
            )
            if status in {"ok", "blocked", "skipped"}:
                return status
            if idx < attempts - 1:
                await asyncio.sleep(0.8 * (idx + 1))
        return last_status

    async def _process_admin_message(self, incoming: AdminInboundMessage) -> None:
        try:
            response = self.admin_handler.handle(incoming)
        except Exception as exc:
            response = f"خطا در مدیریت: {exc}"
        if isinstance(response, str):
            text = response
            reply_markup = None
        else:
            text = str(getattr(response, "text", "") or "")
            reply_markup = getattr(response, "reply_markup", None)
            if not text:
                text = "پاسخ خالی از مدیریت دریافت شد."
        try:
            await self.telegram_client.send_message(incoming.chat_id, text, reply_markup=reply_markup)
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

    async def _process_route_message(self, incoming: IncomingChannelMessage, route: ChannelRoute) -> str:
        trace_id = self._build_trace_id(route, incoming)
        started_at = time.monotonic()
        stage_timings_ms: dict[str, float] = {}

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
                    "final_script": route.final_script,
                    "gaurd_script": route.gaurd_script,
                }
            },
        )

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
                "final_script": route.final_script,
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
                return "blocked"
            await self._audit_log(
                stage="guard",
                status="ok",
                incoming=incoming,
                route=route,
                reason=f"گارد عبور داد ({route.gaurd_script})",
                trace_id=trace_id,
                stage_timings_ms=stage_timings_ms,
                stage_output={
                    "token": self.guard_runner.last_token,
                    "stdout": self.guard_runner.last_stdout,
                    "stderr": self.guard_runner.last_stderr,
                    "duration_ms": self.guard_runner.last_duration_ms,
                },
            )

            channel_started = time.monotonic()
            run_result = await self.script_runner.run(
                route,
                payload_path=Path(paths.payload_path),
                input_dir=input_dir,
                output_dir=output_dir,
                script_name=route.channel_script,
                stage_name="channel_script",
                trace_id=trace_id,
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
                return "skipped"
            await self._audit_log(
                stage="channel_script",
                status="ok",
                incoming=incoming,
                route=route,
                reason=f"channel script اجرا شد ({len(run_result.messages)} پیام خروجی)",
                trace_id=trace_id,
                stage_timings_ms=stage_timings_ms,
                stage_output={
                    "stdout": run_result.stdout,
                    "stderr": run_result.stderr,
                    "messages": [self._script_message_to_dict(msg) for msg in run_result.messages],
                },
            )

            final_payload_path = output_dir / "final_payload.json"
            final_output_dir = output_dir / "final_stage"
            final_output_dir.mkdir(parents=True, exist_ok=True)
            final_payload = {
                "route": payload["route"],
                "message": payload["message"],
                "messages": [self._script_message_to_dict(msg) for msg in run_result.messages],
            }
            final_payload_path.write_text(json.dumps(final_payload, ensure_ascii=False), encoding="utf-8")
            logger.info(
                "Final payload prepared",
                extra={
                    "details": {
                        "trace_id": trace_id,
                        "route": route.name,
                        "final_payload_path": str(final_payload_path),
                        "final_input_messages_count": len(run_result.messages),
                    }
                },
            )

            final_script_path = self.final_script_runner.scripts_dir / route.final_script
            final_started = time.monotonic()
            if final_script_path.exists():
                final_result = await self.final_script_runner.run(
                    route,
                    payload_path=final_payload_path,
                    input_dir=output_dir,
                    output_dir=final_output_dir,
                    script_name=route.final_script,
                    stage_name="final_script",
                    trace_id=trace_id,
                )
            else:
                fallback_name = "default_final_script.py"
                fallback_path = self.final_script_runner.scripts_dir / fallback_name
                if fallback_path.exists():
                    logger.warning(
                        "Final script not found; using default final script",
                        extra={
                            "details": {
                                "route": route.name,
                                "trace_id": trace_id,
                                "final_script": route.final_script,
                                "missing_path": str(final_script_path),
                                "fallback_script": fallback_name,
                            }
                        },
                    )
                    final_result = await self.final_script_runner.run(
                        route,
                        payload_path=final_payload_path,
                        input_dir=output_dir,
                        output_dir=final_output_dir,
                        script_name=fallback_name,
                        stage_name="final_script",
                        trace_id=trace_id,
                    )
                else:
                    logger.warning(
                        "Final script not found; dispatching channel script output as-is",
                        extra={
                            "details": {
                                "route": route.name,
                                "trace_id": trace_id,
                                "final_script": route.final_script,
                                "missing_path": str(final_script_path),
                            }
                        },
                    )
                    final_result = run_result
            stage_timings_ms["final_script"] = round((time.monotonic() - final_started) * 1000.0, 2)
            logger.info(
                "Final script stage completed",
                extra={
                    "details": {
                        "trace_id": trace_id,
                        "route": route.name,
                        "final_script": route.final_script,
                        "output_messages_count": len(final_result.messages),
                        "stage_ms": stage_timings_ms["final_script"],
                    }
                },
            )

            if not final_result.messages:
                await self._audit_log(
                    stage="final_script",
                    status="skipped",
                    incoming=incoming,
                    route=route,
                    reason="خروجی final script خالی بود",
                    trace_id=trace_id,
                    stage_timings_ms=stage_timings_ms,
                )
                logger.info(
                    "Final script generated no output messages",
                    extra={
                        "details": {
                            "route": route.name,
                            "trace_id": trace_id,
                            "source_channel_id": incoming.source_channel_id,
                            "update_id": incoming.update_id,
                            "final_script": route.final_script,
                            "stage_ms": stage_timings_ms["final_script"],
                        }
                    },
                )
                return "skipped"

            await self._audit_log(
                stage="final_script",
                status="ok",
                incoming=incoming,
                route=route,
                reason=f"فاینال‌اسکریپت اجرا شد ({len(final_result.messages)} پیام خروجی)",
                trace_id=trace_id,
                stage_timings_ms=stage_timings_ms,
                stage_output={
                    "stdout": final_result.stdout,
                    "stderr": final_result.stderr,
                    "messages": [self._script_message_to_dict(msg) for msg in final_result.messages],
                },
            )
            (final_output_dir / "final_messages.json").write_text(
                json.dumps(
                    {"messages": [self._script_message_to_dict(msg) for msg in final_result.messages]},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            dispatch_started = time.monotonic()
            await self.dispatcher.dispatch(
                route.destination_target(),
                final_result.messages,
                output_dir=final_output_dir,
                input_dir=input_dir,
                extra_input_dirs=[output_dir],
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
            return "ok"

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
            return "failed"
        except ScriptExecutionError:
            stage_timings_ms["total"] = round((time.monotonic() - started_at) * 1000.0, 2)
            await self._audit_log(
                stage="processing",
                status="failed",
                incoming=incoming,
                route=route,
                reason="اجرای channel/final script خطا داد",
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
            return "failed"
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
            return "failed"
        except Exception:
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
            return "failed"

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

        stage_title = {
            "dispatch": "ارسال به مقصد",
            "guard": "بررسی گارد",
            "script": "اجرای اسکریپت",
            "channel_script": "اجرای channel script",
            "final_script": "اجرای final script",
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
