from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from kiwi.config import RouteRegistry, Settings
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
        storage: StorageManager,
        guard_runner: GuardRunner,
        script_runner: ScriptRunner,
        state_store: StateStore,
        admin_handler: Any | None = None,
    ) -> None:
        self.settings = settings
        self.routes = routes
        self.telegram_client = telegram_client
        self.bale_client = bale_client
        self.storage = storage
        self.guard_runner = guard_runner
        self.script_runner = script_runner
        self.state_store = state_store
        self.dispatcher = BaleDispatcher(self.bale_client)
        self.admin_handler = admin_handler

        self._offset: int | None = self.state_store.load_offset()
        self._stop_event = asyncio.Event()
        self._pending_media_groups: dict[tuple[str, str, str], dict[str, object]] = {}

    async def stop(self) -> None:
        self._stop_event.set()

    async def aclose(self) -> None:
        await self.telegram_client.aclose()
        await self.bale_client.aclose()

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
        if not updates:
            ready = self._flush_ready_media_groups(force=False)
            processed = 0
            for incoming, route in ready:
                await self._process_route_message(incoming, route)
                processed += 1
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
        ready_messages.extend(self._flush_ready_media_groups(force=False))
        ready_messages.sort(key=lambda item: item[0].update_id)

        processed = 0
        for incoming, route in ready_messages:
            await self._process_route_message(incoming, route)
            processed += 1

        return processed

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

    async def _process_route_message(self, incoming: IncomingChannelMessage, route: ChannelRoute) -> None:
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
                "script": route.script,
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
        try:
            for idx, media in enumerate(incoming.medias, start=1):
                if media.kind == MediaKind.STICKER:
                    logger.info(
                        "Sticker input skipped by policy",
                        extra={
                            "details": {
                                "route": route.name,
                                "source_channel_id": incoming.source_channel_id,
                                "update_id": incoming.update_id,
                                "file_id": media.file_id,
                            }
                        },
                    )
                    continue
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

                downloaded = await self.telegram_client.download_file(file_path, target_path, max_bytes=remaining)
                downloaded_total += downloaded

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

            is_allowed = await self.guard_runner.run(
                route,
                payload_path=Path(paths.payload_path),
                input_dir=input_dir,
                output_dir=output_dir,
            )
            if not is_allowed:
                block_reason = self.guard_runner.last_reason or "پیام توسط گارد رد شد"
                await self._audit_log(
                    stage="guard",
                    status="blocked",
                    incoming=incoming,
                    route=route,
                    reason=block_reason,
                )
                logger.info(
                    "Message blocked by guard script",
                    extra={
                        "details": {
                            "route": route.name,
                            "source_channel_id": incoming.source_channel_id,
                            "update_id": incoming.update_id,
                            "gaurd_script": route.gaurd_script,
                        }
                    },
                )
                return

            run_result = await self.script_runner.run(
                route,
                payload_path=Path(paths.payload_path),
                input_dir=input_dir,
                output_dir=output_dir,
            )

            if not run_result.messages:
                await self._audit_log(
                    stage="script",
                    status="skipped",
                    incoming=incoming,
                    route=route,
                    reason="اسکریپت خروجی نداشت",
                )
                logger.info(
                    "Script generated no output messages",
                    extra={
                        "details": {
                            "route": route.name,
                            "source_channel_id": incoming.source_channel_id,
                            "update_id": incoming.update_id,
                        }
                    },
                )
                return

            await self.dispatcher.dispatch(
                route.destination_target(),
                run_result.messages,
                output_dir=output_dir,
                input_dir=input_dir,
            )
            await self._audit_log(
                stage="dispatch",
                status="ok",
                incoming=incoming,
                route=route,
                reason=f"ارسال شد ({len(run_result.messages)} پیام خروجی)",
            )

        except MessageTooLargeError as exc:
            await self._audit_log(
                stage="download",
                status="failed",
                incoming=incoming,
                route=route,
                reason=f"حجم پیام از حد مجاز بیشتر بود: {exc}",
            )
            logger.warning(
                "Message skipped: size limit exceeded",
                extra={
                    "details": {
                        "route": route.name,
                        "source_channel_id": incoming.source_channel_id,
                        "update_id": incoming.update_id,
                        "max_mb": max_mb,
                        "error": str(exc),
                    }
                },
            )
        except ScriptExecutionError:
            await self._audit_log(
                stage="script",
                status="failed",
                incoming=incoming,
                route=route,
                reason="اجرای اسکریپت خطا داد",
            )
            logger.exception(
                "Script execution failed",
                extra={
                    "details": {
                        "route": route.name,
                        "source_channel_id": incoming.source_channel_id,
                        "update_id": incoming.update_id,
                    }
                },
            )
        except GuardExecutionError:
            await self._audit_log(
                stage="guard",
                status="failed",
                incoming=incoming,
                route=route,
                reason=f"اجرای گارد خطا داد ({route.gaurd_script})",
            )
            logger.exception(
                "Guard script execution failed",
                extra={
                    "details": {
                        "route": route.name,
                        "source_channel_id": incoming.source_channel_id,
                        "update_id": incoming.update_id,
                        "gaurd_script": route.gaurd_script,
                    }
                },
            )
        except Exception:
            await self._audit_log(
                stage="processing",
                status="failed",
                incoming=incoming,
                route=route,
                reason="خطای غیرمنتظره در پردازش",
            )
            logger.exception(
                "Unexpected error in route processing",
                extra={
                    "details": {
                        "route": route.name,
                        "source_channel_id": incoming.source_channel_id,
                        "update_id": incoming.update_id,
                    }
                },
            )

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
    ) -> None:
        logger.info(
            "audit_event",
            extra={
                "details": {
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
                    f"مبدا: {incoming.source_channel_username or incoming.source_channel_id}",
                    f"تعداد مدیا: {len(incoming.medias)}",
                ]
            )
            if incoming.media_group_id:
                lines.append(f"media_group_id: {incoming.media_group_id}")
        if reason:
            lines.append(f"توضیح: {reason}")

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
