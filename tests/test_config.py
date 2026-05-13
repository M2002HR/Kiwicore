from __future__ import annotations

import json
from pathlib import Path

from kiwi.config import load_routes, load_settings


def test_load_routes_by_id_and_username(tmp_path: Path) -> None:
    config_path = tmp_path / "channels.json"
    config_path.write_text(
        json.dumps(
            [
                {
                    "name": "r1",
                    "source_channel_id": "-1001",
                    "destination_channel_id": "-2001",
                    "channel_script": "-1001.py",
                },
                {
                    "name": "r2",
                    "source_channel_username": "MyChan",
                    "destination_channel_id": "-2002",
                    "channel_script": "my.py",
                },
            ]
        ),
        encoding="utf-8",
    )

    registry = load_routes(str(config_path))
    assert registry.match("-1001", None) is not None
    assert registry.match("-999", "@mychan") is not None


def test_destination_target_prefers_username(tmp_path: Path) -> None:
    config_path = tmp_path / "channels.json"
    config_path.write_text(
        json.dumps(
            [
                {
                    "source_channel_username": "@usd_iran",
                    "destination_channel_username": "@usd_iran",
                    "destination_channel_id": "-2002",
                }
            ]
        ),
        encoding="utf-8",
    )

    registry = load_routes(str(config_path))
    route = registry.match("-1", "@usd_iran")
    assert route is not None
    assert route.destination_target() == "@usd_iran"


def test_load_routes_default_script_name(tmp_path: Path) -> None:
    config_path = tmp_path / "channels.json"
    config_path.write_text(
        json.dumps(
            [
                {
                    "source_channel_id": "-100777",
                    "destination_channel_id": "-200777",
                }
            ]
        ),
        encoding="utf-8",
    )

    registry = load_routes(str(config_path))
    route = registry.match("-100777", None)
    assert route is not None
    assert route.script == "-100777.py"
    assert route.gaurd_script == "default_guard.py"


def test_load_routes_default_script_name_prefers_username(tmp_path: Path) -> None:
    config_path = tmp_path / "channels.json"
    config_path.write_text(
        json.dumps(
            [
                {
                    "source_channel_id": "-100888",
                    "source_channel_username": "@usd_iran",
                    "destination_channel_id": "-200888",
                }
            ]
        ),
        encoding="utf-8",
    )

    registry = load_routes(str(config_path))
    route = registry.match("-100888", "@usd_iran")
    assert route is not None
    assert route.script == "usd_iran.py"


def test_match_prefers_username_over_id(tmp_path: Path) -> None:
    config_path = tmp_path / "channels.json"
    config_path.write_text(
        json.dumps(
            [
                {
                    "name": "by-id",
                    "source_channel_id": "-100123",
                    "destination_channel_id": "-2001",
                    "channel_script": "id.py",
                },
                {
                    "name": "by-username",
                    "source_channel_username": "@usd_iran",
                    "destination_channel_username": "@usd_iran",
                    "channel_script": "user.py",
                },
            ]
        ),
        encoding="utf-8",
    )
    registry = load_routes(str(config_path))
    matched = registry.match("-100123", "@usd_iran")
    assert matched is not None
    assert matched.name == "by-username"


def test_load_routes_custom_gaurd_script(tmp_path: Path) -> None:
    config_path = tmp_path / "channels.json"
    config_path.write_text(
        json.dumps(
            [
                {
                    "source_channel_id": "-1001",
                    "destination_channel_id": "-2001",
                    "channel_script": "my.py",
                    "gaurd_script": "my_guard.py",
                }
            ]
        ),
        encoding="utf-8",
    )

    registry = load_routes(str(config_path))
    route = registry.match("-1001", None)
    assert route is not None
    assert route.gaurd_script == "my_guard.py"


def test_load_routes_allows_null_scripts(tmp_path: Path) -> None:
    config_path = tmp_path / "channels.json"
    config_path.write_text(
        json.dumps(
            [
                {
                    "source_channel_id": "-1001",
                    "destination_channel_id": "-2001",
                    "channel_script": None,
                    "gaurd_script": None,
                }
            ]
        ),
        encoding="utf-8",
    )
    registry = load_routes(str(config_path))
    route = registry.match("-1001", None)
    assert route is not None
    assert route.channel_script is None
    assert route.gaurd_script is None


def test_load_settings_adds_private_updates_for_admin_bot(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "TELEGRAM_BOT_TOKEN=t",
                "BALE_BOT_TOKEN=b",
                "TELEGRAM_ALLOWED_UPDATES=[\"channel_post\"]",
                "ADMIN_BOT_ENABLED=true",
            ]
        ),
        encoding="utf-8",
    )
    settings = load_settings(str(env_path))
    assert "channel_post" in settings.telegram_allowed_updates
    assert "message" in settings.telegram_allowed_updates
    assert "edited_message" in settings.telegram_allowed_updates
    assert "callback_query" in settings.telegram_allowed_updates


def test_load_settings_telethon_mode_requires_credentials(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "TELEGRAM_BOT_TOKEN=t",
                "BALE_BOT_TOKEN=b",
                "TELEGRAM_SOURCE_MODE=telethon",
            ]
        ),
        encoding="utf-8",
    )
    try:
        load_settings(str(env_path))
        assert False, "expected ValueError"
    except ValueError as exc:
        assert "TELETHON_API_ID" in str(exc)


def test_load_settings_telethon_mode_keeps_admin_updates_only(tmp_path: Path) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text(
        "\n".join(
            [
                "TELEGRAM_BOT_TOKEN=t",
                "BALE_BOT_TOKEN=b",
                "TELEGRAM_SOURCE_MODE=telethon",
                "TELETHON_API_ID=1234",
                "TELETHON_API_HASH=abc",
                "TELEGRAM_ALLOWED_UPDATES=[\"channel_post\",\"message\",\"edited_message\"]",
            ]
        ),
        encoding="utf-8",
    )
    settings = load_settings(str(env_path))
    assert settings.telethon_enabled is True
    assert settings.telegram_allowed_updates == ["message", "edited_message", "callback_query"]


def test_load_routes_allows_duplicate_sources_and_matches_all(tmp_path: Path) -> None:
    config_path = tmp_path / "channels.json"
    config_path.write_text(
        json.dumps(
            [
                {
                    "name": "r1",
                    "source_channel_username": "@dup_src",
                    "destination_channel_username": "@dst1",
                    "channel_script": "s1.py",
                },
                {
                    "name": "r2",
                    "source_channel_username": "@dup_src",
                    "destination_channel_username": "@dst2",
                    "channel_script": "s2.py",
                },
            ]
        ),
        encoding="utf-8",
    )

    registry = load_routes(str(config_path))
    matches = registry.match_all("-100000", "@dup_src")
    assert len(matches) == 2
    assert [m.name for m in matches] == ["r1", "r2"]


def test_load_routes_reads_sync_settings_and_allows_syncing_when_disabled(tmp_path: Path) -> None:
    config_path = tmp_path / "channels.json"
    config_path.write_text(
        json.dumps(
            [
                {
                    "name": "sync-r1",
                    "enabled": False,
                    "source_channel_id": "-1001",
                    "destination_channel_id": "-2001",
                    "channel_script": "s.py",
                    "sync": {
                        "enabled": True,
                        "status": "syncing",
                        "backfill_count": 77,
                        "interval_sec": 120,
                        "batch_size": 3,
                        "retry_attempts": 4,
                        "pending_count": 9,
                        "processed_count": 6,
                        "seeded": True,
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    registry = load_routes(str(config_path))
    matched = registry.match("-1001", None)
    assert matched is not None
    assert matched.is_syncing() is True
    assert matched.sync_backfill_count == 77
    assert matched.sync_interval_sec == 120
    assert matched.sync_batch_size == 3
    assert matched.sync_retry_attempts == 4
    assert matched.sync_pending_count == 9
    assert matched.sync_processed_count == 6
    assert matched.sync_seeded is True


def test_load_routes_sync_active_but_not_seeded_treated_as_syncing(tmp_path: Path) -> None:
    config_path = tmp_path / "channels.json"
    config_path.write_text(
        json.dumps(
            [
                {
                    "name": "sync-r2",
                    "enabled": True,
                    "source_channel_username": "@s2",
                    "destination_channel_id": "-2002",
                    "channel_script": "s2.py",
                    "sync": {
                        "enabled": True,
                        "status": "active",
                        "seeded": False,
                    },
                }
            ]
        ),
        encoding="utf-8",
    )
    route = load_routes(str(config_path)).match("-1", "@s2")
    assert route is not None
    assert route.is_syncing() is True
