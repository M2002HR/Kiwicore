from __future__ import annotations

import json
from pathlib import Path

from kiwi.config import load_routes


def test_load_routes_by_id_and_username(tmp_path: Path) -> None:
    config_path = tmp_path / "channels.json"
    config_path.write_text(
        json.dumps(
            [
                {
                    "name": "r1",
                    "source_channel_id": "-1001",
                    "destination_channel_id": "-2001",
                    "script": "-1001.py",
                },
                {
                    "name": "r2",
                    "source_channel_username": "MyChan",
                    "destination_channel_id": "-2002",
                    "script": "my.py",
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
                    "script": "id.py",
                },
                {
                    "name": "by-username",
                    "source_channel_username": "@usd_iran",
                    "destination_channel_username": "@usd_iran",
                    "script": "user.py",
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
                    "script": "my.py",
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
