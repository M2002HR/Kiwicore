from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

from kiwi.admin_bot import AdminBotHandler
from kiwi.admin_store import AdminStore
from kiwi.config import RouteRegistry, Settings, load_routes
from kiwi.errors import MessageTooLargeError, PlatformApiError
from kiwi.guard_runner import GuardRunner
from kiwi.management_api import ManagementApi
from kiwi.script_runner import ScriptRunner
from kiwi.service import KiwiService
from kiwi.state import StateStore
from kiwi.storage import StorageManager
from kiwi.types import ChannelRoute, IncomingChannelMessage, IncomingMedia, MediaKind


class FakeTelegramClient:
    def __init__(self, updates: list[dict], file_bytes: bytes) -> None:
        self._updates = updates
        self._done = False
        self.file_bytes = file_bytes
        self.get_file_calls = 0
        self.audit_messages: list[tuple[str, str]] = []

    async def get_updates(self, offset, timeout, allowed_updates):
        if self._done:
            return []
        self._done = True
        return self._updates

    async def get_file(self, file_id: str) -> dict:
        self.get_file_calls += 1
        return {"file_path": f"files/{file_id}.bin"}

    async def download_file(self, file_path: str, output_path: Path, max_bytes: int) -> int:
        if len(self.file_bytes) > max_bytes:
            raise MessageTooLargeError("too large")
        output_path.write_bytes(self.file_bytes)
        return len(self.file_bytes)

    async def send_message(self, chat_id: str, text: str, reply_markup: dict | None = None):
        self.audit_messages.append((chat_id, text))
        return {"ok": True}

    async def aclose(self) -> None:
        return None


class FakeTelegramSequenceClient(FakeTelegramClient):
    def __init__(self, update_batches: list[list[dict]], file_bytes: bytes) -> None:
        super().__init__([], file_bytes)
        self._batches = list(update_batches)

    async def get_updates(self, offset, timeout, allowed_updates):
        if not self._batches:
            return []
        return list(self._batches.pop(0))


class MissingSourceFileTelegramClient(FakeTelegramClient):
    async def download_file(self, file_path: str, output_path: Path, max_bytes: int) -> int:  # noqa: ARG002
        raise FileNotFoundError(file_path)


class FakeBaleClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.media_group_sent: list[tuple[str, int]] = []

    async def send_message(self, chat_id: str, text: str, reply_markup: dict | None = None):
        self.sent.append((chat_id, text))
        return {"ok": True}

    async def send_photo(self, chat_id: str, photo_path: Path, caption: str | None = None):
        self.sent.append((chat_id, f"photo:{photo_path.name}"))
        return {"ok": True}

    async def send_video(self, chat_id: str, video_path: Path, caption: str | None = None):
        self.sent.append((chat_id, f"video:{video_path.name}"))
        return {"ok": True}

    async def send_voice(self, chat_id: str, voice_path: Path, caption: str | None = None):
        self.sent.append((chat_id, f"voice:{voice_path.name}"))
        return {"ok": True}

    async def send_audio(self, chat_id: str, audio_path: Path, caption: str | None = None):
        self.sent.append((chat_id, f"audio:{audio_path.name}"))
        return {"ok": True}

    async def send_document(self, chat_id: str, document_path: Path, caption: str | None = None):
        self.sent.append((chat_id, f"doc:{document_path.name}"))
        return {"ok": True}

    async def send_animation(self, chat_id: str, animation_path: Path, caption: str | None = None):
        self.sent.append((chat_id, f"animation:{animation_path.name}"))
        return {"ok": True}

    async def send_video_note(self, chat_id: str, video_note_path: Path):
        self.sent.append((chat_id, f"video_note:{video_note_path.name}"))
        return {"ok": True}

    async def send_media_group(self, chat_id: str, media_group: list[dict]):
        self.media_group_sent.append((chat_id, len(media_group)))
        return [{"ok": True}]

    async def aclose(self) -> None:
        return None


class FakeTelethonSourceClient:
    def __init__(
        self,
        messages: list[IncomingChannelMessage] | None = None,
        seeded_messages: list[IncomingChannelMessage] | None = None,
    ) -> None:
        self.messages = list(messages or [])
        self.seeded_messages = list(seeded_messages or [])
        self.seed_calls: list[tuple[str, int]] = []
        self.primed_cursors: dict[str, int] = {}
        self.closed = False

    async def poll_messages(self, routes: list[ChannelRoute]) -> list[IncomingChannelMessage]:
        out = list(self.messages)
        self.messages.clear()
        return out

    async def seed_recent_messages(self, route: ChannelRoute, limit: int) -> list[IncomingChannelMessage]:
        self.seed_calls.append((route.name, int(limit)))
        out = list(self.seeded_messages)[: max(0, int(limit))]
        self.seeded_messages.clear()
        return out

    async def latest_message_id_for_route(self, route: ChannelRoute) -> int:
        candidates = list(self.messages) + list(self.seeded_messages)
        if not candidates:
            return 0
        return max(int(item.message_id) for item in candidates)

    def route_source_key(self, route: ChannelRoute) -> str | None:
        return route.source_channel_username or route.source_channel_id

    def prime_cursor(self, source_key: str, message_id: int) -> None:
        key = str(source_key or "").strip()
        if not key:
            return
        value = max(0, int(message_id))
        current = self.primed_cursors.get(key)
        if current is None:
            self.primed_cursors[key] = value
            return
        self.primed_cursors[key] = min(current, value)

    async def download_media(self, source_ref: dict, output_path: Path, max_bytes: int) -> int:
        data = b"telethon-media"
        if len(data) > max_bytes:
            raise MessageTooLargeError("too large")
        output_path.write_bytes(data)
        return len(data)

    async def aclose(self) -> None:
        self.closed = True


def _settings(tmp_path: Path, default_max_mb: int = 50) -> Settings:
    gaurd_dir = tmp_path / "gaurds"
    gaurd_dir.mkdir(parents=True, exist_ok=True)
    (gaurd_dir / "default_guard.py").write_text("print('true')", encoding="utf-8")

    return Settings(
        app_env="test",
        log_level="INFO",
        log_format="plain",
        tz="Asia/Tehran",
        telegram_bot_token="t",
        telegram_api_base_url="https://api.telegram.org",
        telegram_file_base_url="https://api.telegram.org/file",
        telegram_poll_timeout_sec=1,
        telegram_allowed_updates=["channel_post"],
        telegram_source_mode="bot",
        bale_bot_token="b",
        bale_api_base_url="https://tapi.bale.ai",
        bale_file_base_url="https://tapi.bale.ai/file",
        http_trust_env=False,
        channels_config_path=str(tmp_path / "channels.json"),
        scripts_dir=str(tmp_path / "scripts"),
        gaurd_scripts_dir=str(gaurd_dir),
        storage_dir=str(tmp_path / "storage"),
        state_path=str(tmp_path / "storage" / "state.json"),
        default_max_message_mb=default_max_mb,
        script_timeout_sec=5,
        gaurd_script_timeout_sec=5,
        poll_idle_sleep_sec=0.01,
        poll_error_sleep_sec=0.01,
        log_channel_target=None,
        media_group_wait_sec=0.3,
        admin_bot_enabled=True,
        admin_users_config_path=str(tmp_path / "config" / "admin_users.json"),
        admin_sessions_path=str(tmp_path / "storage" / "admin_sessions.json"),
        telethon_enabled=False,
        telethon_api_id=None,
        telethon_api_hash="",
        telethon_session_path=str(tmp_path / "storage" / "telethon.session"),
        telethon_poll_batch_size=50,
        telethon_proxy_url="",
    )


def _route_registry(route: ChannelRoute | list[ChannelRoute]) -> RouteRegistry:
    routes = route if isinstance(route, list) else [route]
    by_id: dict[str, list[ChannelRoute]] = {}
    by_username: dict[str, list[ChannelRoute]] = {}
    for item in routes:
        if item.source_channel_id:
            by_id.setdefault(item.source_channel_id, []).append(item)
        if item.source_channel_username:
            by_username.setdefault(item.source_channel_username, []).append(item)
    return RouteRegistry(routes=routes, by_channel_id=by_id, by_channel_username=by_username)


def test_service_sync_initial_due_is_jittered_and_spaced(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    route = ChannelRoute(
        name="sync-jitter-route",
        enabled=False,
        source_channel_id="-1001",
        source_channel_username="@srcchan",
        destination_channel_id="-2001",
        destination_channel_username=None,
        channel_script=None,
        max_message_mb=10,
        sync_enabled=True,
        sync_status="syncing",
        sync_interval_sec=30,
    )
    service = KiwiService(
        settings=settings,
        routes=_route_registry(route),
        telegram_client=FakeTelegramClient([], b""),
        bale_client=FakeBaleClient(),
        storage=StorageManager(settings.storage_dir),
        guard_runner=GuardRunner(settings.gaurd_scripts_dir, timeout_sec=5),
        script_runner=ScriptRunner(settings.scripts_dir, timeout_sec=5),
        state_store=StateStore(settings.state_path),
    )

    base_now = 1_000.0
    expected_jitter = service._initial_sync_jitter_sec(route.name, route.sync_interval_sec)  # noqa: SLF001
    with patch("kiwi.service.time.time", return_value=base_now):
        first_due = service._reserve_sync_due_at(route.name, route.sync_interval_sec)  # noqa: SLF001
        second_due = service._reserve_sync_due_at(route.name, route.sync_interval_sec)  # noqa: SLF001

    assert first_due == base_now
    assert second_due == first_due + route.sync_interval_sec + expected_jitter


def test_service_sync_jitter_varies_across_routes() -> None:
    interval_sec = 60
    values = {
        KiwiService._initial_sync_jitter_sec(f"route-{idx}", interval_sec)  # noqa: SLF001
        for idx in range(1, 16)
    }
    assert all(0 <= value < interval_sec for value in values)
    assert len(values) > 1


def test_service_sync_primes_source_cursor_from_route_checkpoint(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    route1 = ChannelRoute(
        name="sync-r1",
        enabled=False,
        source_channel_id="-1001",
        source_channel_username="@shared_src",
        destination_channel_id="-2001",
        destination_channel_username=None,
        channel_script=None,
        max_message_mb=10,
        sync_enabled=True,
        sync_status="syncing",
        sync_seeded=False,
    )
    route2 = ChannelRoute(
        name="sync-r2",
        enabled=False,
        source_channel_id="-1001",
        source_channel_username="@shared_src",
        destination_channel_id="-2002",
        destination_channel_username=None,
        channel_script=None,
        max_message_mb=10,
        sync_enabled=True,
        sync_status="syncing",
        sync_seeded=False,
    )
    source = FakeTelethonSourceClient()
    service = KiwiService(
        settings=settings,
        routes=_route_registry([route1, route2]),
        telegram_client=FakeTelegramClient([], b""),
        bale_client=FakeBaleClient(),
        source_client=source,
        storage=StorageManager(settings.storage_dir),
        guard_runner=GuardRunner(settings.gaurd_scripts_dir, timeout_sec=5),
        script_runner=ScriptRunner(settings.scripts_dir, timeout_sec=5),
        state_store=StateStore(settings.state_path),
    )
    service.sync_ledger.set_route_checkpoint(route1.name, 120)  # noqa: SLF001
    service.sync_ledger.set_route_checkpoint(route2.name, 90)  # noqa: SLF001

    asyncio.run(service._ensure_sync_baseline())  # noqa: SLF001
    assert source.primed_cursors["@shared_src"] == 90


def test_service_sync_baseline_uses_backfill_window_from_source_latest(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    route = ChannelRoute(
        name="sync-window",
        enabled=False,
        source_channel_id="-1001",
        source_channel_username="@srcchan",
        destination_channel_id="-2001",
        destination_channel_username=None,
        channel_script=None,
        max_message_mb=10,
        sync_enabled=True,
        sync_status="syncing",
        sync_backfill_count=2,
        sync_seeded=False,
    )
    source = FakeTelethonSourceClient(
        messages=[],
        seeded_messages=[
            IncomingChannelMessage(
                update_id=100,
                source_channel_id="-1001",
                source_channel_username="@srcchan",
                message_id=100,
                date=None,
                text="x",
                caption=None,
                medias=[],
                raw={"telethon": True},
                media_group_id=None,
            )
        ],
    )
    service = KiwiService(
        settings=settings,
        routes=_route_registry(route),
        telegram_client=FakeTelegramClient([], b""),
        bale_client=FakeBaleClient(),
        source_client=source,
        storage=StorageManager(settings.storage_dir),
        guard_runner=GuardRunner(settings.gaurd_scripts_dir, timeout_sec=5),
        script_runner=ScriptRunner(settings.scripts_dir, timeout_sec=5),
        state_store=StateStore(settings.state_path),
    )

    asyncio.run(service._ensure_sync_baseline())  # noqa: SLF001
    checkpoint = service.sync_ledger.get_route_checkpoint(route.name)  # noqa: SLF001
    assert checkpoint == 98


def test_service_sync_skips_queued_record_older_than_checkpoint(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    route = ChannelRoute(
        name="sync-stale",
        enabled=False,
        source_channel_id="-1001",
        source_channel_username="@srcchan",
        destination_channel_id="-2001",
        destination_channel_username=None,
        channel_script=None,
        max_message_mb=10,
        sync_enabled=True,
        sync_status="syncing",
        sync_seeded=True,
    )
    service = KiwiService(
        settings=settings,
        routes=_route_registry(route),
        telegram_client=FakeTelegramClient([], b""),
        bale_client=FakeBaleClient(),
        storage=StorageManager(settings.storage_dir),
        guard_runner=GuardRunner(settings.gaurd_scripts_dir, timeout_sec=5),
        script_runner=ScriptRunner(settings.scripts_dir, timeout_sec=5),
        state_store=StateStore(settings.state_path),
    )
    incoming = IncomingChannelMessage(
        update_id=5,
        source_channel_id="-1001",
        source_channel_username="@srcchan",
        message_id=5,
        date=None,
        text="old",
        caption=None,
        medias=[],
        raw={},
        media_group_id=None,
    )
    asyncio.run(service._enqueue_sync_message(route, incoming))  # noqa: SLF001
    service.sync_ledger.set_route_checkpoint(route.name, 10)  # noqa: SLF001

    processed = asyncio.run(service._drain_sync_queue())  # noqa: SLF001
    assert processed == 1
    key = service.sync_ledger.dedupe_key(route.name, incoming.source_channel_id, incoming.message_id, None)  # noqa: SLF001
    record = service.sync_ledger.get_record(key)  # noqa: SLF001
    assert record is not None
    assert record.status == "skipped"


def test_service_flow_dispatches_script_output(tmp_path: Path) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    script_path = scripts_dir / "-1001.py"
    script_path.write_text(
        """
import json
import argparse
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('--payload', required=True)
p.add_argument('--input-dir', required=True)
p.add_argument('--output-dir', required=True)
a = p.parse_args()

payload = json.loads(Path(a.payload).read_text(encoding='utf-8'))
text = payload['message'].get('text') or ''
print(json.dumps({'messages': [{'type': 'text', 'text': 'OUT:' + text}]}))
""".strip(),
        encoding="utf-8",
    )

    update = {
        "update_id": 101,
        "channel_post": {
            "message_id": 8,
            "chat": {"id": -1001, "type": "channel", "username": "srcchan"},
            "text": "hello",
            "document": {"file_id": "f1", "file_size": 5, "file_name": "a.txt"},
        },
    }

    settings = _settings(tmp_path)
    route = ChannelRoute(
        name="r",
        enabled=True,
        source_channel_id="-1001",
        source_channel_username="@srcchan",
        destination_channel_id="-2001",
        destination_channel_username=None,
        channel_script="-1001.py",
        max_message_mb=10,
    )

    tg = FakeTelegramClient([update], b"12345")
    bale = FakeBaleClient()

    service = KiwiService(
        settings=settings,
        routes=_route_registry(route),
        telegram_client=tg,
        bale_client=bale,
        storage=StorageManager(settings.storage_dir),
        guard_runner=GuardRunner(settings.gaurd_scripts_dir, timeout_sec=5),
        script_runner=ScriptRunner(settings.scripts_dir, timeout_sec=5),
        state_store=StateStore(settings.state_path),
    )

    processed = asyncio.run(service.run_once())
    assert processed == 1
    assert bale.sent == [("-2001", "OUT:hello\n-2001")]


def test_service_applies_keyword_links_after_channel_script(tmp_path: Path) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "emit_text.py").write_text(
        """
import json
print(json.dumps({"messages":[{"type":"text","text":"Barcelona و بارسا آماده‌اند."}]}))
""".strip(),
        encoding="utf-8",
    )

    (tmp_path / "keyword_links.json").write_text(
        json.dumps(
            [
                {
                    "destination": "@barcelona_fa",
                    "link": "https://ble.ir/barcelona_fa",
                    "keywords": ["Barcelona", "بارسا"],
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    update = {
        "update_id": 112,
        "channel_post": {
            "message_id": 12,
            "chat": {"id": -1001, "type": "channel"},
            "text": "x",
        },
    }

    settings = _settings(tmp_path)
    route = ChannelRoute(
        name="r-link",
        enabled=True,
        source_channel_id="-1001",
        source_channel_username=None,
        destination_channel_id=None,
        destination_channel_username="@barcelona_fa",
        channel_script="emit_text.py",
        max_message_mb=10,
    )
    tg = FakeTelegramClient([update], b"")
    bale = FakeBaleClient()
    service = KiwiService(
        settings=settings,
        routes=_route_registry(route),
        telegram_client=tg,
        bale_client=bale,
        storage=StorageManager(settings.storage_dir),
        guard_runner=GuardRunner(settings.gaurd_scripts_dir, timeout_sec=5),
        script_runner=ScriptRunner(settings.scripts_dir, timeout_sec=5),
        state_store=StateStore(settings.state_path),
    )

    processed = asyncio.run(service.run_once())
    assert processed == 1
    assert bale.sent == [
        (
            "@barcelona_fa",
            "[Barcelona](https://ble.ir/barcelona_fa) و [بارسا](https://ble.ir/barcelona_fa) آماده‌اند.\n@barcelona_fa",
        )
    ]


def test_service_applies_global_keyword_links_for_multiple_channels(tmp_path: Path) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "emit_text.py").write_text(
        """
import json
print(json.dumps({"messages":[{"type":"text","text":"لامین یامال و نیمار در این آمار کنار هم هستند."}]}))
""".strip(),
        encoding="utf-8",
    )

    (tmp_path / "keyword_links.json").write_text(
        json.dumps(
            [
                {
                    "destination": "@lamine_yamal_official",
                    "link": "https://ble.ir/lamine_yamal_official",
                    "keywords": ["لامین یامال"],
                },
                {
                    "destination": "@neymar_fa",
                    "link": "https://ble.ir/neymar_fa",
                    "keywords": ["نیمار"],
                },
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    update = {
        "update_id": 113,
        "channel_post": {
            "message_id": 13,
            "chat": {"id": -1001, "type": "channel"},
            "text": "x",
        },
    }

    settings = _settings(tmp_path)
    route = ChannelRoute(
        name="r-link-global",
        enabled=True,
        source_channel_id="-1001",
        source_channel_username=None,
        destination_channel_id=None,
        destination_channel_username="@lamine_yamal_official",
        channel_script="emit_text.py",
        max_message_mb=10,
    )
    tg = FakeTelegramClient([update], b"")
    bale = FakeBaleClient()
    service = KiwiService(
        settings=settings,
        routes=_route_registry(route),
        telegram_client=tg,
        bale_client=bale,
        storage=StorageManager(settings.storage_dir),
        guard_runner=GuardRunner(settings.gaurd_scripts_dir, timeout_sec=5),
        script_runner=ScriptRunner(settings.scripts_dir, timeout_sec=5),
        state_store=StateStore(settings.state_path),
    )

    processed = asyncio.run(service.run_once())
    assert processed == 1
    assert bale.sent == [
        (
            "@lamine_yamal_official",
            "[لامین یامال](https://ble.ir/lamine_yamal_official) و [نیمار](https://ble.ir/neymar_fa) در این آمار کنار هم هستند.\n@lamine_yamal_official",
        )
    ]


def test_service_links_keywords_with_space_halfspace_and_no_space(tmp_path: Path) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "emit_text.py").write_text(
        """
import json
print(json.dumps({"messages":[{"type":"text","text":"نیکو اورایلی در منچسترسیتی درخشید و برای منچستر‌سیتی فصل بزرگی ساخت."}]}))
""".strip(),
        encoding="utf-8",
    )

    (tmp_path / "keyword_links.json").write_text(
        json.dumps(
            [
                {
                    "destination": "@manchester_city_ir",
                    "link": "https://ble.ir/manchester_city_ir",
                    "keywords": ["منچستر سیتی"],
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    update = {
        "update_id": 114,
        "channel_post": {
            "message_id": 14,
            "chat": {"id": -1001, "type": "channel"},
            "text": "x",
        },
    }

    settings = _settings(tmp_path)
    route = ChannelRoute(
        name="r-link-spacing",
        enabled=True,
        source_channel_id="-1001",
        source_channel_username=None,
        destination_channel_id=None,
        destination_channel_username="@herewegoclub",
        channel_script="emit_text.py",
        max_message_mb=10,
    )
    tg = FakeTelegramClient([update], b"")
    bale = FakeBaleClient()
    service = KiwiService(
        settings=settings,
        routes=_route_registry(route),
        telegram_client=tg,
        bale_client=bale,
        storage=StorageManager(settings.storage_dir),
        guard_runner=GuardRunner(settings.gaurd_scripts_dir, timeout_sec=5),
        script_runner=ScriptRunner(settings.scripts_dir, timeout_sec=5),
        state_store=StateStore(settings.state_path),
    )

    processed = asyncio.run(service.run_once())
    assert processed == 1
    assert bale.sent == [
        (
            "@herewegoclub",
            "نیکو اورایلی در [منچسترسیتی](https://ble.ir/manchester_city_ir) درخشید و برای [منچستر‌سیتی](https://ble.ir/manchester_city_ir) فصل بزرگی ساخت.\n@herewegoclub",
        )
    ]


def test_service_flow_skips_missing_source_media_without_unexpected_error(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    route = ChannelRoute(
        name="r-missing-media",
        enabled=True,
        source_channel_id="-1001",
        source_channel_username="@srcchan",
        destination_channel_id="-2001",
        destination_channel_username=None,
        channel_script=None,
        max_message_mb=10,
    )

    tg = MissingSourceFileTelegramClient([], b"")
    bale = FakeBaleClient()
    service = KiwiService(
        settings=settings,
        routes=_route_registry(route),
        telegram_client=tg,
        bale_client=bale,
        storage=StorageManager(settings.storage_dir),
        guard_runner=GuardRunner(settings.gaurd_scripts_dir, timeout_sec=5),
        script_runner=ScriptRunner(settings.scripts_dir, timeout_sec=5),
        state_store=StateStore(settings.state_path),
    )

    incoming = IncomingChannelMessage(
        update_id=701,
        source_channel_id="-1001",
        source_channel_username="@srcchan",
        message_id=77,
        date=None,
        text=None,
        caption="caption",
        medias=[
            IncomingMedia(
                kind=MediaKind.PHOTO,
                file_id="f1",
                file_size=None,
                file_name="a.jpg",
                mime_type="image/jpeg",
                duration=None,
            )
        ],
        raw={},
        media_group_id=None,
    )

    status, error = asyncio.run(service._process_route_message_detailed(incoming, route))  # noqa: SLF001
    assert status == "skipped"
    assert error == "source_media_unavailable"


def test_service_marks_football_ai_generation_required_as_ambiguous(tmp_path: Path) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "football.py").write_text(
        """
import sys
print("", end="")
print("football_ai_generation_required_failed", file=sys.stderr)
sys.exit(2)
""".strip(),
        encoding="utf-8",
    )

    settings = _settings(tmp_path)
    route = ChannelRoute(
        name="r-ai-required",
        enabled=True,
        source_channel_id="-1001",
        source_channel_username="@srcchan",
        destination_channel_id="-2001",
        destination_channel_username=None,
        channel_script="football.py",
        max_message_mb=10,
    )

    tg = FakeTelegramClient([], b"")
    bale = FakeBaleClient()
    service = KiwiService(
        settings=settings,
        routes=_route_registry(route),
        telegram_client=tg,
        bale_client=bale,
        storage=StorageManager(settings.storage_dir),
        guard_runner=GuardRunner(settings.gaurd_scripts_dir, timeout_sec=5),
        script_runner=ScriptRunner(settings.scripts_dir, timeout_sec=5),
        state_store=StateStore(settings.state_path),
    )

    incoming = IncomingChannelMessage(
        update_id=702,
        source_channel_id="-1001",
        source_channel_username="@srcchan",
        message_id=78,
        date=None,
        text="english text",
        caption=None,
        medias=[],
        raw={},
        media_group_id=None,
    )

    status, error = asyncio.run(service._process_route_message_detailed(incoming, route))  # noqa: SLF001
    assert status == "ambiguous"
    assert error == "football_ai_generation_required_failed"
    assert bale.sent == []


def test_service_flow_allows_route_without_any_scripts(tmp_path: Path) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()

    update = {
        "update_id": 102,
        "channel_post": {
            "message_id": 18,
            "chat": {"id": -1001, "type": "channel", "username": "srcchan"},
            "text": "hello-no-scripts",
        },
    }

    settings = _settings(tmp_path)
    route = ChannelRoute(
        name="r-no-scripts",
        enabled=True,
        source_channel_id="-1001",
        source_channel_username="@srcchan",
        destination_channel_id="-2001",
        destination_channel_username=None,
        channel_script=None,
        gaurd_script=None,
        max_message_mb=10,
    )

    tg = FakeTelegramClient([update], b"")
    bale = FakeBaleClient()
    storage = StorageManager(settings.storage_dir)
    service = KiwiService(
        settings=settings,
        routes=_route_registry(route),
        telegram_client=tg,
        bale_client=bale,
        source_client=None,
        storage=storage,
        guard_runner=GuardRunner(settings.gaurd_scripts_dir, settings.gaurd_script_timeout_sec),
        script_runner=ScriptRunner(settings.scripts_dir, settings.script_timeout_sec),
        state_store=StateStore(settings.state_path),
    )

    asyncio.run(service.run_once())
    assert bale.sent == [("-2001", "hello-no-scripts\n-2001")]

    state = json.loads(Path(settings.state_path).read_text(encoding="utf-8"))
    assert state["offset"] == 103


def test_service_flow_dispatches_from_telethon_source(tmp_path: Path) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "script.py").write_text(
        """
import json
print(json.dumps({"messages":[{"type":"text","text":"from-telethon"}]}))
""".strip(),
        encoding="utf-8",
    )
    settings = _settings(tmp_path)
    settings.telegram_source_mode = "hybrid"
    settings.telethon_enabled = True

    route = ChannelRoute(
        name="r",
        enabled=True,
        source_channel_id=None,
        source_channel_username="@srcchan",
        destination_channel_id="-2001",
        destination_channel_username=None,
        channel_script="script.py",
        max_message_mb=10,
    )
    incoming = IncomingChannelMessage(
        update_id=901,
        source_channel_id="-100123456",
        source_channel_username="@srcchan",
        message_id=901,
        date=None,
        text="hello",
        caption=None,
        medias=[],
        raw={"telethon": True},
    )
    tg = FakeTelegramClient([], b"")
    bale = FakeBaleClient()
    source = FakeTelethonSourceClient([incoming])
    service = KiwiService(
        settings=settings,
        routes=_route_registry(route),
        telegram_client=tg,
        bale_client=bale,
        source_client=source,
        storage=StorageManager(settings.storage_dir),
        guard_runner=GuardRunner(settings.gaurd_scripts_dir, timeout_sec=5),
        script_runner=ScriptRunner(settings.scripts_dir, timeout_sec=5),
        state_store=StateStore(settings.state_path),
    )
    processed = asyncio.run(service.run_once())
    assert processed == 1
    assert bale.sent == [("-2001", "from-telethon\n-2001")]


def test_service_flow_fanout_same_source_to_multiple_routes(tmp_path: Path) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    script_path = scripts_dir / "fanout.py"
    script_path.write_text(
        """
import json
import argparse
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('--payload', required=True)
p.add_argument('--input-dir', required=True)
p.add_argument('--output-dir', required=True)
a = p.parse_args()

payload = json.loads(Path(a.payload).read_text(encoding='utf-8'))
text = payload['message'].get('text') or ''
print(json.dumps({'messages': [{'type': 'text', 'text': 'OUT:' + text}]}))
""".strip(),
        encoding="utf-8",
    )

    update = {
        "update_id": 151,
        "channel_post": {
            "message_id": 9,
            "chat": {"id": -1001, "type": "channel", "username": "srcchan"},
            "text": "hello fanout",
        },
    }

    settings = _settings(tmp_path)
    route1 = ChannelRoute(
        name="r1",
        enabled=True,
        source_channel_id="-1001",
        source_channel_username="@srcchan",
        destination_channel_id="-2001",
        destination_channel_username=None,
        channel_script="fanout.py",
        max_message_mb=10,
    )
    route2 = ChannelRoute(
        name="r2",
        enabled=True,
        source_channel_id="-1001",
        source_channel_username="@srcchan",
        destination_channel_id="-2002",
        destination_channel_username=None,
        channel_script="fanout.py",
        max_message_mb=10,
    )

    tg = FakeTelegramClient([update], b"")
    bale = FakeBaleClient()
    service = KiwiService(
        settings=settings,
        routes=_route_registry([route1, route2]),
        telegram_client=tg,
        bale_client=bale,
        storage=StorageManager(settings.storage_dir),
        guard_runner=GuardRunner(settings.gaurd_scripts_dir, timeout_sec=5),
        script_runner=ScriptRunner(settings.scripts_dir, timeout_sec=5),
        state_store=StateStore(settings.state_path),
    )

    processed = asyncio.run(service.run_once())
    assert processed == 2
    assert bale.sent == [("-2001", "OUT:hello fanout\n-2001"), ("-2002", "OUT:hello fanout\n-2002")]


def test_service_flow_skips_large_message(tmp_path: Path) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "-1001.py").write_text("print('{\"messages\": []}')", encoding="utf-8")

    update = {
        "update_id": 201,
        "channel_post": {
            "message_id": 11,
            "chat": {"id": -1001, "type": "channel"},
            "document": {"file_id": "f2", "file_size": 100},
        },
    }

    settings = _settings(tmp_path, default_max_mb=1)
    route = ChannelRoute(
        name="r",
        enabled=True,
        source_channel_id="-1001",
        source_channel_username=None,
        destination_channel_id="-2001",
        destination_channel_username=None,
        channel_script="-1001.py",
        max_message_mb=1,
    )

    # 2MB payload -> must exceed 1MB max
    tg = FakeTelegramClient([update], b"x" * (2 * 1024 * 1024))
    bale = FakeBaleClient()

    service = KiwiService(
        settings=settings,
        routes=_route_registry(route),
        telegram_client=tg,
        bale_client=bale,
        storage=StorageManager(settings.storage_dir),
        guard_runner=GuardRunner(settings.gaurd_scripts_dir, timeout_sec=5),
        script_runner=ScriptRunner(settings.scripts_dir, timeout_sec=5),
        state_store=StateStore(settings.state_path),
    )

    processed = asyncio.run(service.run_once())
    assert processed == 1
    assert bale.sent == []


def test_service_flow_drops_oversized_script_output_media(tmp_path: Path) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "-1001.py").write_text(
        """
import argparse
import json
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--payload", required=True)
parser.add_argument("--input-dir", required=True)
parser.add_argument("--output-dir", required=True)
args = parser.parse_args()

output_path = Path(args.output_dir) / "huge.bin"
output_path.write_bytes(b"x" * (2 * 1024 * 1024))
print(json.dumps({"messages": [{"type": "document", "path": "huge.bin"}]}))
""".strip(),
        encoding="utf-8",
    )

    update = {
        "update_id": 211,
        "channel_post": {
            "message_id": 12,
            "chat": {"id": -1001, "type": "channel"},
            "text": "hello",
        },
    }

    settings = _settings(tmp_path, default_max_mb=1)
    route = ChannelRoute(
        name="r",
        enabled=True,
        source_channel_id="-1001",
        source_channel_username=None,
        destination_channel_id="-2001",
        destination_channel_username=None,
        channel_script="-1001.py",
        max_message_mb=1,
    )

    tg = FakeTelegramClient([update], b"")
    bale = FakeBaleClient()
    service = KiwiService(
        settings=settings,
        routes=_route_registry(route),
        telegram_client=tg,
        bale_client=bale,
        storage=StorageManager(settings.storage_dir),
        guard_runner=GuardRunner(settings.gaurd_scripts_dir, timeout_sec=5),
        script_runner=ScriptRunner(settings.scripts_dir, timeout_sec=5),
        state_store=StateStore(settings.state_path),
    )

    processed = asyncio.run(service.run_once())
    assert processed == 1
    assert bale.sent == []


def test_service_flow_skips_sticker_media(tmp_path: Path) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "-1001.py").write_text("print('{\"messages\": []}')", encoding="utf-8")

    update = {
        "update_id": 301,
        "channel_post": {
            "message_id": 21,
            "chat": {"id": -1001, "type": "channel"},
            "sticker": {"file_id": "st1", "file_size": 1234},
        },
    }

    settings = _settings(tmp_path)
    route = ChannelRoute(
        name="r",
        enabled=True,
        source_channel_id="-1001",
        source_channel_username=None,
        destination_channel_id="-2001",
        destination_channel_username=None,
        channel_script="-1001.py",
        max_message_mb=10,
    )
    tg = FakeTelegramClient([update], b"abc")
    bale = FakeBaleClient()
    service = KiwiService(
        settings=settings,
        routes=_route_registry(route),
        telegram_client=tg,
        bale_client=bale,
        storage=StorageManager(settings.storage_dir),
        guard_runner=GuardRunner(settings.gaurd_scripts_dir, timeout_sec=5),
        script_runner=ScriptRunner(settings.scripts_dir, timeout_sec=5),
        state_store=StateStore(settings.state_path),
    )

    processed = asyncio.run(service.run_once())
    assert processed == 1
    assert tg.get_file_calls == 0
    assert bale.sent == []


def test_service_flow_blocks_message_when_guard_denies(tmp_path: Path) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "-1001.py").write_text("print('{\"messages\": [{\"type\":\"text\",\"text\":\"ok\"}]}')", encoding="utf-8")

    update = {
        "update_id": 401,
        "channel_post": {
            "message_id": 31,
            "chat": {"id": -1001, "type": "channel", "username": "srcchan"},
            "text": "hello",
        },
    }

    settings = _settings(tmp_path)
    settings.log_channel_target = "@logchan"
    guard_path = Path(settings.gaurd_scripts_dir) / "deny.py"
    guard_path.write_text("print('false')", encoding="utf-8")
    route = ChannelRoute(
        name="r",
        enabled=True,
        source_channel_id="-1001",
        source_channel_username="@srcchan",
        destination_channel_id="-2001",
        destination_channel_username=None,
        channel_script="-1001.py",
        max_message_mb=10,
        gaurd_script="deny.py",
    )
    tg = FakeTelegramClient([update], b"")
    bale = FakeBaleClient()
    service = KiwiService(
        settings=settings,
        routes=_route_registry(route),
        telegram_client=tg,
        bale_client=bale,
        storage=StorageManager(settings.storage_dir),
        guard_runner=GuardRunner(settings.gaurd_scripts_dir, timeout_sec=5),
        script_runner=ScriptRunner(settings.scripts_dir, timeout_sec=5),
        state_store=StateStore(settings.state_path),
    )

    processed = asyncio.run(service.run_once())
    assert processed == 1
    assert bale.sent == []
    assert any(chat == "@logchan" for chat, _ in tg.audit_messages)
    assert any("توضیح: guard_denied" in text for _, text in tg.audit_messages)
    assert any("لینک پیام: https://t.me/srcchan/31" in text for _, text in tg.audit_messages)


def test_service_keeps_original_document_name(tmp_path: Path) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    script_path = scripts_dir / "-1001.py"
    script_path.write_text(
        """
import json
import argparse
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('--payload', required=True)
p.add_argument('--input-dir', required=True)
p.add_argument('--output-dir', required=True)
a = p.parse_args()
payload = json.loads(Path(a.payload).read_text(encoding='utf-8'))
local_name = payload['inputs'][0]['local_name']
print(json.dumps({'messages': [{'type': 'document', 'path': local_name}]}))
""".strip(),
        encoding="utf-8",
    )

    update = {
        "update_id": 501,
        "channel_post": {
            "message_id": 41,
            "chat": {"id": -1001, "type": "channel"},
            "document": {"file_id": "f3", "file_name": "Original.Name v1.pdf"},
        },
    }
    settings = _settings(tmp_path)
    route = ChannelRoute(
        name="r",
        enabled=True,
        source_channel_id="-1001",
        source_channel_username=None,
        destination_channel_id="-2001",
        destination_channel_username=None,
        channel_script="-1001.py",
        max_message_mb=10,
    )
    tg = FakeTelegramClient([update], b"abc")
    bale = FakeBaleClient()
    service = KiwiService(
        settings=settings,
        routes=_route_registry(route),
        telegram_client=tg,
        bale_client=bale,
        storage=StorageManager(settings.storage_dir),
        guard_runner=GuardRunner(settings.gaurd_scripts_dir, timeout_sec=5),
        script_runner=ScriptRunner(settings.scripts_dir, timeout_sec=5),
        state_store=StateStore(settings.state_path),
    )
    processed = asyncio.run(service.run_once())
    assert processed == 1
    assert bale.sent == [("-2001", "doc:Original.Name v1.pdf")]


def test_service_merges_same_media_group_in_single_processing(tmp_path: Path) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    script_path = scripts_dir / "-1001.py"
    script_path.write_text(
        """
import json
import argparse
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('--payload', required=True)
p.add_argument('--input-dir', required=True)
p.add_argument('--output-dir', required=True)
a = p.parse_args()
payload = json.loads(Path(a.payload).read_text(encoding='utf-8'))
count = len(payload.get('inputs', []))
print(json.dumps({'messages': [{'type': 'text', 'text': f'COUNT:{count}'}]}))
""".strip(),
        encoding="utf-8",
    )
    updates = [
        {
            "update_id": 601,
            "channel_post": {
                "message_id": 71,
                "chat": {"id": -1001, "type": "channel"},
                "media_group_id": "mg-1",
                "caption": "cap",
                "photo": [{"file_id": "p1"}],
            },
        },
        {
            "update_id": 602,
            "channel_post": {
                "message_id": 72,
                "chat": {"id": -1001, "type": "channel"},
                "media_group_id": "mg-1",
                "photo": [{"file_id": "p2"}],
            },
        },
    ]
    settings = _settings(tmp_path)
    route = ChannelRoute(
        name="r",
        enabled=True,
        source_channel_id="-1001",
        source_channel_username=None,
        destination_channel_id="-2001",
        destination_channel_username=None,
        channel_script="-1001.py",
        max_message_mb=10,
    )
    tg = FakeTelegramClient(updates, b"x")
    bale = FakeBaleClient()
    service = KiwiService(
        settings=settings,
        routes=_route_registry(route),
        telegram_client=tg,
        bale_client=bale,
        storage=StorageManager(settings.storage_dir),
        guard_runner=GuardRunner(settings.gaurd_scripts_dir, timeout_sec=5),
        script_runner=ScriptRunner(settings.scripts_dir, timeout_sec=5),
        state_store=StateStore(settings.state_path),
    )
    processed = asyncio.run(service.run_once())
    assert processed == 1
    assert bale.sent == [("-2001", "COUNT:2\n-2001")]


def test_service_merges_media_group_across_polls(tmp_path: Path) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    script_path = scripts_dir / "-1001.py"
    script_path.write_text(
        """
import json
import argparse
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('--payload', required=True)
p.add_argument('--input-dir', required=True)
p.add_argument('--output-dir', required=True)
a = p.parse_args()
payload = json.loads(Path(a.payload).read_text(encoding='utf-8'))
print(json.dumps({'messages': [{'type': 'text', 'text': f\"COUNT:{len(payload.get('inputs', []))}\"}]}))
""".strip(),
        encoding="utf-8",
    )

    class SplitPollTelegramClient(FakeTelegramClient):
        def __init__(self, update_batches: list[list[dict]], file_bytes: bytes) -> None:
            super().__init__([], file_bytes)
            self._batches = update_batches
            self._cursor = 0

        async def get_updates(self, offset, timeout, allowed_updates):
            if self._cursor >= len(self._batches):
                return []
            batch = self._batches[self._cursor]
            self._cursor += 1
            return batch

    updates1 = [
        {
            "update_id": 801,
            "channel_post": {
                "message_id": 91,
                "chat": {"id": -1001, "type": "channel"},
                "media_group_id": "mg-x",
                "caption": "cap",
                "photo": [{"file_id": "x1"}],
            },
        }
    ]
    updates2 = [
        {
            "update_id": 802,
            "channel_post": {
                "message_id": 92,
                "chat": {"id": -1001, "type": "channel"},
                "media_group_id": "mg-x",
                "photo": [{"file_id": "x2"}],
            },
        }
    ]
    settings = _settings(tmp_path)
    settings.media_group_wait_sec = 0.02
    route = ChannelRoute(
        name="r",
        enabled=True,
        source_channel_id="-1001",
        source_channel_username=None,
        destination_channel_id="-2001",
        destination_channel_username=None,
        channel_script="-1001.py",
        max_message_mb=10,
    )
    tg = SplitPollTelegramClient([updates1, updates2], b"x")
    bale = FakeBaleClient()
    service = KiwiService(
        settings=settings,
        routes=_route_registry(route),
        telegram_client=tg,
        bale_client=bale,
        storage=StorageManager(settings.storage_dir),
        guard_runner=GuardRunner(settings.gaurd_scripts_dir, timeout_sec=5),
        script_runner=ScriptRunner(settings.scripts_dir, timeout_sec=5),
        state_store=StateStore(settings.state_path),
    )

    async def _drive() -> tuple[int, int, int]:
        p1 = await service.run_once()
        p2 = await service.run_once()
        await asyncio.sleep(0.03)
        p3 = await service.run_once()
        return p1, p2, p3

    p1, p2, p3 = asyncio.run(_drive())
    assert (p1, p2, p3) == (0, 0, 1)
    assert bale.sent == [("-2001", "COUNT:2\n-2001")]


def test_service_merges_telethon_media_group_before_processing(tmp_path: Path) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    script_path = scripts_dir / "count_inputs.py"
    script_path.write_text(
        """
import json
import argparse
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('--payload', required=True)
p.add_argument('--input-dir', required=True)
p.add_argument('--output-dir', required=True)
a = p.parse_args()
payload = json.loads(Path(a.payload).read_text(encoding='utf-8'))
count = len(payload.get('inputs', []))
caption = str((payload.get('message') or {}).get('caption') or '')
print(json.dumps({'messages': [{'type': 'text', 'text': f'COUNT:{count}|CAP:{caption}'}]}))
""".strip(),
        encoding="utf-8",
    )

    settings = _settings(tmp_path)
    settings.telegram_source_mode = "hybrid"
    settings.telethon_enabled = True

    route = ChannelRoute(
        name="telethon-group",
        enabled=True,
        source_channel_id="-1009",
        source_channel_username="@src",
        destination_channel_id="-2001",
        destination_channel_username=None,
        channel_script="count_inputs.py",
        max_message_mb=10,
    )
    m1 = IncomingChannelMessage(
        update_id=10001,
        source_channel_id="-1009",
        source_channel_username="@src",
        message_id=201,
        date=None,
        text=None,
        caption="album cap",
        medias=[],
        raw={"telethon": True},
        media_group_id="g1",
    )
    m1.medias.append(
        IncomingMedia(
            kind=MediaKind.PHOTO,
            file_id="mt:@src:201",
            source="telethon",
            source_ref={"source_key": "@src", "message_id": 201},
        )
    )
    m2 = IncomingChannelMessage(
        update_id=10002,
        source_channel_id="-1009",
        source_channel_username="@src",
        message_id=202,
        date=None,
        text=None,
        caption=None,
        medias=[],
        raw={"telethon": True},
        media_group_id="g1",
    )
    m2.medias.append(
        IncomingMedia(
            kind=MediaKind.PHOTO,
            file_id="mt:@src:202",
            source="telethon",
            source_ref={"source_key": "@src", "message_id": 202},
        )
    )

    tg = FakeTelegramClient([], b"")
    bale = FakeBaleClient()
    source = FakeTelethonSourceClient(messages=[m1, m2], seeded_messages=[])
    service = KiwiService(
        settings=settings,
        routes=_route_registry(route),
        telegram_client=tg,
        bale_client=bale,
        source_client=source,
        storage=StorageManager(settings.storage_dir),
        guard_runner=GuardRunner(settings.gaurd_scripts_dir, timeout_sec=5),
        script_runner=ScriptRunner(settings.scripts_dir, timeout_sec=5),
        state_store=StateStore(settings.state_path),
    )
    processed = asyncio.run(service.run_once())
    assert processed == 1
    assert bale.sent == [("-2001", "COUNT:2|CAP:album cap\n-2001")]


def test_service_sends_audit_logs_to_telegram_channel(tmp_path: Path) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "-1001.py").write_text("print('{\"messages\": [{\"type\":\"text\",\"text\":\"ok\"}]}')", encoding="utf-8")

    update = {
        "update_id": 701,
        "channel_post": {
            "message_id": 81,
            "chat": {"id": -1001, "type": "channel", "username": "srcchan"},
            "text": "hello",
        },
    }
    settings = _settings(tmp_path)
    settings.log_channel_target = "@logchan"
    route = ChannelRoute(
        name="r",
        enabled=True,
        source_channel_id="-1001",
        source_channel_username="@srcchan",
        destination_channel_id="-2001",
        destination_channel_username=None,
        channel_script="-1001.py",
        max_message_mb=10,
    )
    tg = FakeTelegramClient([update], b"")
    bale = FakeBaleClient()
    service = KiwiService(
        settings=settings,
        routes=_route_registry(route),
        telegram_client=tg,
        bale_client=bale,
        storage=StorageManager(settings.storage_dir),
        guard_runner=GuardRunner(settings.gaurd_scripts_dir, timeout_sec=5),
        script_runner=ScriptRunner(settings.scripts_dir, timeout_sec=5),
        state_store=StateStore(settings.state_path),
    )
    processed = asyncio.run(service.run_once())
    assert processed == 1
    assert bale.sent == [("-2001", "ok\n-2001")]
    assert len(tg.audit_messages) >= 1
    assert all(chat == "@logchan" for chat, _ in tg.audit_messages)
    assert any("کیوی" in text for _, text in tg.audit_messages)
    assert any("لینک پیام: https://t.me/srcchan/81" in text for _, text in tg.audit_messages)


def test_service_handles_admin_private_commands(tmp_path: Path) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "-1001.py").write_text("print('{\"messages\": []}')", encoding="utf-8")

    guards_dir = tmp_path / "gaurds"
    guards_dir.mkdir(parents=True, exist_ok=True)
    (guards_dir / "default_guard.py").write_text("print('true')", encoding="utf-8")
    channels_path = tmp_path / "channels.json"
    channels_path.write_text(
        '[{"name":"r1","enabled":true,"source_channel_id":"-1001","destination_channel_id":"-2001","channel_script":"-1001.py","gaurd_script":"default_guard.py"}]',
        encoding="utf-8",
    )

    update = {
        "update_id": 711,
        "message": {
            "message_id": 1,
            "chat": {"id": 555, "type": "private"},
            "from": {"id": 777, "username": "admin_root"},
            "text": "/login admin change_me",
        },
    }
    settings = _settings(tmp_path)
    settings.channels_config_path = str(channels_path)
    settings.scripts_dir = str(scripts_dir)
    settings.gaurd_scripts_dir = str(guards_dir)
    tg = FakeTelegramClient([update], b"")
    bale = FakeBaleClient()

    service = KiwiService(
        settings=settings,
        routes=_route_registry(
            ChannelRoute(
                name="r1",
                enabled=True,
                source_channel_id="-1001",
                source_channel_username=None,
                destination_channel_id="-2001",
                destination_channel_username=None,
                channel_script="-1001.py",
                max_message_mb=10,
            )
        ),
        telegram_client=tg,
        bale_client=bale,
        storage=StorageManager(settings.storage_dir),
        guard_runner=GuardRunner(settings.gaurd_scripts_dir, timeout_sec=5),
        script_runner=ScriptRunner(settings.scripts_dir, timeout_sec=5),
        state_store=StateStore(settings.state_path),
    )
    store = AdminStore(settings.admin_users_config_path, settings.admin_sessions_path)
    api = ManagementApi(
        channels_config_path=settings.channels_config_path,
        scripts_dir=settings.scripts_dir,
        gaurd_scripts_dir=settings.gaurd_scripts_dir,
        on_routes_reloaded=service.set_routes,
    )
    service.admin_handler = AdminBotHandler(admin_store=store, management_api=api)

    processed = asyncio.run(service.run_once())
    assert processed == 0
    assert any(chat == "555" and "موفق" in text for chat, text in tg.audit_messages)


def test_service_syncing_mode_tracks_pending_and_auto_activates(tmp_path: Path) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "sync.py").write_text(
        """
import json
import argparse
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('--payload', required=True)
p.add_argument('--input-dir', required=True)
p.add_argument('--output-dir', required=True)
a = p.parse_args()

payload = json.loads(Path(a.payload).read_text(encoding='utf-8'))
text = payload['message'].get('text') or ''
print(json.dumps({'messages': [{'type': 'text', 'text': 'SYNC:' + text}]}))
""".strip(),
        encoding="utf-8",
    )
    guards_dir = tmp_path / "gaurds"
    guards_dir.mkdir(parents=True, exist_ok=True)
    (guards_dir / "default_guard.py").write_text("print('true')", encoding="utf-8")

    channels_path = tmp_path / "channels.json"
    channels_path.write_text(
        json.dumps(
            [
                {
                    "name": "sync-route",
                    "enabled": False,
                    "source_channel_id": "-1001",
                    "destination_channel_id": "-2001",
                    "channel_script": "sync.py",
                    "gaurd_script": "default_guard.py",
                    "sync": {
                        "enabled": True,
                        "status": "syncing",
                        "backfill_count": 100,
                        "interval_sec": 300,
                        "batch_size": 1,
                        "retry_attempts": 2,
                        "pending_count": 0,
                        "processed_count": 0,
                        "seeded": False,
                    },
                }
            ]
        ),
        encoding="utf-8",
    )

    update1 = {
        "update_id": 801,
        "channel_post": {
            "message_id": 88,
            "chat": {"id": -1001, "type": "channel"},
            "text": "hello",
        },
    }
    update3 = {
        "update_id": 803,
        "channel_post": {
            "message_id": 90,
            "chat": {"id": -1001, "type": "channel"},
            "text": "live",
        },
    }

    settings = _settings(tmp_path)
    settings.channels_config_path = str(channels_path)
    settings.scripts_dir = str(scripts_dir)
    settings.gaurd_scripts_dir = str(guards_dir)
    tg = FakeTelegramSequenceClient([[update1]], b"")
    bale = FakeBaleClient()
    service = KiwiService(
        settings=settings,
        routes=load_routes(str(channels_path)),
        telegram_client=tg,
        bale_client=bale,
        storage=StorageManager(settings.storage_dir),
        guard_runner=GuardRunner(settings.gaurd_scripts_dir, timeout_sec=5),
        script_runner=ScriptRunner(settings.scripts_dir, timeout_sec=5),
        state_store=StateStore(settings.state_path),
    )
    api = ManagementApi(
        channels_config_path=str(channels_path),
        scripts_dir=str(scripts_dir),
        gaurd_scripts_dir=str(guards_dir),
        sync_ledger=service.sync_ledger,
        sync_queue=service.sync_queue,
        on_routes_reloaded=service.set_routes,
    )
    service.set_route_patch_callback(api.update_route)

    async def _drive() -> None:
        for _ in range(20):
            await service.run_once()
            await asyncio.sleep(0.03)
            saved_now = json.loads(channels_path.read_text(encoding="utf-8"))
            sync_now = saved_now[0]["sync"]
            if sync_now.get("status") == "active":
                break
        tg._batches.append([update3])  # noqa: SLF001
        tg._batches.append([])  # noqa: SLF001
        await service.run_once()
        await asyncio.sleep(0.03)
        await service.run_once()

    asyncio.run(_drive())
    assert bale.sent == [
        ("-2001", "SYNC:hello\n-2001"),
        ("-2001", "SYNC:live\n-2001"),
    ]
    key_live = service.sync_ledger.dedupe_key("sync-route", "-1001", 90, None)
    assert service.sync_ledger.get_record(key_live) is None

    saved = json.loads(channels_path.read_text(encoding="utf-8"))
    sync = saved[0]["sync"]
    assert sync["pending_count"] == 0
    assert sync["status"] == "active"
    assert sync["enabled"] is False
    assert saved[0]["enabled"] is True


def test_service_syncing_retries_non_guard_failures(tmp_path: Path) -> None:
    channels_path = tmp_path / "channels.json"
    channels_path.write_text(
        json.dumps(
            [
                {
                    "name": "sync-retry-route",
                    "enabled": True,
                    "source_channel_id": "-1001",
                    "destination_channel_id": "-2001",
                    "channel_script": "-1001.py",
                    "gaurd_script": "default_guard.py",
                    "sync": {
                        "enabled": True,
                        "status": "syncing",
                        "backfill_count": 0,
                        "interval_sec": 1,
                        "batch_size": 1,
                        "retry_attempts": 2,
                        "pending_count": 1,
                        "processed_count": 0,
                        "seeded": True,
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    settings = _settings(tmp_path)
    settings.channels_config_path = str(channels_path)
    route = load_routes(str(channels_path)).routes[0]

    service = KiwiService(
        settings=settings,
        routes=load_routes(str(channels_path)),
        telegram_client=FakeTelegramClient([], b""),
        bale_client=FakeBaleClient(),
        storage=StorageManager(settings.storage_dir),
        guard_runner=GuardRunner(settings.gaurd_scripts_dir, timeout_sec=5),
        script_runner=ScriptRunner(settings.scripts_dir, timeout_sec=5),
        state_store=StateStore(settings.state_path),
    )
    msg = IncomingChannelMessage(
        update_id=999,
        source_channel_id="-1001",
        source_channel_username=None,
        message_id=123,
        date=None,
        text="x",
        caption=None,
        medias=[],
        raw={},
        media_group_id=None,
    )
    payload = service._serialize_incoming(msg)  # noqa: SLF001
    created, dedupe_key, _ = service.sync_ledger.register_message(  # noqa: SLF001
        route_name=route.name,
        source_channel_id=msg.source_channel_id,
        message_id=msg.message_id,
        media_group_id=msg.media_group_id,
        payload=payload,
    )
    assert created is True
    asyncio.run(
        service.sync_queue.enqueue(  # noqa: SLF001
            dedupe_key=dedupe_key,
            route_name=route.name,
            due_at=0.0,
            payload_hash="x",
        )
    )

    calls = {"n": 0}

    async def _fake_process(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 3:
            return ("ok", None)
        return ("failed", "sendMessage HTTP 500")

    service._process_route_message_detailed = _fake_process  # type: ignore[method-assign]  # noqa: SLF001
    processed = asyncio.run(service._drain_sync_queue())  # noqa: SLF001
    assert processed == 1
    assert calls["n"] == 3


def test_service_sync_ledger_dedupes_same_message_after_sent(tmp_path: Path) -> None:
    channels_path = tmp_path / "channels.json"
    channels_path.write_text(
        json.dumps(
            [
                {
                    "name": "sync-dedupe-route",
                    "enabled": True,
                    "source_channel_id": "-1001",
                    "destination_channel_id": "-2001",
                    "channel_script": None,
                    "gaurd_script": "default_guard.py",
                    "sync": {"enabled": True, "status": "syncing", "seeded": True},
                }
            ]
        ),
        encoding="utf-8",
    )
    settings = _settings(tmp_path)
    settings.channels_config_path = str(channels_path)
    bale = FakeBaleClient()
    service = KiwiService(
        settings=settings,
        routes=load_routes(str(channels_path)),
        telegram_client=FakeTelegramClient([], b""),
        bale_client=bale,
        storage=StorageManager(settings.storage_dir),
        guard_runner=GuardRunner(settings.gaurd_scripts_dir, timeout_sec=5),
        script_runner=ScriptRunner(settings.scripts_dir, timeout_sec=5),
        state_store=StateStore(settings.state_path),
    )
    route = service.routes.routes[0]
    incoming = IncomingChannelMessage(
        update_id=1001,
        source_channel_id="-1001",
        source_channel_username=None,
        message_id=101,
        date=None,
        text="hello",
        caption=None,
        medias=[],
        raw={},
        media_group_id=None,
    )
    asyncio.run(service._enqueue_sync_message(route, incoming))  # noqa: SLF001
    first = asyncio.run(service._drain_sync_queue())  # noqa: SLF001
    assert first == 1
    assert bale.sent == [("-2001", "hello\n-2001")]

    asyncio.run(service._enqueue_sync_message(route, incoming))  # noqa: SLF001
    second = asyncio.run(service._drain_sync_queue())  # noqa: SLF001
    assert second == 0
    assert bale.sent == [("-2001", "hello\n-2001")]


def test_service_sync_marks_ambiguous_and_creates_review(tmp_path: Path) -> None:
    channels_path = tmp_path / "channels.json"
    channels_path.write_text(
        json.dumps(
            [
                {
                    "name": "sync-ambiguous-route",
                    "enabled": True,
                    "source_channel_id": "-1001",
                    "destination_channel_id": "-2001",
                    "channel_script": None,
                    "gaurd_script": "default_guard.py",
                    "sync": {"enabled": True, "status": "syncing", "seeded": True},
                }
            ]
        ),
        encoding="utf-8",
    )
    settings = _settings(tmp_path)
    settings.channels_config_path = str(channels_path)
    service = KiwiService(
        settings=settings,
        routes=load_routes(str(channels_path)),
        telegram_client=FakeTelegramClient([], b""),
        bale_client=FakeBaleClient(),
        storage=StorageManager(settings.storage_dir),
        guard_runner=GuardRunner(settings.gaurd_scripts_dir, timeout_sec=5),
        script_runner=ScriptRunner(settings.scripts_dir, timeout_sec=5),
        state_store=StateStore(settings.state_path),
    )
    route = service.routes.routes[0]
    incoming = IncomingChannelMessage(
        update_id=2001,
        source_channel_id="-1001",
        source_channel_username=None,
        message_id=202,
        date=None,
        text="x",
        caption=None,
        medias=[],
        raw={},
        media_group_id=None,
    )

    async def _fake_detailed(*args, **kwargs):
        return ("ambiguous", "sendMessage HTTP 500")

    service._process_route_message_detailed = _fake_detailed  # type: ignore[method-assign]  # noqa: SLF001
    asyncio.run(service._enqueue_sync_message(route, incoming))  # noqa: SLF001
    processed = asyncio.run(service._drain_sync_queue())  # noqa: SLF001
    assert processed == 1

    key = service.sync_ledger.dedupe_key(route.name, incoming.source_channel_id, incoming.message_id, None)  # noqa: SLF001
    record = service.sync_ledger.get_record(key)  # noqa: SLF001
    assert record is not None
    assert record.status == "ambiguous"
    reviews = service.sync_ledger.list_review(limit=10, only_open=True)  # noqa: SLF001
    assert len(reviews) == 1
    assert reviews[0]["dedupe_key"] == key


def test_service_sync_seeds_from_stored_messages(tmp_path: Path) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "sync.py").write_text(
        """
import json
import argparse
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('--payload', required=True)
p.add_argument('--input-dir', required=True)
p.add_argument('--output-dir', required=True)
a = p.parse_args()

payload = json.loads(Path(a.payload).read_text(encoding='utf-8'))
text = payload['message'].get('text') or ''
print(json.dumps({'messages': [{'type': 'text', 'text': 'SYNC:' + text}]}))
""".strip(),
        encoding="utf-8",
    )
    guards_dir = tmp_path / "gaurds"
    guards_dir.mkdir(parents=True, exist_ok=True)
    (guards_dir / "default_guard.py").write_text("print('true')", encoding="utf-8")

    channels_path = tmp_path / "channels.json"
    channels_path.write_text(
        json.dumps(
            [
                {
                    "name": "sync-from-storage",
                    "enabled": True,
                    "source_channel_username": "@stored_src",
                    "destination_channel_id": "-2001",
                    "channel_script": "sync.py",
                    "gaurd_script": "default_guard.py",
                    "sync": {
                        "enabled": True,
                        "status": "active",
                        "backfill_count": 2,
                        "interval_sec": 1,
                        "batch_size": 1,
                        "retry_attempts": 2,
                        "pending_count": 0,
                        "processed_count": 0,
                        "seeded": False,
                    },
                }
            ]
        ),
        encoding="utf-8",
    )

    settings = _settings(tmp_path)
    settings.channels_config_path = str(channels_path)
    settings.scripts_dir = str(scripts_dir)
    settings.gaurd_scripts_dir = str(guards_dir)
    message_root = Path(settings.storage_dir) / "messages" / "-100555"
    for idx in [1, 2, 3]:
        base = message_root / f"{900 + idx}_20260101T00000{idx}Z"
        base.mkdir(parents=True, exist_ok=True)
        raw_update = {
            "update_id": 900 + idx,
            "channel_post": {
                "message_id": idx,
                "chat": {"id": -100555, "type": "channel", "username": "stored_src"},
                "text": f"hist-{idx}",
            },
        }
        (base / "raw_update.json").write_text(json.dumps(raw_update), encoding="utf-8")

    service = KiwiService(
        settings=settings,
        routes=load_routes(str(channels_path)),
        telegram_client=FakeTelegramClient([], b""),
        bale_client=FakeBaleClient(),
        storage=StorageManager(settings.storage_dir),
        guard_runner=GuardRunner(settings.gaurd_scripts_dir, timeout_sec=5),
        script_runner=ScriptRunner(settings.scripts_dir, timeout_sec=5),
        state_store=StateStore(settings.state_path),
    )

    processed1 = asyncio.run(service.run_once())
    assert processed1 == 0
    assert service.bale_client.sent == []
    checkpoint = service.sync_ledger.get_route_checkpoint("sync-from-storage")  # noqa: SLF001
    assert checkpoint == 3


def test_service_sync_prefers_source_backfill_over_local_storage(tmp_path: Path) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "sync.py").write_text(
        """
import json
import argparse
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument('--payload', required=True)
p.add_argument('--input-dir', required=True)
p.add_argument('--output-dir', required=True)
a = p.parse_args()

payload = json.loads(Path(a.payload).read_text(encoding='utf-8'))
text = payload['message'].get('text') or ''
print(json.dumps({'messages': [{'type': 'text', 'text': 'SYNC:' + text}]}))
""".strip(),
        encoding="utf-8",
    )
    guards_dir = tmp_path / "gaurds"
    guards_dir.mkdir(parents=True, exist_ok=True)
    (guards_dir / "default_guard.py").write_text("print('true')", encoding="utf-8")

    channels_path = tmp_path / "channels.json"
    channels_path.write_text(
        json.dumps(
            [
                {
                    "name": "sync-from-source",
                    "enabled": True,
                    "source_channel_username": "@stored_src",
                    "destination_channel_id": "-2001",
                    "channel_script": "sync.py",
                    "gaurd_script": "default_guard.py",
                    "sync": {
                        "enabled": True,
                        "status": "syncing",
                        "backfill_count": 2,
                        "interval_sec": 1,
                        "batch_size": 2,
                        "retry_attempts": 2,
                        "pending_count": 0,
                        "processed_count": 0,
                        "seeded": False,
                    },
                }
            ]
        ),
        encoding="utf-8",
    )

    settings = _settings(tmp_path)
    settings.channels_config_path = str(channels_path)
    settings.scripts_dir = str(scripts_dir)
    settings.gaurd_scripts_dir = str(guards_dir)

    # Old local snapshots must be ignored when source backfill is available.
    old_root = Path(settings.storage_dir) / "messages" / "-100555"
    old_root.mkdir(parents=True, exist_ok=True)
    (old_root / "1_20220101T000001Z").mkdir(parents=True, exist_ok=True)
    (old_root / "1_20220101T000001Z" / "raw_update.json").write_text(
        json.dumps(
            {
                "update_id": 1,
                "channel_post": {
                    "message_id": 1,
                    "chat": {"id": -100555, "type": "channel", "username": "stored_src"},
                    "text": "old-storage",
                },
            }
        ),
        encoding="utf-8",
    )

    seeded = [
        IncomingChannelMessage(
            update_id=9001,
            source_channel_id="-100555",
            source_channel_username="@stored_src",
            message_id=90,
            date=None,
            text="src-1",
            caption=None,
            medias=[],
            raw={"telethon": True},
            media_group_id=None,
        ),
        IncomingChannelMessage(
            update_id=9002,
            source_channel_id="-100555",
            source_channel_username="@stored_src",
            message_id=91,
            date=None,
            text="src-2",
            caption=None,
            medias=[],
            raw={"telethon": True},
            media_group_id=None,
        ),
    ]
    source = FakeTelethonSourceClient(messages=[], seeded_messages=seeded)

    service = KiwiService(
        settings=settings,
        routes=load_routes(str(channels_path)),
        telegram_client=FakeTelegramClient([], b""),
        bale_client=FakeBaleClient(),
        source_client=source,
        storage=StorageManager(settings.storage_dir),
        guard_runner=GuardRunner(settings.gaurd_scripts_dir, timeout_sec=5),
        script_runner=ScriptRunner(settings.scripts_dir, timeout_sec=5),
        state_store=StateStore(settings.state_path),
    )

    processed = asyncio.run(service.run_once())
    assert processed == 0
    assert source.seed_calls == []
    assert service.bale_client.sent == []


class FlakyTelegramClient:
    def __init__(self) -> None:
        self.calls = 0

    async def get_updates(self, offset, timeout, allowed_updates):
        self.calls += 1
        if self.calls == 1:
            raise PlatformApiError("network timeout")
        return []

    async def aclose(self) -> None:
        return None


def test_service_run_retries_after_poll_error(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    route = ChannelRoute(
        name="r",
        enabled=True,
        source_channel_id="-1001",
        source_channel_username=None,
        destination_channel_id="-2001",
        destination_channel_username=None,
        channel_script="-1001.py",
        max_message_mb=1,
    )
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "-1001.py").write_text("print('{\"messages\": []}')", encoding="utf-8")

    tg = FlakyTelegramClient()
    bale = FakeBaleClient()
    service = KiwiService(
        settings=settings,
        routes=_route_registry(route),
        telegram_client=tg,
        bale_client=bale,
        storage=StorageManager(settings.storage_dir),
        guard_runner=GuardRunner(settings.gaurd_scripts_dir, timeout_sec=5),
        script_runner=ScriptRunner(settings.scripts_dir, timeout_sec=5),
        state_store=StateStore(settings.state_path),
    )

    async def _run_for_a_moment() -> None:
        task = asyncio.create_task(service.run())
        await asyncio.sleep(0.05)
        await service.stop()
        await task

    asyncio.run(_run_for_a_moment())
    assert tg.calls >= 2


def test_service_logs_each_stage_with_stage_output(tmp_path: Path) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "stage_script.py").write_text(
        """
import json
print(json.dumps({"messages":[{"type":"text","text":"sample out"}]}))
""".strip(),
        encoding="utf-8",
    )

    update = {
        "update_id": 144,
        "channel_post": {
            "message_id": 144,
            "chat": {"id": -100123, "type": "channel", "username": "kiwi_kiwi_test"},
            "text": "Bruno Fernandes congratulating Marcus Rashford on his LALIGA title win ❤️",
        },
    }

    settings = _settings(tmp_path)
    settings.log_channel_target = "@logchan"
    route = ChannelRoute(
        name="kiwi_test",
        enabled=True,
        source_channel_id="-100123",
        source_channel_username="@kiwi_kiwi_test",
        destination_channel_id=None,
        destination_channel_username="@kiwi_kiwi_test",
        channel_script="stage_script.py",
        max_message_mb=10,
    )

    tg = FakeTelegramClient([update], b"")
    bale = FakeBaleClient()
    service = KiwiService(
        settings=settings,
        routes=_route_registry(route),
        telegram_client=tg,
        bale_client=bale,
        storage=StorageManager(settings.storage_dir),
        guard_runner=GuardRunner(settings.gaurd_scripts_dir, timeout_sec=5),
        script_runner=ScriptRunner(settings.scripts_dir, timeout_sec=5),
        state_store=StateStore(settings.state_path),
    )

    processed = asyncio.run(service.run_once())
    assert processed == 1
    assert bale.sent == [("@kiwi_kiwi_test", "sample out\n@kiwi_kiwi_test")]

    stage_logs = [text for chat, text in tg.audit_messages if chat == "@logchan"]
    assert any("بررسی گارد | موفق" in text for text in stage_logs)
    assert any("اجرای channel script | موفق" in text for text in stage_logs)
    assert any("stage_output:" in text for text in stage_logs)
    assert any("sample out" in text for text in stage_logs)
