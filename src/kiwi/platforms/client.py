from __future__ import annotations

import asyncio
import json
import mimetypes
import re
from pathlib import Path

import httpx

from kiwi.errors import MessageTooLargeError, PlatformApiError


class BotApiClient:
    _INLINE_LINK_RE = re.compile(r"\[[^\]\n]+\]\(\s*https?://[^)\s]+\s*\)", re.IGNORECASE)
    _INLINE_HTML_LINK_RE = re.compile(r"<a\s+href=(['\"])https?://.+?\1>.*?</a>", re.IGNORECASE | re.DOTALL)

    def __init__(
        self,
        *,
        token: str,
        api_base_url: str,
        file_base_url: str,
        timeout_sec: float = 40.0,
        upload_max_concurrency: int | None = None,
        trust_env: bool = False,
    ) -> None:
        self.token = token
        self.api_base_url = api_base_url.rstrip("/")
        self.file_base_url = file_base_url.rstrip("/")
        timeout = httpx.Timeout(
            connect=15.0,
            read=timeout_sec,
            write=max(30.0, float(timeout_sec)),
            pool=max(30.0, float(timeout_sec)),
        )
        # Ignore process-level proxy env vars by default to avoid crashes when
        # local SOCKS proxy variables are set with unsupported schemes.
        self.client = httpx.AsyncClient(timeout=timeout, trust_env=trust_env)
        try:
            max_uploads = int(upload_max_concurrency or 0)
        except Exception:
            max_uploads = 0
        self._upload_semaphore = asyncio.Semaphore(max(1, max_uploads)) if max_uploads > 0 else None

    async def aclose(self) -> None:
        await self.client.aclose()

    def _method_url(self, method: str) -> str:
        return f"{self.api_base_url}/bot{self.token}/{method}"

    def _file_url(self, file_path: str) -> str:
        return f"{self.file_base_url}/bot{self.token}/{file_path}"

    async def get_updates(self, offset: int | None, timeout: int, allowed_updates: list[str]) -> list[dict]:
        payload = {
            "offset": offset,
            "timeout": timeout,
            "allowed_updates": allowed_updates,
        }
        response = await self._post("getUpdates", json=payload)
        if not isinstance(response, list):
            raise PlatformApiError("getUpdates response is not a list")
        return response

    async def get_file(self, file_id: str) -> dict:
        response = await self._post("getFile", json={"file_id": file_id})
        if not isinstance(response, dict):
            raise PlatformApiError("getFile response is not an object")
        return response

    async def get_chat_member(self, chat_id: str, user_id: int | str) -> dict:
        response = await self._post("getChatMember", json={"chat_id": chat_id, "user_id": int(user_id)})
        if not isinstance(response, dict):
            raise PlatformApiError("getChatMember response is not an object")
        return response

    async def send_message(self, chat_id: str, text: str, reply_markup: dict | None = None) -> dict:
        retries = 3  # initial attempt + 2 retries
        backoff_sec = 0.7
        response: object | None = None
        last_error: PlatformApiError | None = None
        payload: dict[str, object] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        parse_mode = self._pick_parse_mode(text)
        if parse_mode:
            payload["parse_mode"] = parse_mode
        for attempt in range(1, retries + 1):
            try:
                response = await self._post("sendMessage", json=payload)
                break
            except PlatformApiError as exc:
                if parse_mode and self._is_parse_entities_error(exc):
                    payload.pop("parse_mode", None)
                    parse_mode = None
                    continue
                last_error = exc
                if attempt >= retries or not self._is_transient_upload_error(exc):
                    raise
                await asyncio.sleep(backoff_sec * attempt)
        if response is None:
            assert last_error is not None
            raise last_error
        if not isinstance(response, dict):
            raise PlatformApiError("sendMessage response is not an object")
        return response

    async def delete_message(self, chat_id: str, message_id: int) -> bool:
        response = await self._post("deleteMessage", json={"chat_id": chat_id, "message_id": int(message_id)})
        return bool(response)

    async def answer_callback_query(
        self,
        callback_query_id: str,
        *,
        text: str | None = None,
        show_alert: bool = False,
    ) -> bool:
        payload: dict[str, object] = {
            "callback_query_id": callback_query_id,
            "show_alert": bool(show_alert),
        }
        if isinstance(text, str) and text.strip():
            payload["text"] = text.strip()
        response = await self._post("answerCallbackQuery", json=payload)
        return bool(response)

    async def send_photo(
        self,
        chat_id: str,
        photo_path: Path,
        caption: str | None = None,
        reply_markup: dict | None = None,
    ) -> dict:
        return await self._send_file(
            "sendPhoto",
            chat_id=chat_id,
            field_name="photo",
            file_path=photo_path,
            caption=caption,
            reply_markup=reply_markup,
        )

    async def send_video(
        self,
        chat_id: str,
        video_path: Path,
        caption: str | None = None,
        reply_markup: dict | None = None,
    ) -> dict:
        return await self._send_file(
            "sendVideo",
            chat_id=chat_id,
            field_name="video",
            file_path=video_path,
            caption=caption,
            reply_markup=reply_markup,
        )

    async def send_voice(
        self,
        chat_id: str,
        voice_path: Path,
        caption: str | None = None,
        reply_markup: dict | None = None,
    ) -> dict:
        return await self._send_file(
            "sendVoice",
            chat_id=chat_id,
            field_name="voice",
            file_path=voice_path,
            caption=caption,
            reply_markup=reply_markup,
        )

    async def send_audio(
        self,
        chat_id: str,
        audio_path: Path,
        caption: str | None = None,
        reply_markup: dict | None = None,
    ) -> dict:
        return await self._send_file(
            "sendAudio",
            chat_id=chat_id,
            field_name="audio",
            file_path=audio_path,
            caption=caption,
            reply_markup=reply_markup,
        )

    async def send_document(
        self,
        chat_id: str,
        document_path: Path,
        caption: str | None = None,
        reply_markup: dict | None = None,
    ) -> dict:
        return await self._send_file(
            "sendDocument",
            chat_id=chat_id,
            field_name="document",
            file_path=document_path,
            caption=caption,
            reply_markup=reply_markup,
        )

    async def send_animation(
        self,
        chat_id: str,
        animation_path: Path,
        caption: str | None = None,
        reply_markup: dict | None = None,
    ) -> dict:
        return await self._send_file(
            "sendAnimation",
            chat_id=chat_id,
            field_name="animation",
            file_path=animation_path,
            caption=caption,
            reply_markup=reply_markup,
        )

    async def send_sticker(self, chat_id: str, sticker_path: Path) -> dict:
        return await self._send_file(
            "sendSticker",
            chat_id=chat_id,
            field_name="sticker",
            file_path=sticker_path,
            caption=None,
        )

    async def send_video_note(self, chat_id: str, video_note_path: Path) -> dict:
        return await self._send_file(
            "sendVideoNote",
            chat_id=chat_id,
            field_name="video_note",
            file_path=video_note_path,
            caption=None,
        )

    async def send_media_group(self, chat_id: str, media_group: list[dict]) -> list[dict]:
        if len(media_group) < 2:
            raise ValueError("media_group requires at least two items")
        # Media-group delivery is not safely retryable. Upstream can accept the
        # album even when the transport/errors look failed. Never auto-resend here.
        response = await self._send_media_group_once(chat_id, media_group)
        if not isinstance(response, list):
            raise PlatformApiError("sendMediaGroup response is not a list")
        return response

    async def _send_media_group_once(self, chat_id: str, media_group: list[dict]) -> object:
        data: dict[str, str] = {"chat_id": chat_id}
        files_payload: dict[str, tuple[str, object, str]] = {}
        file_handles = []
        media_items: list[dict] = []
        try:
            for idx, item in enumerate(media_group):
                media_type = str(item.get("type") or "").strip().lower()
                if media_type not in {"photo", "video", "document", "audio"}:
                    raise ValueError(f"Unsupported media group item type: {media_type}")

                raw_path = item.get("path")
                if not isinstance(raw_path, Path):
                    raise ValueError("media group item path must be Path")
                if not raw_path.exists():
                    raise FileNotFoundError(str(raw_path))

                field_name = f"file{idx}"
                fh = raw_path.open("rb")
                file_handles.append(fh)
                mime = self._guess_upload_mime(raw_path, media_type=media_type)
                files_payload[field_name] = (raw_path.name, fh, mime)

                media_obj: dict[str, str] = {
                    "type": media_type,
                    "media": f"attach://{field_name}",
                }
                caption = item.get("caption")
                if isinstance(caption, str) and caption.strip():
                    media_obj["caption"] = caption.strip()
                    parse_mode = self._pick_parse_mode(caption)
                    if parse_mode:
                        media_obj["parse_mode"] = parse_mode
                media_items.append(media_obj)

            data["media"] = json.dumps(media_items, ensure_ascii=False)
            if self._upload_semaphore is None:
                return await self._post("sendMediaGroup", data=data, files=files_payload)
            async with self._upload_semaphore:
                return await self._post("sendMediaGroup", data=data, files=files_payload)
        finally:
            for fh in file_handles:
                fh.close()

    async def _send_file(
        self,
        method: str,
        *,
        chat_id: str,
        field_name: str,
        file_path: Path,
        caption: str | None = None,
        reply_markup: dict | None = None,
    ) -> dict:
        if not file_path.exists():
            raise FileNotFoundError(str(file_path))
        data: dict[str, str] = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
            parse_mode = self._pick_parse_mode(caption)
            if parse_mode:
                data["parse_mode"] = parse_mode
        if reply_markup is not None:
            data["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False)
        # Bale occasionally returns transient 5xx upload errors.
        # Re-open the file for each attempt and retry a few times.
        retries = 5
        backoff_sec = 0.8
        response: object | None = None
        last_error: PlatformApiError | None = None
        for attempt in range(1, retries + 1):
            try:
                with file_path.open("rb") as fh:
                    mime = self._guess_upload_mime(file_path, media_type=field_name)
                    upload_name = self._safe_upload_filename(file_path, media_type=field_name)
                    files = {field_name: (upload_name, fh, mime)}
                    if self._upload_semaphore is None:
                        response = await self._post(method, data=data, files=files)
                    else:
                        async with self._upload_semaphore:
                            response = await self._post(method, data=data, files=files)
                break
            except PlatformApiError as exc:
                if "parse_mode" in data and self._is_parse_entities_error(exc):
                    data.pop("parse_mode", None)
                    continue
                last_error = exc
                if attempt >= retries or not self._is_transient_upload_error(exc):
                    raise
                await asyncio.sleep(backoff_sec * attempt)
        if response is None:
            assert last_error is not None
            raise last_error
        if not isinstance(response, dict):
            raise PlatformApiError(f"{method} response is not an object")
        return response

    @staticmethod
    def _is_transient_upload_error(exc: PlatformApiError) -> bool:
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
    def _is_retry_safe_upload_error(exc: PlatformApiError) -> bool:
        text = str(exc).lower()
        return (
            "http 500" in text
            or "http 502" in text
            or "http 503" in text
            or "http 504" in text
            or "failed to upload file bytes" in text
        )

    @staticmethod
    def _is_parse_entities_error(exc: PlatformApiError) -> bool:
        text = str(exc).lower()
        return "can't parse entities" in text or "cannot parse entities" in text

    @classmethod
    def _has_inline_markdown_link(cls, text: str | None) -> bool:
        if not isinstance(text, str) or not text.strip():
            return False
        return bool(cls._INLINE_LINK_RE.search(text))

    @classmethod
    def _has_inline_html_link(cls, text: str | None) -> bool:
        if not isinstance(text, str) or not text.strip():
            return False
        return bool(cls._INLINE_HTML_LINK_RE.search(text))

    @classmethod
    def _pick_parse_mode(cls, text: str | None) -> str | None:
        if cls._has_inline_html_link(text):
            return "HTML"
        if cls._has_inline_markdown_link(text):
            return "Markdown"
        return None

    @staticmethod
    def _guess_upload_mime(file_path: Path, *, media_type: str) -> str:
        guessed, _ = mimetypes.guess_type(file_path.name)
        if guessed:
            return guessed
        defaults = {
            "photo": "image/jpeg",
            "video": "video/mp4",
            "audio": "audio/mpeg",
            "voice": "audio/ogg",
            "animation": "video/mp4",
            "document": "application/octet-stream",
            "video_note": "video/mp4",
        }
        return defaults.get(media_type, "application/octet-stream")

    @staticmethod
    def _safe_upload_filename(file_path: Path, *, media_type: str) -> str:
        raw_name = str(file_path.name or "").strip()
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", raw_name).strip("._")
        suffix = str(file_path.suffix or "").strip()
        if suffix and not cleaned.lower().endswith(suffix.lower()):
            cleaned = f"{cleaned}{suffix}" if cleaned else f"file{suffix}"
        if not cleaned:
            default_ext = {
                "photo": ".jpg",
                "video": ".mp4",
                "audio": ".mp3",
                "voice": ".ogg",
                "animation": ".mp4",
                "document": ".bin",
                "video_note": ".mp4",
            }.get(media_type, ".bin")
            cleaned = f"file{default_ext}"
        if len(cleaned) <= 120:
            return cleaned
        if "." not in cleaned:
            return cleaned[:120]
        stem, ext = cleaned.rsplit(".", 1)
        keep_stem = max(1, 120 - len(ext) - 1)
        return f"{stem[:keep_stem]}.{ext}"

    async def download_file(self, file_path: str, output_path: Path, max_bytes: int) -> int:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        url = self._file_url(file_path)

        written = 0
        try:
            async with self.client.stream("GET", url) as response:
                if response.status_code != 200:
                    raise PlatformApiError(f"download file failed: HTTP {response.status_code}")

                with output_path.open("wb") as fh:
                    async for chunk in response.aiter_bytes():
                        written += len(chunk)
                        if written > max_bytes:
                            fh.close()
                            output_path.unlink(missing_ok=True)
                            raise MessageTooLargeError(f"Downloaded file exceeded limit ({max_bytes} bytes)")
                        fh.write(chunk)
        except httpx.HTTPError as exc:
            output_path.unlink(missing_ok=True)
            raise PlatformApiError(f"download file network error: {exc.__class__.__name__}: {exc}") from exc

        return written

    async def _post(
        self,
        method: str,
        *,
        json: dict | None = None,
        data: dict | None = None,
        files: dict | None = None,
    ) -> object:
        try:
            response = await self.client.post(self._method_url(method), json=json, data=data, files=files)
        except httpx.HTTPError as exc:
            raise PlatformApiError(f"{method} network error: {exc.__class__.__name__}: {exc}") from exc
        if response.status_code != 200:
            raise PlatformApiError(f"{method} HTTP {response.status_code}: {response.text}")

        body = response.json()
        if not isinstance(body, dict):
            raise PlatformApiError(f"{method} invalid response body")
        if not body.get("ok"):
            description = body.get("description", "unknown API error")
            raise PlatformApiError(f"{method} API error: {description}")
        return body.get("result")
