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
        self.last_dispatch_message_ids: list[int] = []
        self.last_dispatch_metadata: list[dict] = []
        self._last_send_response: object | None = None

    async def dispatch(
        self,
        destination_target: str,
        messages: list[ScriptOutputMessage],
        *,
        output_dir: Path,
        input_dir: Path,
        extra_input_dirs: list[Path] | None = None,
    ) -> None:
        self.last_dispatch_message_ids = []
        self.last_dispatch_metadata = []
        idx = 0
        while idx < len(messages):
            current = messages[idx]
            if current.type not in self.MEDIA_GROUP_TYPES:
                sent_ids = await self._send_one(
                    destination_target,
                    current,
                    output_dir=output_dir,
                    input_dir=input_dir,
                    extra_input_dirs=extra_input_dirs,
                )
                self.last_dispatch_message_ids.extend(sent_ids)
                self.last_dispatch_metadata.extend(self._extract_message_meta(self._last_send_response))
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
                sent_ids = await self._send_one(
                    destination_target,
                    current,
                    output_dir=output_dir,
                    input_dir=input_dir,
                    extra_input_dirs=extra_input_dirs,
                )
                self.last_dispatch_message_ids.extend(sent_ids)
                self.last_dispatch_metadata.extend(self._extract_message_meta(self._last_send_response))
                idx = j
                continue

            resolved_group: list[tuple[ScriptOutputMessage, Path, str | None, bool]] = []
            group_reply_markup: dict | None = None
            for item in group:
                path = self._resolve_path(
                    item.path or "",
                    output_dir=output_dir,
                    input_dir=input_dir,
                    extra_input_dirs=extra_input_dirs,
                )
                caption = item.caption.strip() if isinstance(item.caption, str) and item.caption.strip() else None
                resolved_group.append((item, path, caption, bool(item.append_destination_footer)))
                if group_reply_markup is None and isinstance(item.reply_markup, dict):
                    group_reply_markup = item.reply_markup

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
                response = await self._send_media_group_with_retry(destination_target, media_group)
                self.last_dispatch_message_ids.extend(self._extract_message_ids(response))
                self.last_dispatch_metadata.extend(self._extract_message_meta(response))
                if group_reply_markup is not None:
                    action = await self.bale_client.send_message(
                        destination_target,
                        "👇 انتخاب پرامپت",
                        reply_markup=group_reply_markup,
                    )
                    self.last_dispatch_message_ids.extend(self._extract_message_ids(action))
                    self.last_dispatch_metadata.extend(self._extract_message_meta(action))
            except PlatformApiError as exc:
                # Keep media-group delivery atomic. Falling back to single sends can split
                # albums in destination and produce unstable ordering/duplicates.
                if self._is_media_group_ambiguous_error(exc):
                    raise PlatformApiError(f"sendMediaGroup ambiguous: {exc}") from exc
                raise exc
            idx = j
        self.last_dispatch_message_ids = self._dedupe_message_ids(self.last_dispatch_message_ids)
        self.last_dispatch_metadata = self._dedupe_message_meta(self.last_dispatch_metadata)

    async def _send_one(
        self,
        destination_target: str,
        message: ScriptOutputMessage,
        *,
        output_dir: Path,
        input_dir: Path,
        extra_input_dirs: list[Path] | None = None,
    ) -> list[int]:
        self._last_send_response = None
        if message.type == OutputMessageKind.TEXT:
            raw_text = (message.text or "").strip()
            if not raw_text:
                return []
            text = (
                self._with_destination_footer(
                    raw_text,
                    destination_target=destination_target,
                    ensure_nonempty=False,
                    append_footer=bool(message.append_destination_footer),
                )
                or raw_text
            )
            response = await self.bale_client.send_message(destination_target, text, reply_markup=message.reply_markup)
            self._last_send_response = response
            return self._extract_message_ids(response)

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
                reply_markup=message.reply_markup,
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
                reply_markup=message.reply_markup,
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
                reply_markup=message.reply_markup,
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
                reply_markup=message.reply_markup,
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
                reply_markup=message.reply_markup,
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
                reply_markup=message.reply_markup,
                media_kind=OutputMessageKind.ANIMATION.value,
            )
        if message.type == OutputMessageKind.STICKER:
            # Global policy: stickers are blocked and never sent.
            return []
        if message.type == OutputMessageKind.VIDEO_NOTE:
            response = await self.bale_client.send_video_note(destination_target, path)
            self._last_send_response = response
            return self._extract_message_ids(response)

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

    async def _send_media_group_with_retry(self, destination_target: str, media_group: list[dict]) -> list[dict]:
        # Media groups are retry-unsafe (duplicate-prone on ambiguous failures).
        response = await self.bale_client.send_media_group(destination_target, media_group)
        if isinstance(response, list):
            return response
        return []

    async def _send_media_with_text_fallback(
        self,
        *,
        send_primary,
        destination_target: str,
        path: Path,
        caption: str | None,
        reply_markup: dict | None,
        media_kind: str,
    ) -> list[int]:
        try:
            response = await send_primary(destination_target, path, caption=caption, reply_markup=reply_markup)
            self._last_send_response = response
            return self._extract_message_ids(response)
        except PlatformApiError as exc:
            mode = str(self.media_upload_fallback_mode or "none").strip().lower()
            # Never downgrade photos to document fallback. This avoids sending
            # images as generic files when transient upload issues happen.
            if media_kind == OutputMessageKind.PHOTO.value and mode == "document":
                mode = "none"
            if mode not in {"text", "document"} or not self._is_upload_bytes_error(exc):
                raise
            if mode == "document":
                response = await self.bale_client.send_document(
                    destination_target,
                    path,
                    caption=caption,
                    reply_markup=reply_markup,
                )
                self._last_send_response = response
                return self._extract_message_ids(response)
            fallback_text = self._build_media_fallback_text(caption=caption, media_kind=media_kind, path=path)
            if not fallback_text:
                return []
            response = await self.bale_client.send_message(destination_target, fallback_text, reply_markup=reply_markup)
            self._last_send_response = response
            return self._extract_message_ids(response)

    async def _send_followup_markup(self, destination_target: str, reply_markup: dict | None) -> list[int]:
        if not isinstance(reply_markup, dict):
            return []
        response = await self.bale_client.send_message(
            destination_target,
            "👇 انتخاب پرامپت",
            reply_markup=reply_markup,
        )
        # Keep last-send response as the most recent outbound action for metadata snapshots.
        self._last_send_response = response
        return self._extract_message_ids(response)

    @staticmethod
    def _is_media_group_ambiguous_error(exc: PlatformApiError) -> bool:
        text = str(exc).lower()
        if not text:
            return False
        return (
            "network error" in text
            or "connecttimeout" in text
            or "readtimeout" in text
            or "timeout" in text
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
            or "invalid file http url specified" in text
            or "unsupported url protocol" in text
        )

    @staticmethod
    def _build_media_fallback_text(*, caption: str | None, media_kind: str, path: Path) -> str:
        base = str(caption or "").strip()
        if base:
            return base
        filename = path.name or "file"
        return f"ارسال مدیا موقتاً ناموفق بود ({media_kind}: {filename})."

    @staticmethod
    def _extract_message_ids(response: object) -> list[int]:
        out: list[int] = []

        def _collect_one(item: object) -> None:
            if not isinstance(item, dict):
                return
            for key in ("message_id", "id"):
                value = item.get(key)
                try:
                    msg_id = int(value)
                except Exception:
                    continue
                if msg_id > 0:
                    out.append(msg_id)
                    return

        if isinstance(response, list):
            for item in response:
                _collect_one(item)
        else:
            _collect_one(response)
        return BaleDispatcher._dedupe_message_ids(out)

    @staticmethod
    def _dedupe_message_ids(message_ids: list[int]) -> list[int]:
        out: list[int] = []
        seen: set[int] = set()
        for item in message_ids:
            value = int(item)
            if value <= 0 or value in seen:
                continue
            seen.add(value)
            out.append(value)
        return out

    @staticmethod
    def _extract_message_meta(response: object) -> list[dict]:
        items = response if isinstance(response, list) else [response]
        out: list[dict] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            meta = dict(item)
            out.append(meta)
        return out

    @staticmethod
    def _dedupe_message_meta(items: list[dict]) -> list[dict]:
        out: list[dict] = []
        seen: set[tuple[int, int]] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            msg_id = 0
            date_val = 0
            try:
                msg_id = int(item.get("message_id") or 0)
            except Exception:
                msg_id = 0
            try:
                date_val = int(float(item.get("date") or 0))
            except Exception:
                date_val = 0
            key = (msg_id, date_val)
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out

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
