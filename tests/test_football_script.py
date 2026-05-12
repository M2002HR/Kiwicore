from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path


def _script_path() -> Path:
    return Path(__file__).resolve().parents[1] / "scripts" / "channel_scripts" / "football.py"


def _load_module():
    path = _script_path()
    spec = importlib.util.spec_from_file_location("football_script_test_module", path)
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
    env["FOOTBALL_AI_ENABLED"] = "false"
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


def test_football_script_keeps_default_passthrough_when_ai_disabled(tmp_path: Path) -> None:
    payload = {
        "message": {"text": "hello"},
        "inputs": [],
    }
    out = _run_script(payload, tmp_path)
    assert out == {"messages": [{"type": "text", "text": "hello"}]}


def test_football_script_applies_ai_text_to_caption_and_removes_old_text(tmp_path: Path, monkeypatch) -> None:
    mod = _load_module()
    monkeypatch.setenv("FOOTBALL_AI_ENABLED", "true")
    monkeypatch.setenv("FOOTBALL_AI_ENDPOINT", "http://fake.local/proxy/gemini")
    monkeypatch.setenv("FOOTBALL_AI_FAIL_OPEN", "true")

    def fake_call(*, endpoint: str, body: dict, timeout_sec: float):
        assert endpoint == "http://fake.local/proxy/gemini"
        return "برد مهم و نمایش درخشان؛ تیم با انسجام کامل سه امتیاز را گرفت."

    monkeypatch.setattr(mod, "_call_gemini_text", fake_call)

    payload = {
        "route": {"destination_target": "@dest"},
        "message": {"text": "raw text", "caption": "raw cap"},
        "inputs": [{"kind": "photo", "local_name": "a.jpg"}],
    }
    messages = mod.build_messages(payload, input_dir=tmp_path)
    assert messages == [
        {
            "type": "photo",
            "path": "a.jpg",
            "caption": "برد مهم و نمایش درخشان؛ تیم با انسجام کامل سه امتیاز را گرفت.",
        }
    ]


def test_football_script_uses_image_parts_in_ai_request(tmp_path: Path, monkeypatch) -> None:
    mod = _load_module()
    monkeypatch.setenv("FOOTBALL_AI_ENABLED", "true")
    monkeypatch.setenv("FOOTBALL_AI_ENDPOINT", "http://fake.local/proxy/gemini")

    input_dir = tmp_path / "input"
    input_dir.mkdir()
    (input_dir / "frame.jpg").write_bytes(b"fakejpeg")

    captured: dict[str, object] = {}
    calls = {"count": 0}

    def fake_call(*, endpoint: str, body: dict, timeout_sec: float):
        calls["count"] += 1
        captured["body"] = body
        if calls["count"] == 1:
            return None
        return "یک روایت فوتبالی روان از صحنه بازی"

    monkeypatch.setattr(mod, "_call_gemini_text", fake_call)

    payload = {
        "route": {"destination_target": "@dest"},
        "message": {"caption": "caption"},
        "inputs": [{"kind": "photo", "local_name": "frame.jpg", "mime_type": "image/jpeg"}],
    }

    out = mod.build_messages(payload, input_dir=input_dir)
    assert out[0]["caption"] == "یک روایت فوتبالی روان از صحنه بازی"

    body = captured.get("body")
    assert isinstance(body, dict)
    contents = body.get("contents")
    assert isinstance(contents, list) and contents
    parts = contents[0].get("parts")
    assert isinstance(parts, list)
    assert any(isinstance(part, dict) and "inlineData" in part for part in parts)


def test_football_script_refines_low_quality_first_pass(tmp_path: Path, monkeypatch) -> None:
    mod = _load_module()
    monkeypatch.setenv("FOOTBALL_AI_ENABLED", "true")
    monkeypatch.setenv("FOOTBALL_AI_ENDPOINT", "http://fake.local/proxy/gemini")

    calls = {"count": 0}

    def fake_call(*, endpoint: str, body: dict, timeout_sec: float):
        calls["count"] += 1
        if calls["count"] == 1:
            return "goal by x after pass"
        return "تیم با یک حمله سریع به گل رسید و با بازی منظم نتیجه را حفظ کرد."

    monkeypatch.setattr(mod, "_call_gemini_text", fake_call)

    payload = {
        "route": {"destination_target": "@dest"},
        "message": {"text": "raw"},
        "inputs": [],
    }
    out = mod.build_messages(payload, input_dir=tmp_path)
    assert calls["count"] == 2
    assert out == [{"type": "text", "text": "تیم با یک حمله سریع به گل رسید و با بازی منظم نتیجه را حفظ کرد."}]


def test_football_script_stdout_stays_json_when_ai_enabled_and_endpoint_fails(tmp_path: Path) -> None:
    payload = {
        "route": {"destination_target": "@dest"},
        "message": {"text": "متن تست"},
        "inputs": [],
    }
    out = _run_script(
        payload,
        tmp_path,
        env_patch={
            "FOOTBALL_AI_ENABLED": "true",
            "FOOTBALL_AI_ENDPOINT": "http://127.0.0.1:9/nowhere",
            "FOOTBALL_AI_RETRY_COUNT": "0",
            "FOOTBALL_AI_TIMEOUT_SEC": "0.2",
            "FOOTBALL_AI_FAIL_OPEN": "true",
        },
    )
    assert out == {"messages": [{"type": "text", "text": "متن تست"}]}


def test_football_script_postprocess_removes_prompt_leak_and_duplicate_signature(tmp_path: Path, monkeypatch) -> None:
    mod = _load_module()
    monkeypatch.setenv("FOOTBALL_AI_ENABLED", "true")
    monkeypatch.setenv("FOOTBALL_AI_ENDPOINT", "http://fake.local/proxy/gemini")

    leaked = (
        "Professional Persian football content writer.\n"
        "Raw text, captions, or images.\n"
        "ساخت فیلم بیوگرافی رسمی ایان رایت در حال توسعه است.\n"
        "@kiwi_kiwi_test\n"
        "ساخت فیلم بیوگرافی رسمی ایان رایت در حال توسعه است.\n"
        "@kiwi_kiwi_test"
    )

    def fake_call(*, endpoint: str, body: dict, timeout_sec: float):
        return leaked

    monkeypatch.setattr(mod, "_call_gemini_text", fake_call)
    payload = {
        "route": {"destination_target": "@kiwi_kiwi_test"},
        "message": {"caption": "raw cap"},
        "inputs": [{"kind": "photo", "local_name": "a.jpg"}],
    }

    messages = mod.build_messages(payload, input_dir=tmp_path)
    assert len(messages) == 1
    caption = messages[0].get("caption")
    assert isinstance(caption, str)
    assert "Professional Persian football content writer" not in caption
    assert caption.count("@kiwi_kiwi_test") == 1
    assert "ساخت فیلم بیوگرافی رسمی ایان رایت در حال توسعه است." in caption
