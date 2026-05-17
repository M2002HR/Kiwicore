from __future__ import annotations

import json
import asyncio
from pathlib import Path

from kiwi.management_api import ManagementApi
from kiwi.sync_ledger import SyncLedger


def test_management_api_crud_and_reload(tmp_path: Path) -> None:
    channels_path = tmp_path / "config" / "channels.json"
    channels_path.parent.mkdir(parents=True, exist_ok=True)
    channels_path.write_text("[]", encoding="utf-8")
    scripts_dir = tmp_path / "scripts"
    guards_dir = tmp_path / "guards"
    scripts_dir.mkdir()
    guards_dir.mkdir()
    (scripts_dir / "default_channel_script.py").write_text("# x", encoding="utf-8")
    (guards_dir / "default_guard.py").write_text("# x", encoding="utf-8")

    seen = {"count": 0}

    def _reloaded(registry):
        seen["count"] += 1
        assert registry is not None

    api = ManagementApi(
        channels_config_path=str(channels_path),
        scripts_dir=str(scripts_dir),
        gaurd_scripts_dir=str(guards_dir),
        on_routes_reloaded=_reloaded,
    )

    created = api.add_route(
        {
            "name": "r1",
            "enabled": True,
            "source_channel_id": "-1001",
            "destination_channel_id": "-2001",
            "channel_script": "default_channel_script.py",
            "gaurd_script": "default_guard.py",
        }
    )
    assert created["name"] == "r1"
    assert len(api.list_routes()) == 1

    updated = api.update_route("r1", {"max_message_mb": 33})
    assert updated["max_message_mb"] == 33

    disabled = api.set_route_enabled("r1", False)
    assert disabled["status"] == "deactive"
    assert seen["count"] >= 3

    assert api.list_script_files() == ["default_channel_script.py"]
    assert api.list_guard_files() == ["default_guard.py"]

    sync_started = api.start_route_sync("r1")
    assert sync_started["status"] in {"syncing", "synced"}

    sync_updated = api.update_route_sync("r1", {"interval_sec": 120, "batch_size": 2})
    assert sync_updated["interval_sec"] == 120
    assert sync_updated["batch_size"] == 2

    sync_stopped = api.stop_route_sync("r1")
    assert sync_stopped["status"] == "deactive"

    api.delete_route("r1")
    assert api.list_routes() == []


def test_sync_stats_includes_route_progress_and_remaining(tmp_path: Path) -> None:
    channels_path = tmp_path / "config" / "channels.json"
    channels_path.parent.mkdir(parents=True, exist_ok=True)
    channels_path.write_text(
        json.dumps(
            [
                {
                    "name": "r1",
                    "enabled": True,
                    "source_channel_id": "-1001",
                    "destination_channel_id": "-2001",
                    "channel_script": "default_channel_script.py",
                    "gaurd_script": "default_guard.py",
                }
            ]
        ),
        encoding="utf-8",
    )
    scripts_dir = tmp_path / "scripts"
    guards_dir = tmp_path / "guards"
    scripts_dir.mkdir()
    guards_dir.mkdir()
    (scripts_dir / "default_channel_script.py").write_text("# x", encoding="utf-8")
    (guards_dir / "default_guard.py").write_text("# x", encoding="utf-8")

    ledger = SyncLedger(str(tmp_path / "app_data" / "sync_ledger.sqlite3"))
    payload = {"source_channel_id": "-1001", "message_id": 1}

    _, key1, _ = ledger.register_message(route_name="r1", source_channel_id="-1001", message_id=1, media_group_id=None, payload=payload)
    ledger.mark_status(key1, status="sent")
    ledger.register_message(route_name="r1", source_channel_id="-1001", message_id=2, media_group_id=None, payload=payload)
    _, key3, _ = ledger.register_message(route_name="r1", source_channel_id="-1001", message_id=3, media_group_id=None, payload=payload)
    ledger.mark_processing(key3)
    _, key4, _ = ledger.register_message(route_name="r1", source_channel_id="-1001", message_id=4, media_group_id=None, payload=payload)
    ledger.mark_status(key4, status="failed")
    _, key5, _ = ledger.register_message(route_name="r1", source_channel_id="-1001", message_id=5, media_group_id=None, payload=payload)
    ledger.mark_status(key5, status="skipped")

    api = ManagementApi(
        channels_config_path=str(channels_path),
        scripts_dir=str(scripts_dir),
        gaurd_scripts_dir=str(guards_dir),
        sync_ledger=ledger,
        sync_queue=None,
        on_routes_reloaded=lambda _registry: None,
    )

    stats = asyncio.run(api.sync_stats())
    metrics = stats["route_metrics"]["r1"]
    assert metrics["remaining_unsynced"] == 3
    assert metrics["done"] == 2
    assert metrics["total_seen"] == 5
    assert metrics["progress_pct"] == 40.0


def test_sync_stats_snapshot_uses_queued_estimate_without_async_queue_calls(tmp_path: Path) -> None:
    channels_path = tmp_path / "config" / "channels.json"
    channels_path.parent.mkdir(parents=True, exist_ok=True)
    channels_path.write_text("[]", encoding="utf-8")
    scripts_dir = tmp_path / "scripts"
    guards_dir = tmp_path / "guards"
    scripts_dir.mkdir()
    guards_dir.mkdir()
    (scripts_dir / "default_channel_script.py").write_text("# x", encoding="utf-8")
    (guards_dir / "default_guard.py").write_text("# x", encoding="utf-8")

    ledger = SyncLedger(str(tmp_path / "app_data" / "sync_ledger.sqlite3"))
    payload = {"source_channel_id": "-1001", "message_id": 1}
    ledger.register_message(route_name="r1", source_channel_id="-1001", message_id=1, media_group_id=None, payload=payload)
    ledger.register_message(route_name="r1", source_channel_id="-1001", message_id=2, media_group_id=None, payload=payload)

    class _QueueMustNotBeCalled:
        async def depth(self) -> int:
            raise AssertionError("sync_stats_snapshot must not call async queue depth")

    api = ManagementApi(
        channels_config_path=str(channels_path),
        scripts_dir=str(scripts_dir),
        gaurd_scripts_dir=str(guards_dir),
        sync_ledger=ledger,
        sync_queue=_QueueMustNotBeCalled(),
        on_routes_reloaded=lambda _registry: None,
    )

    snap = api.sync_stats_snapshot()
    assert snap["queue_depth"] == 2
    assert int(snap["status_counts"].get("queued", 0) or 0) == 2


def test_sync_stats_does_not_probe_source_by_default(tmp_path: Path) -> None:
    channels_path = tmp_path / "config" / "channels.json"
    channels_path.parent.mkdir(parents=True, exist_ok=True)
    channels_path.write_text(
        json.dumps(
            [
                {
                    "name": "r1",
                    "source_channel_username": "@src1",
                    "destination_channel_id": "-2001",
                    "channel_script": "default_channel_script.py",
                    "gaurd_script": "default_guard.py",
                    "status": "syncing",
                }
            ]
        ),
        encoding="utf-8",
    )
    scripts_dir = tmp_path / "scripts"
    guards_dir = tmp_path / "guards"
    scripts_dir.mkdir()
    guards_dir.mkdir()
    (scripts_dir / "default_channel_script.py").write_text("# x", encoding="utf-8")
    (guards_dir / "default_guard.py").write_text("# x", encoding="utf-8")

    ledger = SyncLedger(str(tmp_path / "app_data" / "sync_ledger.sqlite3"))
    payload = {"source_channel_id": "-1001", "message_id": 1}
    ledger.register_message(route_name="r1", source_channel_id="-1001", message_id=1, media_group_id=None, payload=payload)

    class _SourceMustNotBeCalled:
        async def latest_message_id_for_route(self, route):  # noqa: ARG002
            raise AssertionError("sync_stats must not probe source by default")

    api = ManagementApi(
        channels_config_path=str(channels_path),
        scripts_dir=str(scripts_dir),
        gaurd_scripts_dir=str(guards_dir),
        sync_ledger=ledger,
        sync_queue=None,
        source_client=_SourceMustNotBeCalled(),
        on_routes_reloaded=lambda _registry: None,
    )

    stats = asyncio.run(api.sync_stats())
    assert "r1" in stats["route_metrics"]
    assert int(stats["route_metrics"]["r1"]["remaining_unsynced"]) == 1


def test_start_route_sync_does_not_probe_source_client(tmp_path: Path) -> None:
    channels_path = tmp_path / "config" / "channels.json"
    channels_path.parent.mkdir(parents=True, exist_ok=True)
    channels_path.write_text(
        json.dumps(
            [
                {
                    "name": "r1",
                    "status": "deactive",
                    "source_channel_username": "@src1",
                    "destination_channel_id": "-2001",
                    "channel_script": "default_channel_script.py",
                    "gaurd_script": "default_guard.py",
                }
            ]
        ),
        encoding="utf-8",
    )
    scripts_dir = tmp_path / "scripts"
    guards_dir = tmp_path / "guards"
    scripts_dir.mkdir()
    guards_dir.mkdir()
    (scripts_dir / "default_channel_script.py").write_text("# x", encoding="utf-8")
    (guards_dir / "default_guard.py").write_text("# x", encoding="utf-8")

    class _SourceMustNotBeCalled:
        async def latest_message_id_for_route(self, route):  # noqa: ARG002
            raise AssertionError("start_route_sync must not probe source client")

    ledger = SyncLedger(str(tmp_path / "app_data" / "sync_ledger.sqlite3"))
    api = ManagementApi(
        channels_config_path=str(channels_path),
        scripts_dir=str(scripts_dir),
        gaurd_scripts_dir=str(guards_dir),
        sync_ledger=ledger,
        sync_queue=None,
        source_client=_SourceMustNotBeCalled(),
        on_routes_reloaded=lambda _registry: None,
    )

    started = api.start_route_sync("r1")
    assert started["status"] == "syncing"

    ledger.set_route_checkpoint("r1", 123)
    started_again = api.start_route_sync("r1")
    assert started_again["status"] == "synced"


def test_sync_ledger_route_checkpoint_is_monotonic(tmp_path: Path) -> None:
    ledger = SyncLedger(str(tmp_path / "app_data" / "sync_ledger.sqlite3"))
    ledger.set_route_checkpoint("r1", 123)
    assert ledger.get_route_checkpoint("r1") == 123

    # Lower values must never move checkpoint backwards.
    ledger.set_route_checkpoint("r1", 100)
    assert ledger.get_route_checkpoint("r1") == 123

    # Higher value should advance normally.
    ledger.set_route_checkpoint("r1", 140)
    assert ledger.get_route_checkpoint("r1") == 140
