from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


def _script_path() -> Path:
    return Path(__file__).resolve().parents[1] / "scripts" / "channel_scripts" / "wallpaper.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("wallpaper_script_test_module", _script_path())
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_script(payload: dict, tmp_path: Path) -> dict:
    payload_path = tmp_path / "payload.json"
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    payload_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(_script_path()),
            "--payload",
            str(payload_path),
            "--input-dir",
            str(input_dir),
            "--output-dir",
            str(output_dir),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_wallpaper_script_filters_to_images_only_and_keeps_album_order(tmp_path: Path) -> None:
    mod = _load_module()
    payload = {
        "route": {
            "destination_channel_username": "@hd_ultra_wallpapers",
            "destination_target": "@hd_ultra_wallpapers",
        },
        "inputs": [
            {"kind": "photo", "local_name": "a.jpg"},
            {"kind": "document", "local_name": "b.png", "mime_type": "image/png"},
            {"kind": "video", "local_name": "c.mp4", "mime_type": "video/mp4"},
            {"kind": "document", "local_name": "d.zip", "mime_type": "application/zip"},
            {"kind": "document", "local_name": "e.webp", "file_name": "e.webp"},
        ],
    }

    out = mod.build_messages(payload, input_dir=tmp_path)
    assert [item["path"] for item in out] == ["a.jpg"]
    assert all(item["type"] == "photo" for item in out)


def test_wallpaper_script_adds_link_caption_to_first_media_only(tmp_path: Path) -> None:
    mod = _load_module()
    payload = {
        "route": {
            "destination_channel_username": "@wallpapersarena",
            "destination_target": "@wallpapersarena",
        },
        "inputs": [
            {"kind": "photo", "local_name": "x.jpg"},
            {"kind": "photo", "local_name": "y.jpg"},
        ],
    }

    out = mod.build_messages(payload, input_dir=tmp_path)
    assert len(out) == 2
    assert "caption" in out[0]
    assert "caption" not in out[1]
    assert out[0]["caption"] == "به چنل سرزمین والپیپر بپیوندید..."
    assert out[0]["append_destination_footer"] is False


def test_wallpaper_script_keeps_hashtags_before_join_message(tmp_path: Path) -> None:
    mod = _load_module()
    payload = {
        "message": {
            "caption": "#Vaporwave #NeonCity \n@WallpapersArena",
        },
        "inputs": [
            {"kind": "photo", "local_name": "x.jpg"},
            {"kind": "photo", "local_name": "y.jpg"},
        ],
    }

    out = mod.build_messages(payload, input_dir=tmp_path)
    assert len(out) == 2
    assert out[0]["caption"] == "#Vaporwave #NeonCity\nبه چنل سرزمین والپیپر بپیوندید..."
    assert out[0]["append_destination_footer"] is False


def test_wallpaper_script_returns_empty_when_no_images(tmp_path: Path) -> None:
    mod = _load_module()
    payload = {
        "route": {"destination_target": "@dest"},
        "inputs": [
            {"kind": "video", "local_name": "clip.mp4"},
            {"kind": "document", "local_name": "file.pdf", "mime_type": "application/pdf"},
        ],
    }
    assert mod.build_messages(payload, input_dir=tmp_path) == []


def test_wallpaper_script_cli_outputs_json(tmp_path: Path) -> None:
    payload = {
        "route": {
            "destination_channel_username": "@hd_ultra_wallpapers",
            "destination_target": "@hd_ultra_wallpapers",
        },
        "inputs": [
            {"kind": "document", "local_name": "k.jpg", "mime_type": "image/jpeg"},
        ],
    }

    out = _run_script(payload, tmp_path)
    assert out == {"messages": []}
