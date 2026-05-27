from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path


def _script_path() -> Path:
    return Path(__file__).resolve().parents[1] / "scripts" / "channel_scripts" / "photo_prompt.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("photo_prompt_script_test_module", _script_path())
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


def test_photo_prompt_script_passes_prompt_photo_post(tmp_path: Path) -> None:
    mod = _load_module()
    payload = {
        "message": {
            "text": "Prompt: cinematic portrait, 85mm lens, depth of field, skin texture, photorealistic",
            "caption": "",
        },
        "inputs": [
            {"kind": "photo", "local_name": "a.jpg"},
            {"kind": "photo", "local_name": "b.jpg"},
        ],
    }
    out = mod.build_messages(payload, input_dir=tmp_path)
    assert len(out) == 2
    assert all(item["type"] == "photo" for item in out)
    assert out[0]["caption"].startswith("Prompt:")


def test_photo_prompt_script_blocks_without_prompt_text(tmp_path: Path) -> None:
    mod = _load_module()
    payload = {
        "message": {"text": "اطلاعیه: قابلیت جدید اضافه شد", "caption": ""},
        "inputs": [{"kind": "photo", "local_name": "a.jpg"}],
    }
    assert mod.build_messages(payload, input_dir=tmp_path) == []


def test_photo_prompt_script_blocks_without_image(tmp_path: Path) -> None:
    mod = _load_module()
    payload = {
        "message": {"text": "Prompt: cinematic portrait with dramatic light", "caption": ""},
        "inputs": [{"kind": "video", "local_name": "v.mp4"}],
    }
    assert mod.build_messages(payload, input_dir=tmp_path) == []


def test_photo_prompt_script_cli_outputs_json(tmp_path: Path) -> None:
    payload = {
        "message": {"text": "Prompt: portrait, cinematic lighting, realistic skin texture", "caption": ""},
        "inputs": [{"kind": "photo", "local_name": "a.jpg"}],
    }
    out = _run_script(payload, tmp_path)
    assert isinstance(out, dict)
    assert isinstance(out.get("messages"), list)
    assert len(out["messages"]) == 1
    first = out["messages"][0]
    assert first["type"] == "photo"
    assert first["path"] == "a.jpg"
    assert "Prompt: portrait, cinematic lighting, realistic skin texture" in str(first.get("caption") or "")


def test_photo_prompt_script_adds_gender_buttons_and_token_store(tmp_path: Path, monkeypatch) -> None:
    mod = _load_module()
    store_path = tmp_path / "prompt_tokens.json"
    monkeypatch.setenv("PHOTO_PROMPT_TOKEN_STORE_PATH", str(store_path))
    monkeypatch.setenv("PHOTO_PROMPT_BOT_DEEPLINK_BASE", "https://ble.ir/pirashki_bot?start=")
    payload = {
        "message": {
            "text": (
                "نسخه زنانه:\n\n"
                "ultra realistic female portrait, studio lighting, depth of field, cinematic\n\n"
                "برای استفاده مستقیم و رایگان نسخه زنانه این پرامپت کلیک کنید و عکستون رو بفرستید\n\n"
                "نسخه مردانه:\n\n"
                "ultra realistic male portrait, studio lighting, depth of field, cinematic\n\n"
                "برای استفاده مستقیم و رایگان نسخه مردانه این پرامپت کلیک کنید و عکستون رو بفرستید"
            ),
            "caption": "",
        },
        "inputs": [{"kind": "photo", "local_name": "a.jpg"}],
    }
    out = mod.build_messages(payload, input_dir=tmp_path)
    assert len(out) == 1
    markup = out[0].get("reply_markup")
    assert isinstance(markup, dict)
    rows = markup.get("inline_keyboard")
    assert isinstance(rows, list) and rows and isinstance(rows[0], list)
    assert len(rows[0]) == 2
    assert rows[0][0]["text"] == "نسخه زنانه"
    assert rows[0][1]["text"] == "نسخه مردانه"
    assert str(rows[0][0]["url"]).startswith("https://ble.ir/pirashki_bot?start=pp_")
    assert str(rows[0][1]["url"]).startswith("https://ble.ir/pirashki_bot?start=pp_")
    assert "ساخت عکس با این پرامپت" in str(out[0].get("caption") or "")
    assert store_path.exists()
    stored = json.loads(store_path.read_text(encoding="utf-8"))
    assert isinstance(stored, dict)
    assert len(stored) >= 2


def test_photo_prompt_script_adds_single_prompt_button(tmp_path: Path, monkeypatch) -> None:
    mod = _load_module()
    store_path = tmp_path / "prompt_tokens.json"
    monkeypatch.setenv("PHOTO_PROMPT_TOKEN_STORE_PATH", str(store_path))
    monkeypatch.setenv("PHOTO_PROMPT_BOT_DEEPLINK_BASE", "https://ble.ir/pirashki_bot?start=")
    payload = {
        "message": {
            "text": (
                "متن پرامپت:\n\n"
                "ultra realistic portrait, studio lighting, depth of field, skin texture, cinematic look\n\n"
                "برای استفاده مستقیم و رایگان این پرامپت کلیک کنید و عکستون رو بفرستید\n\n"
                "@AiFreeRoPrompt"
            ),
            "caption": "",
        },
        "inputs": [{"kind": "photo", "local_name": "a.jpg"}],
    }
    out = mod.build_messages(payload, input_dir=tmp_path)
    assert len(out) == 1
    msg = out[0]
    markup = msg.get("reply_markup")
    assert isinstance(markup, dict)
    rows = markup.get("inline_keyboard")
    assert isinstance(rows, list) and rows and isinstance(rows[0], list)
    assert len(rows[0]) == 1
    assert rows[0][0]["text"] == "👁 استفاده از پرامپت"
    assert str(rows[0][0]["url"]).startswith("https://ble.ir/pirashki_bot?start=pp_")
    caption = str(msg.get("caption") or "")
    assert "کلیک کنید" not in caption
    assert "@AiFreeRoPrompt" not in caption
    assert "ساخت عکس با این پرامپت" in caption
