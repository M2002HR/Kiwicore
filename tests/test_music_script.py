from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path


def _script_path() -> Path:
    return Path(__file__).resolve().parents[1] / "scripts" / "channel_scripts" / "music.py"


def _load_module():
    path = _script_path()
    spec = importlib.util.spec_from_file_location("music_script_test_module", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_script(payload: dict, tmp_path: Path, *, env_patch: dict[str, str] | None = None) -> dict:
    payload_path = tmp_path / "payload.json"
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    payload_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    env = os.environ.copy()
    env["CHANNEL_SCRIPT_AI_ENABLED"] = "false"
    if env_patch:
        env.update(env_patch)

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
        env=env,
    )
    return json.loads(result.stdout)


def test_music_script_keeps_default_passthrough_when_ai_disabled(tmp_path: Path) -> None:
    payload = {
        "message": {"text": "hello"},
        "inputs": [],
    }
    out = _run_script(payload, tmp_path)
    assert out == {"messages": [{"type": "text", "text": "hello"}]}


def test_music_script_orders_media_as_poster_demo_track(tmp_path: Path) -> None:
    payload = {
        "message": {"caption": "demo caption"},
        "inputs": [
            {
                "kind": "audio",
                "local_name": "full_track.mp3",
                "file_name": "full_track.mp3",
                "mime_type": "audio/mpeg",
                "title": "Full Track",
                "performer": "Example Artist",
            },
            {"kind": "photo", "local_name": "poster.jpg", "file_name": "poster.jpg", "mime_type": "image/jpeg"},
            {"kind": "video", "local_name": "demo_clip.mp4", "file_name": "demo_clip.mp4", "mime_type": "video/mp4"},
        ],
    }
    out = _run_script(payload, tmp_path)
    assert out == {
        "messages": [
            {"type": "photo", "path": "poster.jpg", "caption": "🎵 Full Track\n🎤 Example Artist\n\ndemo caption"},
            {"type": "video", "path": "demo_clip.mp4"},
            {"type": "audio", "path": "full_track.mp3"},
        ]
    }


def test_music_script_moves_generated_caption_to_first_media_after_reorder(tmp_path: Path, monkeypatch) -> None:
    mod = _load_module()
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_ENABLED", "true")
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_ENDPOINT", "http://fake.local/proxy/gemini")

    def fake_call(*, endpoint: str, body: dict, timeout_sec: float):
        assert endpoint == "http://fake.local/proxy/gemini"
        return "کپشن نهایی موزیک"

    monkeypatch.setattr(mod, "_call_gemini_text", fake_call)

    payload = {
        "route": {"destination_target": "@iran_music_fa"},
        "message": {"caption": "old"},
        "inputs": [
            {
                "kind": "audio",
                "local_name": "song.mp3",
                "file_name": "song.mp3",
                "mime_type": "audio/mpeg",
                "title": "Night Call",
                "performer": "Arian",
            },
            {"kind": "photo", "local_name": "poster.jpg", "file_name": "poster.jpg", "mime_type": "image/jpeg"},
            {"kind": "video", "local_name": "preview.mp4", "file_name": "preview.mp4", "mime_type": "video/mp4"},
        ],
    }
    messages = mod.build_messages(payload, input_dir=tmp_path)
    assert messages == [
        {"type": "photo", "path": "poster.jpg", "caption": "🎵 Night Call\n🎤 Arian\n\nکپشن نهایی موزیک"},
        {"type": "video", "path": "preview.mp4"},
        {"type": "audio", "path": "song.mp3"},
    ]


def test_music_script_reorders_in_fail_open_path(tmp_path: Path) -> None:
    payload = {
        "message": {"caption": "raw"},
        "inputs": [
            {
                "kind": "audio",
                "local_name": "song.mp3",
                "file_name": "song.mp3",
                "mime_type": "audio/mpeg",
                "title": "Morning Light",
                "performer": "Mitra",
            },
            {"kind": "photo", "local_name": "cover.jpg", "file_name": "cover.jpg", "mime_type": "image/jpeg"},
        ],
    }
    out = _run_script(
        payload,
        tmp_path,
        env_patch={
            "CHANNEL_SCRIPT_AI_ENABLED": "true",
            "CHANNEL_SCRIPT_AI_ENDPOINT": "http://127.0.0.1:9/nowhere",
            "CHANNEL_SCRIPT_AI_MANDATORY": "false",
            "CHANNEL_SCRIPT_AI_FAIL_OPEN": "true",
            "CHANNEL_SCRIPT_AI_RETRY_COUNT": "0",
            "CHANNEL_SCRIPT_AI_TIMEOUT_SEC": "0.2",
        },
    )
    assert out == {
        "messages": [
            {"type": "photo", "path": "cover.jpg", "caption": "🎵 Morning Light\n🎤 Mitra\n\nraw"},
            {"type": "audio", "path": "song.mp3"},
        ]
    }


def test_music_script_blocks_audio_without_metadata(tmp_path: Path) -> None:
    payload = {
        "message": {"caption": "raw"},
        "inputs": [
            {"kind": "audio", "local_name": "song.mp3", "file_name": "song.mp3", "mime_type": "audio/mpeg"},
            {"kind": "photo", "local_name": "cover.jpg", "file_name": "cover.jpg", "mime_type": "image/jpeg"},
        ],
    }
    out = _run_script(payload, tmp_path)
    assert out == {"messages": []}


def test_music_script_blocks_blacklisted_artist(tmp_path: Path) -> None:
    payload = {
        "message": {"caption": "raw"},
        "inputs": [
            {
                "kind": "audio",
                "local_name": "song.mp3",
                "file_name": "song.mp3",
                "mime_type": "audio/mpeg",
                "title": "Track Name",
                "performer": "Toomaj Salehi",
            },
            {"kind": "photo", "local_name": "cover.jpg", "file_name": "cover.jpg", "mime_type": "image/jpeg"},
        ],
    }
    out = _run_script(payload, tmp_path)
    assert out == {"messages": []}
