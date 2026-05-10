from __future__ import annotations

import asyncio
from pathlib import Path

from kiwi.errors import PlatformApiError
from kiwi.types import OutputMessageKind, ScriptOutputMessage


class BaleDispatcher:
    MEDIA_GROUP_TYPES = {
        OutputMessageKind.PHOTO,
        OutputMessageKind.VIDEO,
        OutputMessageKind.DOCUMENT,
        OutputMessageKind.AUDIO,
    }

    def __init__(self, bale_client) -> None:
        self.bale_client = bale_client

    async def dispatch(
        self,
        destination_target: str,
        messages: list[ScriptOutputMessage],
        *,
        output_dir: Path,
        input_dir: Path,
    ) -> None:
        idx = 0
        while idx < len(messages):
            current = messages[idx]
            if current.type not in self.MEDIA_GROUP_TYPES:
                await self._send_one(destination_target, current, output_dir=output_dir, input_dir=input_dir)
                idx += 1
                continue

            group: list[ScriptOutputMessage] = [current]
            group_family = self._group_family(current.type)
            j = idx + 1
            while (
                j < len(messages)
                and messages[j].type in self.MEDIA_GROUP_TYPES
                and self._group_family(messages[j].type) == group_family
            ):
                group.append(messages[j])
                j += 1

            if len(group) < 2:
                await self._send_one(destination_target, current, output_dir=output_dir, input_dir=input_dir)
                idx = j
                continue

            media_group = []
            for item in group:
                path = self._resolve_path(item.path or "", output_dir=output_dir, input_dir=input_dir)
                media_group.append({"type": item.type.value, "path": path, "caption": item.caption})

            try:
                await self._send_media_group_with_retry(destination_target, media_group)
            except PlatformApiError:
                sent_any = False
                last_error: PlatformApiError | None = None
                for item in group:
                    try:
                        sent_any = await self._send_one(
                            destination_target,
                            item,
                            output_dir=output_dir,
                            input_dir=input_dir,
                        ) or sent_any
                    except PlatformApiError as exc:
                        last_error = exc
                        continue
                if not sent_any and last_error is not None:
                    raise last_error
            idx = j

    async def _send_one(
        self,
        destination_target: str,
        message: ScriptOutputMessage,
        *,
        output_dir: Path,
        input_dir: Path,
    ) -> bool:
        if message.type == OutputMessageKind.TEXT:
            await self.bale_client.send_message(destination_target, message.text or "")
            return True

        path = self._resolve_path(message.path or "", output_dir=output_dir, input_dir=input_dir)

        if message.type == OutputMessageKind.PHOTO:
            return await self._send_with_document_fallback(
                send_primary=self.bale_client.send_photo,
                destination_target=destination_target,
                path=path,
                caption=message.caption,
            )
        if message.type == OutputMessageKind.VIDEO:
            return await self._send_with_document_fallback(
                send_primary=self.bale_client.send_video,
                destination_target=destination_target,
                path=path,
                caption=message.caption,
            )
        if message.type == OutputMessageKind.VOICE:
            return await self._send_with_document_fallback(
                send_primary=self.bale_client.send_voice,
                destination_target=destination_target,
                path=path,
                caption=message.caption,
            )
        if message.type == OutputMessageKind.AUDIO:
            return await self._send_with_document_fallback(
                send_primary=self.bale_client.send_audio,
                destination_target=destination_target,
                path=path,
                caption=message.caption,
            )
        if message.type == OutputMessageKind.DOCUMENT:
            await self.bale_client.send_document(destination_target, path, caption=message.caption)
            return True
        if message.type == OutputMessageKind.ANIMATION:
            return await self._send_with_document_fallback(
                send_primary=self.bale_client.send_animation,
                destination_target=destination_target,
                path=path,
                caption=message.caption,
            )
        if message.type == OutputMessageKind.STICKER:
            # Global policy: stickers are blocked and never sent.
            return False
        if message.type == OutputMessageKind.VIDEO_NOTE:
            await self.bale_client.send_video_note(destination_target, path)
            return True

        raise ValueError(f"Unsupported output message type: {message.type}")

    @staticmethod
    def _resolve_path(value: str, *, output_dir: Path, input_dir: Path) -> Path:
        candidate = Path(value)
        if candidate.is_absolute():
            resolved = candidate
        else:
            out_path = output_dir / candidate
            if out_path.exists():
                resolved = out_path
            else:
                resolved = input_dir / candidate

        if not resolved.exists():
            raise FileNotFoundError(str(resolved))
        return resolved

    async def _send_media_group_with_retry(self, destination_target: str, media_group: list[dict]) -> None:
        retries = 3  # initial try + 2 retries
        backoff_sec = 0.8
        last_error: PlatformApiError | None = None
        for attempt in range(1, retries + 1):
            try:
                await self.bale_client.send_media_group(destination_target, media_group)
                return
            except PlatformApiError as exc:
                last_error = exc
                if attempt >= retries or not self._is_transient_error(exc):
                    raise
                await asyncio.sleep(backoff_sec * attempt)
        assert last_error is not None
        raise last_error

    async def _send_with_document_fallback(
        self,
        *,
        send_primary,
        destination_target: str,
        path: Path,
        caption: str | None,
    ) -> bool:
        try:
            await send_primary(destination_target, path, caption=caption)
            return True
        except PlatformApiError as exc:
            if not self._is_transient_error(exc):
                raise
            await self.bale_client.send_document(destination_target, path, caption=caption)
            return True

    @staticmethod
    def _is_transient_error(exc: PlatformApiError) -> bool:
        text = str(exc).lower()
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
    def _group_family(kind: OutputMessageKind) -> str:
        if kind in {OutputMessageKind.PHOTO, OutputMessageKind.VIDEO}:
            return "visual"
        if kind == OutputMessageKind.DOCUMENT:
            return "document"
        if kind == OutputMessageKind.AUDIO:
            return "audio"
        return kind.value
