from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from kiwi.config import RouteRegistry, Settings
from kiwi.dispatcher import BaleDispatcher
from kiwi.errors import GuardExecutionError, MessageTooLargeError, PlatformApiError, ScriptExecutionError
from kiwi.guard_runner import GuardRunner
from kiwi.platforms.parser import parse_telegram_channel_update
from kiwi.script_runner import ScriptRunner
from kiwi.state import StateStore
from kiwi.storage import StorageManager
from kiwi.types import ChannelRoute, IncomingChannelMessage, MediaKind

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

        self._offset: int | None = self.state_store.load_offset()
        self._stop_event = asyncio.Event()

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
            return 0

        processed = 0
        for update in updates:
            update_id = int(update.get("update_id") or 0)
            if update_id:
                self._offset = update_id + 1
                self.state_store.save_offset(self._offset)

            incoming = parse_telegram_channel_update(update)
            if incoming is None:
                continue

            route = self.routes.match(incoming.source_channel_id, incoming.source_channel_username)
            if route is None:
                continue

            await self._process_route_message(incoming, route)
            processed += 1

        return processed

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

                suffix = Path(file_path).suffix or ""
                target_name = f"input_{idx}_{media.kind.value}{suffix}"
                target_path = input_dir / target_name

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
                        "local_name": target_name,
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

        except MessageTooLargeError as exc:
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
