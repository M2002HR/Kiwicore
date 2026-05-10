from __future__ import annotations

import asyncio
import json
from pathlib import Path

from kiwi.config import RouteRegistry, Settings
from kiwi.errors import MessageTooLargeError, PlatformApiError
from kiwi.guard_runner import GuardRunner
from kiwi.script_runner import ScriptRunner
from kiwi.service import KiwiService
from kiwi.state import StateStore
from kiwi.storage import StorageManager
from kiwi.types import ChannelRoute


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

    async def send_message(self, chat_id: str, text: str):
        self.audit_messages.append((chat_id, text))
        return {"ok": True}

    async def aclose(self) -> None:
        return None


class FakeBaleClient:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.media_group_sent: list[tuple[str, int]] = []

    async def send_message(self, chat_id: str, text: str):
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
    )


def _route_registry(route: ChannelRoute) -> RouteRegistry:
    by_id = {}
    by_username = {}
    if route.source_channel_id:
        by_id[route.source_channel_id] = route
    if route.source_channel_username:
        by_username[route.source_channel_username] = route
    return RouteRegistry(routes=[route], by_channel_id=by_id, by_channel_username=by_username)


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
        script="-1001.py",
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
    assert bale.sent == [("-2001", "OUT:hello")]

    state = json.loads(Path(settings.state_path).read_text(encoding="utf-8"))
    assert state["offset"] == 102


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
        script="-1001.py",
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
        script="-1001.py",
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
            "chat": {"id": -1001, "type": "channel"},
            "text": "hello",
        },
    }

    settings = _settings(tmp_path)
    guard_path = Path(settings.gaurd_scripts_dir) / "deny.py"
    guard_path.write_text("print('false')", encoding="utf-8")
    route = ChannelRoute(
        name="r",
        enabled=True,
        source_channel_id="-1001",
        source_channel_username=None,
        destination_channel_id="-2001",
        destination_channel_username=None,
        script="-1001.py",
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
        script="-1001.py",
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
        script="-1001.py",
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
    assert bale.sent == [("-2001", "COUNT:2")]


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
        script="-1001.py",
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
    assert bale.sent == [("-2001", "COUNT:2")]


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
        script="-1001.py",
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
    assert bale.sent == [("-2001", "ok")]
    assert len(tg.audit_messages) >= 1
    assert all(chat == "@logchan" for chat, _ in tg.audit_messages)
    assert any("کیوی" in text for _, text in tg.audit_messages)


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
        script="-1001.py",
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
