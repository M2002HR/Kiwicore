from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from kiwi.platforms.telethon_source import TelethonSourceClient
from kiwi.types import ChannelRoute


@dataclass
class _Peer:
    channel_id: int


class _ChannelEntity:
    def __init__(self, key: str, channel_id: int = 555) -> None:
        self._key = key
        self.id = int(channel_id)
        self.broadcast = True

    def __str__(self) -> str:
        return self._key


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


class _PhotoMsg(_Msg):
    def __init__(self, message_id: int, *, grouped_id: int | None, text: str = "") -> None:
        super().__init__(message_id=message_id, text=text)
        self.grouped_id = grouped_id
        self.photo = object()
        self.file = type("F", (), {"size": 123, "name": f"{message_id}.jpg", "mime_type": "image/jpeg", "duration": None})()


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
        key = str(entity)
        if key.startswith("PeerChannel(channel_id=") and key.endswith(")"):
            try:
                cid = int(key.removeprefix("PeerChannel(channel_id=").removesuffix(")"))
            except Exception:
                cid = 555
            return _ChannelEntity(key, channel_id=cid)
        return _ChannelEntity(key, channel_id=555)

    async def get_messages(self, entity, limit=None, ids=None, min_id=None, max_id=None):
        key = str(entity)
        if ids is not None:
            return self.by_id.get((key, int(ids)))
        hist = list(self.history_by_entity.get(key, []))
        if min_id is not None:
            hist = [m for m in hist if int(m.id) > int(min_id)]
        if max_id is not None:
            hist = [m for m in hist if int(m.id) < int(max_id)]
        if limit is None:
            return list(reversed(hist))
        return list(reversed(hist))[: int(limit)]

    def iter_messages(self, entity, min_id=0, max_id=None, limit=50, reverse=False):
        key = str(entity)
        hist = [m for m in self.history_by_entity.get(key, []) if int(m.id) > int(min_id)]
        if max_id is not None:
            hist = [m for m in hist if int(m.id) < int(max_id)]
        hist.sort(key=lambda m: int(m.id), reverse=not bool(reverse))
        hist = hist[: int(limit)]

        async def _gen():
            for item in hist:
                yield item

        return _gen()


class _Dialog:
    def __init__(self, entity) -> None:
        self.entity = entity


class _DialogEntity:
    def __init__(self, channel_id: int, key: str) -> None:
        self.id = int(channel_id)
        self._key = key
        self.broadcast = True

    def __str__(self) -> str:
        return self._key


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

def test_poll_messages_can_resolve_via_source_channel_id() -> None:
    class _ChannelIdFake(_FakeTelethonClient):
        async def get_entity(self, entity):
            if str(entity) == "-100555":
                raise ValueError("not resolvable as plain numeric string")
            key = str(entity)
            if key.startswith("PeerChannel(channel_id=") and key.endswith(")"):
                return _ChannelEntity(key, channel_id=555)
            return _ChannelEntity(key, channel_id=555)

    source = TelethonSourceClient(api_id=1, api_hash="x", session_path="./tmp.session")
    fake = _ChannelIdFake()
    # Fake client stores history by stringified entity. get_entity(PeerChannel(...)) => str(PeerChannel(...)).
    fake.history_by_entity["PeerChannel(channel_id=555)"] = [_Msg(10, "x"), _Msg(11, "y")]
    source._client = fake  # noqa: SLF001
    source._ensure_connected = lambda: asyncio.sleep(0)  # type: ignore[method-assign]  # noqa: SLF001

    route = ChannelRoute(
        name="r-id",
        enabled=True,
        source_channel_id="-100555",
        source_channel_username=None,
        destination_channel_id="-2001",
        destination_channel_username=None,
        channel_script="default_channel_script.py",
        max_message_mb=50,
        sync_enabled=True,
        sync_status="syncing",
        sync_seeded=True,
    )

    first = asyncio.run(source.poll_messages([route]))
    assert first == []
    fake.history_by_entity["PeerChannel(channel_id=555)"].append(_Msg(12, "z"))
    second = asyncio.run(source.poll_messages([route]))
    assert [item.message_id for item in second] == [12]


def test_poll_messages_resolves_channel_id_via_dialog_scan_when_get_entity_fails() -> None:
    class _DialogFallbackFake(_FakeTelethonClient):
        def __init__(self) -> None:
            super().__init__()
            self._entity = _DialogEntity(555, "dialog_entity_555")

        async def get_entity(self, entity):
            raise ValueError(f"not resolvable: {entity}")

        def iter_dialogs(self, limit=1000, archived=None):  # noqa: ARG002
            async def _gen():
                yield _Dialog(self._entity)

            return _gen()

    source = TelethonSourceClient(api_id=1, api_hash="x", session_path="./tmp.session")
    fake = _DialogFallbackFake()
    fake.history_by_entity["dialog_entity_555"] = [_Msg(10, "x"), _Msg(11, "y")]
    source._client = fake  # noqa: SLF001
    source._ensure_connected = lambda: asyncio.sleep(0)  # type: ignore[method-assign]  # noqa: SLF001

    route = ChannelRoute(
        name="r-id-dialog",
        enabled=True,
        source_channel_id="-100555",
        source_channel_username=None,
        destination_channel_id="-2001",
        destination_channel_username=None,
        channel_script="default_channel_script.py",
        max_message_mb=50,
        sync_enabled=True,
        sync_status="syncing",
        sync_seeded=True,
    )

    first = asyncio.run(source.poll_messages([route]))
    assert first == []
    fake.history_by_entity["dialog_entity_555"].append(_Msg(12, "z"))
    second = asyncio.run(source.poll_messages([route]))
    assert [item.message_id for item in second] == [12]


def test_latest_message_id_reports_standard_unresolvable_entity_error() -> None:
    class _UnresolvableFake(_FakeTelethonClient):
        async def get_entity(self, entity):
            raise ValueError(f"Could not find the input entity for {entity}")

    source = TelethonSourceClient(api_id=1, api_hash="x", session_path="./tmp.session")
    source._client = _UnresolvableFake()  # noqa: SLF001
    source._ensure_connected = lambda: asyncio.sleep(0)  # type: ignore[method-assign]  # noqa: SLF001

    route = ChannelRoute(
        name="r-unresolvable",
        enabled=True,
        source_channel_id="-100777",
        source_channel_username=None,
        destination_channel_id="-2001",
        destination_channel_username=None,
        channel_script="default_channel_script.py",
        max_message_mb=50,
        sync_enabled=True,
        sync_status="syncing",
        sync_seeded=True,
    )

    try:
        asyncio.run(source.latest_message_id_for_route(route))
        raise AssertionError("expected ValueError")
    except ValueError as exc:
        assert "Unable to resolve source entity" in str(exc)


def test_expand_media_group_merges_all_group_members_into_single_incoming() -> None:
    source = TelethonSourceClient(api_id=1, api_hash="x", session_path="./tmp.session")
    fake = _FakeTelethonClient()
    grouped_id = 14005268120515820
    fake.history_by_entity["@stored_src"] = [
        _PhotoMsg(7218, grouped_id=None),
        _PhotoMsg(7219, grouped_id=grouped_id),
        _PhotoMsg(7220, grouped_id=grouped_id),
        _PhotoMsg(7221, grouped_id=grouped_id),
        _PhotoMsg(7222, grouped_id=grouped_id, text="album caption"),
        _PhotoMsg(7223, grouped_id=None),
    ]
    source._client = fake  # noqa: SLF001
    source._ensure_connected = lambda: asyncio.sleep(0)  # type: ignore[method-assign]  # noqa: SLF001

    route = _route()
    incoming = asyncio.run(source._to_incoming(fake.history_by_entity["@stored_src"][2], source_key="@stored_src", source_username=route.source_channel_username))  # noqa: SLF001
    assert incoming is not None
    assert incoming.message_id == 7220
    assert len(incoming.medias) == 1

    expanded = asyncio.run(source.expand_media_group(incoming, source_username=route.source_channel_username))
    assert expanded.message_id == 7222
    assert expanded.update_id == 7222
    assert expanded.media_group_id == str(grouped_id)
    assert len(expanded.medias) == 4
    assert expanded.caption == "album caption"


def test_seed_recent_messages_collapses_album_members_before_enqueue() -> None:
    source = TelethonSourceClient(api_id=1, api_hash="x", session_path="./tmp.session")
    fake = _FakeTelethonClient()
    grouped_id = 14078066505163477
    fake.history_by_entity["@stored_src"] = [
        _PhotoMsg(7650, grouped_id=grouped_id, text="album caption"),
        _PhotoMsg(7651, grouped_id=grouped_id),
        _PhotoMsg(7652, grouped_id=grouped_id),
        _PhotoMsg(7653, grouped_id=grouped_id),
        _PhotoMsg(7654, grouped_id=grouped_id),
        _PhotoMsg(7655, grouped_id=grouped_id),
        _PhotoMsg(7656, grouped_id=grouped_id),
        _PhotoMsg(7657, grouped_id=grouped_id),
    ]
    source._client = fake  # noqa: SLF001
    source._ensure_connected = lambda: asyncio.sleep(0)  # type: ignore[method-assign]  # noqa: SLF001

    route = _route()
    out = asyncio.run(source.seed_recent_messages(route, limit=8))
    assert len(out) == 1
    merged = out[0]
    assert merged.media_group_id == str(grouped_id)
    assert merged.message_id == 7657
    assert merged.update_id == 7657
    assert len(merged.medias) == 8
