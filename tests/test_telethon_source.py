from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from kiwi.platforms.telethon_source import TelethonSourceClient
from kiwi.types import ChannelRoute


@dataclass
class _Peer:
    channel_id: int


class _Msg:
    def __init__(self, message_id: int, text: str = "") -> None:
        self.id = message_id
        self.message = text
        self.peer_id = _Peer(channel_id=555)
        self.file = None
        self.photo = None
        self.video = None
        self.voice = None
        self.audio = None
        self.gif = None
        self.animation = None
        self.sticker = None
        self.video_note = None
        self.document = None
        self.grouped_id = None
        self.date = None


class _DownloadMsg(_Msg):
    def __init__(self, message_id: int, *, payload: bytes) -> None:
        super().__init__(message_id=message_id, text="")
        self.file = type("F", (), {"size": len(payload)})()
        self._payload = payload

    async def download_media(self, file: str):
        out = Path(f"{file}.jpg")
        out.write_bytes(self._payload)
        return str(out)


class _FakeTelethonClient:
    def __init__(self) -> None:
        self.history_by_entity: dict[str, list[_Msg]] = {}
        self.by_id: dict[tuple[str, int], _Msg] = {}

    async def get_entity(self, entity):
        return str(entity)

    async def get_messages(self, entity, limit=None, ids=None):
        key = str(entity)
        if ids is not None:
            return self.by_id.get((key, int(ids)))
        hist = list(self.history_by_entity.get(key, []))
        if limit is None:
            return list(reversed(hist))
        return list(reversed(hist))[: int(limit)]

    def iter_messages(self, entity, min_id=0, limit=50, reverse=False):
        key = str(entity)
        hist = [m for m in self.history_by_entity.get(key, []) if int(m.id) > int(min_id)]
        hist.sort(key=lambda m: int(m.id), reverse=not bool(reverse))
        hist = hist[: int(limit)]

        async def _gen():
            for item in hist:
                yield item

        return _gen()


def _route() -> ChannelRoute:
    return ChannelRoute(
        name="r1",
        enabled=True,
        source_channel_id=None,
        source_channel_username="@stored_src",
        destination_channel_id="-2001",
        destination_channel_username=None,
        channel_script="default_channel_script.py",
        max_message_mb=50,
    )


def test_poll_messages_skips_old_history_on_first_cursor_init() -> None:
    source = TelethonSourceClient(api_id=1, api_hash="x", session_path="./tmp.session")
    fake = _FakeTelethonClient()
    fake.history_by_entity["@stored_src"] = [_Msg(1, "a"), _Msg(2, "b"), _Msg(3, "c")]
    source._client = fake  # noqa: SLF001
    source._ensure_connected = lambda: asyncio.sleep(0)  # type: ignore[method-assign]  # noqa: SLF001

    route = _route()
    first = asyncio.run(source.poll_messages([route]))
    assert first == []

    fake.history_by_entity["@stored_src"].append(_Msg(4, "d"))
    second = asyncio.run(source.poll_messages([route]))
    assert [item.message_id for item in second] == [4]


def test_seed_recent_messages_uses_last_n_and_keeps_cursor() -> None:
    source = TelethonSourceClient(api_id=1, api_hash="x", session_path="./tmp.session")
    fake = _FakeTelethonClient()
    fake.history_by_entity["@stored_src"] = [_Msg(1, "a"), _Msg(2, "b"), _Msg(3, "c"), _Msg(4, "d"), _Msg(5, "e")]
    source._client = fake  # noqa: SLF001
    source._ensure_connected = lambda: asyncio.sleep(0)  # type: ignore[method-assign]  # noqa: SLF001

    route = _route()
    seeded = asyncio.run(source.seed_recent_messages(route, 2))
    assert [item.message_id for item in seeded] == [4, 5]

    fake.history_by_entity["@stored_src"].append(_Msg(6, "f"))
    polled = asyncio.run(source.poll_messages([route]))
    assert [item.message_id for item in polled] == [6]


def test_download_media_accepts_telethon_generated_filename(tmp_path: Path) -> None:
    source = TelethonSourceClient(api_id=1, api_hash="x", session_path="./tmp.session")
    fake = _FakeTelethonClient()
    msg = _DownloadMsg(77, payload=b"abc123")
    fake.by_id[("@stored_src", 77)] = msg
    source._client = fake  # noqa: SLF001
    source._ensure_connected = lambda: asyncio.sleep(0)  # type: ignore[method-assign]  # noqa: SLF001
    source._entity_cache["@stored_src"] = "@stored_src"  # noqa: SLF001

    out = tmp_path / "photo_1"
    size = asyncio.run(source.download_media({"source_key": "@stored_src", "message_id": 77}, out, max_bytes=100))
    assert size == 6
    assert out.exists()
    assert out.read_bytes() == b"abc123"
