from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from kiwi.errors import MessageTooLargeError, PlatformApiError


class BotApiClient:
    def __init__(
        self,
        *,
        token: str,
        api_base_url: str,
        file_base_url: str,
        timeout_sec: float = 40.0,
        trust_env: bool = False,
    ) -> None:
        self.token = token
        self.api_base_url = api_base_url.rstrip("/")
        self.file_base_url = file_base_url.rstrip("/")
        timeout = httpx.Timeout(connect=15.0, read=timeout_sec, write=30.0, pool=30.0)
        # Ignore process-level proxy env vars by default to avoid crashes when
        # local SOCKS proxy variables are set with unsupported schemes.
        self.client = httpx.AsyncClient(timeout=timeout, trust_env=trust_env)

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

    async def send_message(self, chat_id: str, text: str) -> dict:
        response = await self._post("sendMessage", json={"chat_id": chat_id, "text": text})
        if not isinstance(response, dict):
            raise PlatformApiError("sendMessage response is not an object")
        return response

    async def send_photo(self, chat_id: str, photo_path: Path, caption: str | None = None) -> dict:
        return await self._send_file("sendPhoto", chat_id=chat_id, field_name="photo", file_path=photo_path, caption=caption)

    async def send_video(self, chat_id: str, video_path: Path, caption: str | None = None) -> dict:
        return await self._send_file("sendVideo", chat_id=chat_id, field_name="video", file_path=video_path, caption=caption)

    async def send_voice(self, chat_id: str, voice_path: Path, caption: str | None = None) -> dict:
        return await self._send_file("sendVoice", chat_id=chat_id, field_name="voice", file_path=voice_path, caption=caption)

    async def send_audio(self, chat_id: str, audio_path: Path, caption: str | None = None) -> dict:
        return await self._send_file("sendAudio", chat_id=chat_id, field_name="audio", file_path=audio_path, caption=caption)

    async def send_document(self, chat_id: str, document_path: Path, caption: str | None = None) -> dict:
        return await self._send_file(
            "sendDocument",
            chat_id=chat_id,
            field_name="document",
            file_path=document_path,
            caption=caption,
        )

    async def send_animation(self, chat_id: str, animation_path: Path, caption: str | None = None) -> dict:
        return await self._send_file(
            "sendAnimation",
            chat_id=chat_id,
            field_name="animation",
            file_path=animation_path,
            caption=caption,
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

    async def _send_file(
        self,
        method: str,
        *,
        chat_id: str,
        field_name: str,
        file_path: Path,
        caption: str | None = None,
    ) -> dict:
        if not file_path.exists():
            raise FileNotFoundError(str(file_path))
        data: dict[str, str] = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        # Bale occasionally returns transient 5xx upload errors.
        # Re-open the file for each attempt and retry a few times.
        retries = 3
        backoff_sec = 0.6
        response: object | None = None
        last_error: PlatformApiError | None = None
        for attempt in range(1, retries + 1):
            try:
                with file_path.open("rb") as fh:
                    files = {field_name: (file_path.name, fh, "application/octet-stream")}
                    response = await self._post(method, data=data, files=files)
                break
            except PlatformApiError as exc:
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
            or "http 500" in text
            or "http 502" in text
            or "http 503" in text
            or "http 504" in text
            or "failed to upload file bytes" in text
        )

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
