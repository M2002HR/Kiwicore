from __future__ import annotations

import json
from pathlib import Path

from kiwi.management_api import ManagementApi


def test_management_api_crud_and_reload(tmp_path: Path) -> None:
    channels_path = tmp_path / "config" / "channels.json"
    channels_path.parent.mkdir(parents=True, exist_ok=True)
    channels_path.write_text("[]", encoding="utf-8")
    scripts_dir = tmp_path / "scripts"
    guards_dir = tmp_path / "guards"
    scripts_dir.mkdir()
    guards_dir.mkdir()
    (scripts_dir / "default_scripts.py").write_text("# x", encoding="utf-8")
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
            "script": "default_scripts.py",
            "gaurd_script": "default_guard.py",
        }
    )
    assert created["name"] == "r1"
    assert len(api.list_routes()) == 1

    updated = api.update_route("r1", {"max_message_mb": 33})
    assert updated["max_message_mb"] == 33

    disabled = api.set_route_enabled("r1", False)
    assert disabled["enabled"] is False
    assert seen["count"] >= 3

    assert api.list_script_files() == ["default_scripts.py"]
    assert api.list_guard_files() == ["default_guard.py"]

    sync_started = api.start_route_sync("r1")
    assert isinstance(sync_started.get("sync"), dict)
    assert sync_started["sync"]["status"] == "syncing"

    sync_updated = api.update_route_sync("r1", {"interval_sec": 120, "batch_size": 2})
    assert sync_updated["sync"]["interval_sec"] == 120
    assert sync_updated["sync"]["batch_size"] == 2

    sync_stopped = api.stop_route_sync("r1")
    assert sync_stopped["sync"]["enabled"] is False

    api.delete_route("r1")
    assert api.list_routes() == []
