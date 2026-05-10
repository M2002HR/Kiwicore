from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest

from kiwi.errors import PlatformApiError
from kiwi.platforms.client import BotApiClient


class RaisingHttpClient:
    async def post(self, *args, **kwargs):
        raise httpx.ConnectTimeout("timeout")

    async def aclose(self):
        return None


class SequencedHttpClient:
    def __init__(self, responses):
        self.responses = responses
        self.calls = 0

    async def post(self, *args, **kwargs):
        value = self.responses[self.calls]
        self.calls += 1
        if isinstance(value, Exception):
            raise value
        return value

    async def aclose(self):
        return None


def _ok_response(result: dict) -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": result})


def _http_error_response(code: int, description: str) -> httpx.Response:
    return httpx.Response(code, text=description)


def test_client_wraps_httpx_error_as_platform_error() -> None:
    client = BotApiClient(
        token="t",
        api_base_url="https://api.telegram.org",
        file_base_url="https://api.telegram.org/file",
    )
    client.client = RaisingHttpClient()

    with pytest.raises(PlatformApiError) as exc_info:
        asyncio.run(client.get_updates(offset=None, timeout=1, allowed_updates=[]))

    assert "network error" in str(exc_info.value)


def test_send_message_retries_on_transient_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(*args, **kwargs):
        return None

    monkeypatch.setattr("kiwi.platforms.client.asyncio.sleep", no_sleep)
    client = BotApiClient(
        token="t",
        api_base_url="https://api.telegram.org",
        file_base_url="https://api.telegram.org/file",
    )
    client.client = SequencedHttpClient(
        [
            httpx.ConnectTimeout("timeout"),
            _ok_response({"message_id": 9}),
        ]
    )
    result = asyncio.run(client.send_message("@dest", "hi"))
    assert result == {"message_id": 9}
    assert client.client.calls == 2


def test_send_file_retries_on_transient_upload_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(*args, **kwargs):
        return None

    monkeypatch.setattr("kiwi.platforms.client.asyncio.sleep", no_sleep)

    client = BotApiClient(
        token="t",
        api_base_url="https://api.telegram.org",
        file_base_url="https://api.telegram.org/file",
    )
    client.client = SequencedHttpClient(
        [
            _http_error_response(500, "Internal Error: failed to upload file bytes"),
            _ok_response({"message_id": 123}),
        ]
    )
    photo_path = tmp_path / "x.jpg"
    photo_path.write_bytes(b"abc")

    result = asyncio.run(client.send_photo("@dest", photo_path, caption="cap"))

    assert result == {"message_id": 123}
    assert client.client.calls == 2


def test_send_file_no_retry_on_non_transient_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(*args, **kwargs):
        return None

    monkeypatch.setattr("kiwi.platforms.client.asyncio.sleep", no_sleep)

    client = BotApiClient(
        token="t",
        api_base_url="https://api.telegram.org",
        file_base_url="https://api.telegram.org/file",
    )
    client.client = SequencedHttpClient([_http_error_response(400, "Bad Request")])
    doc_path = tmp_path / "x.txt"
    doc_path.write_bytes(b"abc")

    with pytest.raises(PlatformApiError):
        asyncio.run(client.send_document("@dest", doc_path))

    assert client.client.calls == 1


def test_send_media_group_retries_on_transient_upload_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(*args, **kwargs):
        return None

    monkeypatch.setattr("kiwi.platforms.client.asyncio.sleep", no_sleep)

    client = BotApiClient(
        token="t",
        api_base_url="https://api.telegram.org",
        file_base_url="https://api.telegram.org/file",
    )
    client.client = SequencedHttpClient(
        [
            _http_error_response(500, "Internal Error: failed to upload file bytes"),
            _ok_response([{"message_id": 11}, {"message_id": 12}]),
        ]
    )
    p1 = tmp_path / "a.mp3"
    p2 = tmp_path / "b.mp3"
    p1.write_bytes(b"a")
    p2.write_bytes(b"b")

    result = asyncio.run(
        client.send_media_group(
            "@dest",
            [
                {"type": "audio", "path": p1, "caption": "cap"},
                {"type": "audio", "path": p2},
            ],
        )
    )
    assert isinstance(result, list)
    assert client.client.calls == 2
