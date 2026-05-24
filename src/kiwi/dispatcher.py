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
        self.media_upload_fallback_mode = "none"

    async def dispatch(
        self,
        destination_target: str,
        messages: list[ScriptOutputMessage],
        *,
        output_dir: Path,
        input_dir: Path,
        extra_input_dirs: list[Path] | None = None,
    ) -> None:
        idx = 0
        while idx < len(messages):
            current = messages[idx]
            if current.type not in self.MEDIA_GROUP_TYPES:
                await self._send_one(
                    destination_target,
                    current,
                    output_dir=output_dir,
                    input_dir=input_dir,
                    extra_input_dirs=extra_input_dirs,
                )
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
                await self._send_one(
                    destination_target,
                    current,
                    output_dir=output_dir,
                    input_dir=input_dir,
                    extra_input_dirs=extra_input_dirs,
                )
                idx = j
                continue

            resolved_group: list[tuple[ScriptOutputMessage, Path, str | None, bool]] = []
            for item in group:
                path = self._resolve_path(
                    item.path or "",
                    output_dir=output_dir,
                    input_dir=input_dir,
                    extra_input_dirs=extra_input_dirs,
                )
                caption = item.caption.strip() if isinstance(item.caption, str) and item.caption.strip() else None
                resolved_group.append((item, path, caption, bool(item.append_destination_footer)))

            # Album-level caption should be carried by the first media item.
            # Some clients/platforms ignore captions on non-first items.
            group_caption_raw = None
            group_append_footer = True
            for _, _, cap, append_footer in resolved_group:
                if cap:
                    group_caption_raw = cap
                    group_append_footer = append_footer
                    break
            group_caption = self._with_destination_footer(
                group_caption_raw,
                destination_target=destination_target,
                ensure_nonempty=False,
                append_footer=group_append_footer,
            )
            media_group = []
            for index, (item, path, _, _) in enumerate(resolved_group):
                media_group.append({"type": item.type.value, "path": path, "caption": group_caption if index == 0 else None})

            try:
                await self._send_media_group_with_retry(destination_target, media_group)
            except PlatformApiError as exc:
                # Keep media-group delivery atomic. Falling back to single sends can split
                # albums in destination and produce unstable ordering/duplicates.
                raise exc
            idx = j

    async def _send_one(
        self,
        destination_target: str,
        message: ScriptOutputMessage,
        *,
        output_dir: Path,
        input_dir: Path,
        extra_input_dirs: list[Path] | None = None,
    ) -> bool:
        if message.type == OutputMessageKind.TEXT:
            raw_text = (message.text or "").strip()
            if not raw_text:
                return False
            text = (
                self._with_destination_footer(
                    raw_text,
                    destination_target=destination_target,
                    ensure_nonempty=False,
                    append_footer=bool(message.append_destination_footer),
                )
                or raw_text
            )
            await self.bale_client.send_message(destination_target, text)
            return True

        path = self._resolve_path(
            message.path or "",
            output_dir=output_dir,
            input_dir=input_dir,
            extra_input_dirs=extra_input_dirs,
        )

        if message.type == OutputMessageKind.PHOTO:
            return await self._send_media_with_text_fallback(
                send_primary=self.bale_client.send_photo,
                destination_target=destination_target,
                path=path,
                caption=self._with_destination_footer(
                    message.caption,
                    destination_target=destination_target,
                    ensure_nonempty=False,
                    append_footer=bool(message.append_destination_footer),
                ),
                media_kind=OutputMessageKind.PHOTO.value,
            )
        if message.type == OutputMessageKind.VIDEO:
            return await self._send_media_with_text_fallback(
                send_primary=self.bale_client.send_video,
                destination_target=destination_target,
                path=path,
                caption=self._with_destination_footer(
                    message.caption,
                    destination_target=destination_target,
                    ensure_nonempty=False,
                    append_footer=bool(message.append_destination_footer),
                ),
                media_kind=OutputMessageKind.VIDEO.value,
            )
        if message.type == OutputMessageKind.VOICE:
            return await self._send_media_with_text_fallback(
                send_primary=self.bale_client.send_voice,
                destination_target=destination_target,
                path=path,
                caption=self._with_destination_footer(
                    message.caption,
                    destination_target=destination_target,
                    ensure_nonempty=False,
                    append_footer=bool(message.append_destination_footer),
                ),
                media_kind=OutputMessageKind.VOICE.value,
            )
        if message.type == OutputMessageKind.AUDIO:
            return await self._send_media_with_text_fallback(
                send_primary=self.bale_client.send_audio,
                destination_target=destination_target,
                path=path,
                caption=self._with_destination_footer(
                    message.caption,
                    destination_target=destination_target,
                    ensure_nonempty=False,
                    append_footer=bool(message.append_destination_footer),
                ),
                media_kind=OutputMessageKind.AUDIO.value,
            )
        if message.type == OutputMessageKind.DOCUMENT:
            return await self._send_media_with_text_fallback(
                send_primary=self.bale_client.send_document,
                destination_target=destination_target,
                path=path,
                caption=self._with_destination_footer(
                    message.caption,
                    destination_target=destination_target,
                    ensure_nonempty=False,
                    append_footer=bool(message.append_destination_footer),
                ),
                media_kind=OutputMessageKind.DOCUMENT.value,
            )
        if message.type == OutputMessageKind.ANIMATION:
            return await self._send_media_with_text_fallback(
                send_primary=self.bale_client.send_animation,
                destination_target=destination_target,
                path=path,
                caption=self._with_destination_footer(
                    message.caption,
                    destination_target=destination_target,
                    ensure_nonempty=False,
                    append_footer=bool(message.append_destination_footer),
                ),
                media_kind=OutputMessageKind.ANIMATION.value,
            )
        if message.type == OutputMessageKind.STICKER:
            # Global policy: stickers are blocked and never sent.
            return False
        if message.type == OutputMessageKind.VIDEO_NOTE:
            await self.bale_client.send_video_note(destination_target, path)
            return True

        raise ValueError(f"Unsupported output message type: {message.type}")

    @staticmethod
    def _resolve_path(
        value: str,
        *,
        output_dir: Path,
        input_dir: Path,
        extra_input_dirs: list[Path] | None = None,
    ) -> Path:
        candidate = Path(value)
        if candidate.is_absolute():
            resolved = candidate
        else:
            out_path = output_dir / candidate
            if out_path.exists():
                resolved = out_path
            else:
                in_path = input_dir / candidate
                if in_path.exists():
                    resolved = in_path
                else:
                    resolved = in_path
                    for extra_dir in extra_input_dirs or []:
                        extra_path = extra_dir / candidate
                        if extra_path.exists():
                            resolved = extra_path
                            break

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

    async def _send_media_with_text_fallback(
        self,
        *,
        send_primary,
        destination_target: str,
        path: Path,
        caption: str | None,
        media_kind: str,
    ) -> bool:
        try:
            await send_primary(destination_target, path, caption=caption)
            return True
        except PlatformApiError as exc:
            mode = str(self.media_upload_fallback_mode or "none").strip().lower()
            # Never downgrade photos to document fallback. This avoids sending
            # images as generic files when transient upload issues happen.
            if media_kind == OutputMessageKind.PHOTO.value and mode == "document":
                mode = "none"
            if mode not in {"text", "document"} or not self._is_upload_bytes_error(exc):
                raise
            if mode == "document":
                await self.bale_client.send_document(destination_target, path, caption=caption)
                return True
            fallback_text = self._build_media_fallback_text(caption=caption, media_kind=media_kind, path=path)
            if not fallback_text:
                return False
            await self.bale_client.send_message(destination_target, fallback_text)
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
    def _is_upload_bytes_error(exc: PlatformApiError) -> bool:
        text = str(exc).lower()
        return (
            "failed to upload file bytes" in text
            or "http 413" in text
            or "request entity too large" in text
            or "payload too large" in text
            or "entity too large" in text
        )

    @staticmethod
    def _build_media_fallback_text(*, caption: str | None, media_kind: str, path: Path) -> str:
        base = str(caption or "").strip()
        if base:
            return base
        filename = path.name or "file"
        return f"ارسال مدیا موقتاً ناموفق بود ({media_kind}: {filename})."

    @staticmethod
    def _group_family(kind: OutputMessageKind) -> str:
        if kind in {OutputMessageKind.PHOTO, OutputMessageKind.VIDEO}:
            return "visual"
        if kind == OutputMessageKind.DOCUMENT:
            return "document"
        if kind == OutputMessageKind.AUDIO:
            return "audio"
        return kind.value

    @staticmethod
    def _with_destination_footer(
        value: str | None,
        *,
        destination_target: str,
        ensure_nonempty: bool,
        append_footer: bool = True,
    ) -> str | None:
        footer = str(destination_target or "").strip()
        base = str(value or "").strip()
        if not append_footer:
            if base:
                return base
            return "" if ensure_nonempty else None
        if not footer:
            return base or (None if not ensure_nonempty else "")
        if not base:
            return footer if ensure_nonempty else None
        if base.endswith(footer):
            return base
        return f"{base}\n{footer}"
