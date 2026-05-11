from __future__ import annotations

import json
import os
import subprocess
import sys
import importlib.util
from pathlib import Path


def _run_default(payload: dict, tmp_path: Path) -> dict:
    payload_path = tmp_path / "payload.json"
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    payload_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    script = Path(__file__).resolve().parents[1] / "scripts" / "channel_scripts" / "default_channel_script.py"
    env = os.environ.copy()
    env["SCRIPT_CLEAN_AI_ENABLED"] = "false"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
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
        env=env,
    )
    return json.loads(result.stdout)


def _load_default_module():
    script = Path(__file__).resolve().parents[1] / "scripts" / "channel_scripts" / "default_channel_script.py"
    spec = importlib.util.spec_from_file_location("kiwi_default_script_test_module", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_default_script_text_passthrough(tmp_path: Path) -> None:
    payload = {
        "message": {"text": "hello"},
        "inputs": [],
    }
    out = _run_default(payload, tmp_path)
    assert out == {"messages": [{"type": "text", "text": "hello"}]}


def test_default_script_media_caption_passthrough(tmp_path: Path) -> None:
    payload = {
        "message": {"caption": "cap"},
        "inputs": [
            {"kind": "photo", "local_name": "input_1_photo.jpg"},
        ],
    }
    out = _run_default(payload, tmp_path)
    assert out["messages"][0]["type"] == "photo"
    assert out["messages"][0]["path"] == "input_1_photo.jpg"
    assert out["messages"][0]["caption"] == "cap"


def test_default_script_sticker_passthrough(tmp_path: Path) -> None:
    payload = {
        "message": {},
        "inputs": [
            {"kind": "sticker", "local_name": "input_1_sticker.webp"},
        ],
    }
    out = _run_default(payload, tmp_path)
    assert out == {"messages": []}


def test_default_script_keeps_media_with_text_in_single_flow(tmp_path: Path) -> None:
    payload = {
        "message": {"text": "album text"},
        "inputs": [
            {"kind": "photo", "local_name": "a.jpg"},
            {"kind": "photo", "local_name": "b.jpg"},
        ],
    }
    out = _run_default(payload, tmp_path)
    assert out["messages"] == [
        {"type": "photo", "path": "a.jpg", "caption": "album text"},
        {"type": "photo", "path": "b.jpg"},
    ]


def test_default_script_maps_gif_like_document_to_animation(tmp_path: Path) -> None:
    payload = {
        "message": {"caption": "cap"},
        "inputs": [
            {
                "kind": "document",
                "local_name": "animation.gif.mp4",
                "file_name": "animation.gif.mp4",
                "mime_type": "video/mp4",
            }
        ],
    }
    out = _run_default(payload, tmp_path)
    assert out["messages"] == [{"type": "animation", "path": "animation.gif.mp4", "caption": "cap"}]


def test_default_script_removes_links_and_replaces_source_footer_with_destination_signature(tmp_path: Path) -> None:
    payload = {
        "route": {
            "source_channel_username": "@source_chan",
            "destination_channel_id": "-1009000",
            "destination_target": "@dest_chan",
        },
        "message": {
            "source_channel_username": "@source_chan",
            "text": "خبر فوری\n@source_chan\nhttps://t.me/source_chan/12 #tag",
        },
        "inputs": [],
    }
    out = _run_default(payload, tmp_path)
    assert out == {"messages": [{"type": "text", "text": "خبر فوری\n-1009000"}]}


def test_default_script_keeps_mentions_inside_core_lines(tmp_path: Path) -> None:
    payload = {
        "route": {"destination_target": "@dest"},
        "message": {
            "text": "⚽️ @jfball - just football\nhttps://t.me/any/1",
        },
        "inputs": [],
    }
    out = _run_default(payload, tmp_path)
    assert out == {"messages": [{"type": "text", "text": "⚽️ @jfball - just football\n@dest"}]}


def test_default_script_removes_blocklisted_emojis(tmp_path: Path) -> None:
    payload = {
        "route": {"destination_target": "@dest"},
        "message": {"text": "سلام 🇮🇱 🏳️‍⚧️ 🏳️‍🌈 👰 🤵"},
        "inputs": [],
    }
    out = _run_default(payload, tmp_path)
    assert out == {"messages": [{"type": "text", "text": "سلام"}]}


def test_default_script_removes_reference_context_phrase(tmp_path: Path) -> None:
    payload = {
        "route": {"destination_target": "@dest"},
        "message": {
            "text": "خبر مهم\nدر شبکه x منتشر شد https://x.com/a/1\nادامه تحلیل بازار",
        },
        "inputs": [],
    }
    out = _run_default(payload, tmp_path)
    assert out == {"messages": [{"type": "text", "text": "خبر مهم\nادامه تحلیل بازار\n@dest"}]}


def test_default_script_keeps_short_text_message(tmp_path: Path) -> None:
    payload = {
        "message": {"text": "س"},
        "inputs": [],
    }
    out = _run_default(payload, tmp_path)
    assert out == {"messages": [{"type": "text", "text": "س"}]}


def test_default_script_filters_noisy_ai_output_lines() -> None:
    mod = _load_default_module()
    original = "🏴 Rangers v Red Star Belgrade 🇷🇸\n@kiwi_kiwi_test"
    noisy = (
        "Goal: Keep only core informational content.\n"
        "Constraint: No explanations.\n"
        "`🇪🇸 Barcelona v Galatasaray 🇹🇷` -> Core information.\n"
        "🏴 Rangers v Red Star Belgrade 🇷🇸\n"
        "@kiwi_kiwi_test"
    )
    cleaned = mod._sanitize_ai_output(noisy, original=original)
    assert cleaned == "🏴 Rangers v Red Star Belgrade 🇷🇸\n@kiwi_kiwi_test"
