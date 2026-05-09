from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def _run_default(payload: dict, tmp_path: Path) -> dict:
    payload_path = tmp_path / "payload.json"
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    payload_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    script = Path(__file__).resolve().parents[1] / "scripts" / "channel_scripts" / "default_scripts.py"
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
    )
    return json.loads(result.stdout)


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
