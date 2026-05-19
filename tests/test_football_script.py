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


def test_football_script_keeps_default_passthrough_when_ai_disabled(tmp_path: Path) -> None:
    payload = {
        "message": {"text": "hello"},
        "inputs": [],
    }
    out = _run_script(payload, tmp_path)
    assert out == {"messages": [{"type": "text", "text": "hello"}]}


def test_football_script_applies_ai_text_to_caption_and_removes_old_text(tmp_path: Path, monkeypatch) -> None:
    mod = _load_module()
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_ENABLED", "true")
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_ENDPOINT", "http://fake.local/proxy/gemini")
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_FAIL_OPEN", "true")

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


def test_football_script_preserves_source_emojis_in_generated_caption(tmp_path: Path, monkeypatch) -> None:
    mod = _load_module()
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_ENABLED", "true")
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_ENDPOINT", "http://fake.local/proxy/gemini")

    def fake_call(*, endpoint: str, body: dict, timeout_sec: float):
        return "تیم با نمایش منظم و حملات سریع، سه امتیاز ارزشمند را گرفت."

    monkeypatch.setattr(mod, "_call_gemini_text", fake_call)

    payload = {
        "route": {"destination_target": "@dest"},
        "message": {"caption": "برد دلچسب تیم 😍🔥"},
        "inputs": [{"kind": "photo", "local_name": "a.jpg"}],
    }
    out = mod.build_messages(payload, input_dir=tmp_path)
    caption = str(out[0].get("caption") or "")
    assert "😍" in caption
    assert "🔥" in caption


def test_football_script_keeps_destination_signature_last_when_injecting_emojis(tmp_path: Path, monkeypatch) -> None:
    mod = _load_module()
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_ENABLED", "true")
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_ENDPOINT", "http://fake.local/proxy/gemini")

    def fake_call(*, endpoint: str, body: dict, timeout_sec: float):
        return "تیم با نمایش خوب بازی را برد.\n@dest"

    monkeypatch.setattr(mod, "_call_gemini_text", fake_call)

    payload = {
        "route": {"destination_target": "@dest"},
        "message": {"caption": "برد دلچسب تیم 😍"},
        "inputs": [{"kind": "photo", "local_name": "a.jpg"}],
    }
    out = mod.build_messages(payload, input_dir=tmp_path)
    caption = str(out[0].get("caption") or "")
    assert "😍" in caption
    assert caption.endswith("\n@dest")


def test_football_script_ai_request_is_text_only_even_with_photo_input(tmp_path: Path, monkeypatch) -> None:
    mod = _load_module()
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_ENABLED", "true")
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_ENDPOINT", "http://fake.local/proxy/gemini")

    captured: dict[str, object] = {}

    def fake_call(*, endpoint: str, body: dict, timeout_sec: float):
        captured["body"] = body
        return "یک روایت فوتبالی روان از صحنه بازی"

    monkeypatch.setattr(mod, "_call_gemini_text", fake_call)

    payload = {
        "route": {"destination_target": "@dest"},
        "message": {"caption": "caption"},
        "inputs": [{"kind": "photo", "local_name": "frame.jpg", "mime_type": "image/jpeg"}],
    }

    out = mod.build_messages(payload, input_dir=tmp_path)
    assert out[0]["caption"] == "یک روایت فوتبالی روان از صحنه بازی"

    body = captured.get("body")
    assert isinstance(body, dict)
    contents = body.get("contents")
    assert isinstance(contents, list) and contents
    parts = contents[0].get("parts")
    assert isinstance(parts, list)
    assert all(isinstance(part, dict) and "text" in part for part in parts)
    assert not any(isinstance(part, dict) and "inlineData" in part for part in parts)


def test_football_script_refines_low_quality_first_pass(tmp_path: Path, monkeypatch) -> None:
    mod = _load_module()
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_ENABLED", "true")
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_ENDPOINT", "http://fake.local/proxy/gemini")

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
            "CHANNEL_SCRIPT_AI_ENABLED": "true",
            "CHANNEL_SCRIPT_AI_ENDPOINT": "http://127.0.0.1:9/nowhere",
            "CHANNEL_SCRIPT_AI_MANDATORY": "false",
            "CHANNEL_SCRIPT_AI_RETRY_COUNT": "0",
            "CHANNEL_SCRIPT_AI_TIMEOUT_SEC": "0.2",
            "CHANNEL_SCRIPT_AI_FAIL_OPEN": "true",
        },
    )
    assert out == {"messages": [{"type": "text", "text": "متن تست"}]}


def test_football_script_exits_with_ai_generation_required_failed_when_mandatory_ai_unavailable(tmp_path: Path) -> None:
    payload = {
        "route": {"destination_target": "@dest"},
        "message": {"caption": "متن تست"},
        "inputs": [{"kind": "photo", "local_name": "a.jpg"}],
    }
    payload_path = tmp_path / "payload.json"
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    payload_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    env = os.environ.copy()
    env.update(
        {
            "CHANNEL_SCRIPT_AI_ENABLED": "true",
            "CHANNEL_SCRIPT_AI_ENDPOINT": "http://127.0.0.1:9/nowhere",
            "CHANNEL_SCRIPT_AI_MANDATORY": "true",
            "CHANNEL_SCRIPT_AI_RETRY_COUNT": "0",
            "CHANNEL_SCRIPT_AI_TIMEOUT_SEC": "0.2",
        }
    )
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
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode != 0
    assert "ai_generation_required_failed" in (result.stderr or "")
    assert "پست جدید فوتبالی منتشر شد" not in (result.stdout or "")
    assert "خبر جدید فوتبالی منتشر شد" not in (result.stdout or "")


def test_football_script_postprocess_removes_prompt_leak_and_duplicate_signature(tmp_path: Path, monkeypatch) -> None:
    mod = _load_module()
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_ENABLED", "true")
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_ENDPOINT", "http://fake.local/proxy/gemini")

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


def test_football_script_respects_global_budget_and_falls_back_fast(tmp_path: Path, monkeypatch) -> None:
    mod = _load_module()
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_ENABLED", "true")
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_ENDPOINT", "http://fake.local/proxy/gemini")
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_MANDATORY", "false")
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_FAIL_OPEN", "false")
    monkeypatch.setattr(mod, "_ai_total_budget_sec", lambda: 0.1)

    def should_not_call(**kwargs):
        raise AssertionError("_call_gemini_text should not be called when global budget is exhausted")

    monkeypatch.setattr(mod, "_call_gemini_text", should_not_call)
    payload = {
        "route": {"destination_target": "@dest"},
        "message": {"text": "متن کوتاه"},
        "inputs": [],
    }
    out = mod.build_messages(payload, input_dir=tmp_path)
    assert out == [{"type": "text", "text": "متن کوتاه"}]


def test_football_script_forces_persian_rewrite_when_first_output_is_english(tmp_path: Path, monkeypatch) -> None:
    mod = _load_module()
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_ENABLED", "true")
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_ENDPOINT", "http://fake.local/proxy/gemini")

    calls = {"count": 0}

    def fake_call(*, endpoint: str, body: dict, timeout_sec: float):
        calls["count"] += 1
        if calls["count"] == 1:
            return "Bruno Fernandes beat Declan Rice to the FWA Footballer of the Year award."
        return "برونو فرناندس در رای‌گیری جایزه بهترین بازیکن سال نویسندگان فوتبال، دکلان رایس را پشت سر گذاشت."

    monkeypatch.setattr(mod, "_call_gemini_text", fake_call)
    payload = {
        "route": {"destination_target": "@dest"},
        "message": {"caption": "raw cap"},
        "inputs": [{"kind": "photo", "local_name": "a.jpg"}],
    }
    out = mod.build_messages(payload, input_dir=tmp_path)
    assert calls["count"] >= 2
    assert "برونو فرناندس" in (out[0].get("caption") or "")


def test_football_script_media_without_caption_passthroughs_without_ai(tmp_path: Path, monkeypatch) -> None:
    mod = _load_module()
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_ENABLED", "true")
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_ENDPOINT", "http://fake.local/proxy/gemini")

    def fake_call(*, endpoint: str, body: dict, timeout_sec: float):
        raise AssertionError("AI should not be called for media-only messages without text/caption")

    monkeypatch.setattr(mod, "_call_gemini_text", fake_call)
    payload = {
        "route": {"destination_target": "@dest"},
        "message": {},
        "inputs": [{"kind": "photo", "local_name": "a.jpg"}],
    }
    out = mod.build_messages(payload, input_dir=tmp_path)
    assert out == [{"type": "photo", "path": "a.jpg"}]


def test_football_script_non_photo_without_caption_passthroughs_without_ai(tmp_path: Path, monkeypatch) -> None:
    mod = _load_module()
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_ENABLED", "true")
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_ENDPOINT", "http://fake.local/proxy/gemini")

    def fake_call(*, endpoint: str, body: dict, timeout_sec: float):
        raise AssertionError("AI should not be called for media-only messages without text/caption")

    monkeypatch.setattr(mod, "_call_gemini_text", fake_call)
    payload = {
        "route": {"destination_target": "@dest"},
        "message": {},
        "inputs": [{"kind": "video", "local_name": "a.mp4"}],
    }

    out = mod.build_messages(payload, input_dir=tmp_path)
    assert out == [{"type": "video", "path": "a.mp4"}]


def test_football_script_video_with_caption_bypasses_ai_even_in_mandatory_mode(tmp_path: Path, monkeypatch) -> None:
    mod = _load_module()
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_ENABLED", "true")
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_MANDATORY", "true")
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_ENDPOINT", "http://fake.local/proxy/gemini")

    def fake_call(*, endpoint: str, body: dict, timeout_sec: float):
        raise AssertionError("AI should not be called for video inputs")

    monkeypatch.setattr(mod, "_call_gemini_text", fake_call)
    payload = {
        "route": {"destination_target": "@dest"},
        "message": {"caption": "Pep laughed about the tattoo moment 😅"},
        "inputs": [{"kind": "video", "local_name": "clip.mp4"}],
    }

    out = mod.build_messages(payload, input_dir=tmp_path)
    assert out == [{"type": "video", "path": "clip.mp4", "caption": "Pep laughed about the tattoo moment 😅"}]


def test_football_script_raises_when_mandatory_ai_unavailable_for_album_caption(
    tmp_path: Path, monkeypatch
) -> None:
    mod = _load_module()
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_ENABLED", "true")
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_MANDATORY", "true")
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_FAIL_OPEN", "true")
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_ENDPOINT", "http://fake.local/proxy/gemini")

    def fake_call(*, endpoint: str, body: dict, timeout_sec: float):
        return None

    monkeypatch.setattr(mod, "_call_gemini_text", fake_call)

    payload = {
        "route": {"destination_target": "@dest"},
        "message": {"caption": "ngl you probably need the full collection 💚"},
        "inputs": [
            {"kind": "photo", "local_name": "photo_1"},
            {"kind": "photo", "local_name": "photo_2"},
            {"kind": "photo", "local_name": "photo_3"},
        ],
    }
    try:
        mod.build_messages(payload, input_dir=tmp_path)
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "ai_generation_required_failed" in str(exc)


def test_football_script_builds_prompt_even_when_ai_is_disabled(tmp_path: Path, monkeypatch) -> None:
    mod = _load_module()
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_ENABLED", "false")

    seen: dict[str, str] = {}
    original_prompt = mod._football_prompt

    def tracking_prompt(source_text: str, destination: str, *, length_instruction: str, layout_instruction: str) -> str:
        seen["source_text"] = source_text
        seen["destination"] = destination
        seen["length_instruction"] = length_instruction
        seen["layout_instruction"] = layout_instruction
        return original_prompt(
            source_text,
            destination,
            length_instruction=length_instruction,
            layout_instruction=layout_instruction,
        )

    monkeypatch.setattr(mod, "_football_prompt", tracking_prompt)

    payload = {
        "route": {"destination_target": "@dest"},
        "message": {"text": "Inter Miami won"},
        "inputs": [],
    }
    out = mod.build_messages(payload, input_dir=tmp_path)
    assert seen["destination"] == "@dest"
    assert "TEXT:" in seen["source_text"]
    assert "طول خروجی باید نزدیک طول ورودی باشد" in seen["length_instruction"]
    assert "خروجی را خوانا نگه دار" in seen["layout_instruction"]
    assert out == [{"type": "text", "text": "Inter Miami won"}]


def test_football_script_length_bounds_follow_source_size() -> None:
    mod = _load_module()
    low_s, high_s, bucket_s = mod._source_length_bounds("گل")
    medium_source = (
        "تیم در نیمه اول با پرس شدید بازی را کنترل کرد و بعد از گل اول، با پاس‌های کوتاه و منظم "
        "ریتم مسابقه را نگه داشت تا حریف نتواند ضدحمله‌های خطرناک بسازد."
    )
    low_m, high_m, bucket_m = mod._source_length_bounds(medium_source)
    long_source = " ".join(["تیم"] * 180)
    low_l, high_l, bucket_l = mod._source_length_bounds(long_source)
    assert bucket_s == "short"
    assert bucket_m == "medium"
    assert bucket_l == "long"
    assert low_s < low_m < low_l
    assert high_s < high_m < high_l


def test_football_script_applies_newline_layout_when_source_is_multiline(tmp_path: Path, monkeypatch) -> None:
    mod = _load_module()
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_ENABLED", "true")
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_ENDPOINT", "http://fake.local/proxy/gemini")

    def fake_call(*, endpoint: str, body: dict, timeout_sec: float):
        return "تیم عالی بود. مالکیت بالا بود. دفاع منسجم بود."

    monkeypatch.setattr(mod, "_call_gemini_text", fake_call)
    payload = {
        "route": {"destination_target": "@dest"},
        "message": {"caption": "خط اول\nخط دوم\n@dest"},
        "inputs": [{"kind": "photo", "local_name": "a.jpg"}],
    }
    out = mod.build_messages(payload, input_dir=tmp_path)
    caption = str(out[0].get("caption") or "")
    assert "\n" in caption


def test_football_script_rejects_non_persian_caption_in_mandatory_mode(tmp_path: Path, monkeypatch) -> None:
    mod = _load_module()
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_ENABLED", "true")
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_ENDPOINT", "http://fake.local/proxy/gemini")
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_FAIL_OPEN", "false")

    def fake_call(*, endpoint: str, body: dict, timeout_sec: float):
        return "Saka dropping these memes on his Instagram story after two huge Arsenal wins last week 😂"

    monkeypatch.setattr(mod, "_call_gemini_text", fake_call)
    payload = {
        "route": {"destination_target": "@dest"},
        "message": {"caption": "english cap"},
        "inputs": [{"kind": "photo", "local_name": "a.jpg"}],
    }
    try:
        mod.build_messages(payload, input_dir=tmp_path)
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "ai_generation_required_failed" in str(exc)


def test_football_script_mandatory_mode_skips_empty_source_message(tmp_path: Path, monkeypatch) -> None:
    mod = _load_module()
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_ENABLED", "true")
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_MANDATORY", "true")
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_ENDPOINT", "http://fake.local/proxy/gemini")

    payload = {
        "route": {"destination_target": "@dest"},
        "message": {},
        "inputs": [],
    }
    out = mod.build_messages(payload, input_dir=tmp_path)
    assert out == []


def test_football_script_keeps_short_persian_caption_with_destination_signature_in_mandatory_mode(
    tmp_path: Path, monkeypatch
) -> None:
    mod = _load_module()
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_ENABLED", "true")
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_MANDATORY", "true")
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_FAIL_OPEN", "true")
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_ENDPOINT", "http://fake.local/proxy/gemini")

    def fake_call(*, endpoint: str, body: dict, timeout_sec: float):
        return "برد خوب تیم\n@dest"

    monkeypatch.setattr(mod, "_call_gemini_text", fake_call)
    payload = {
        "route": {"destination_target": "@dest"},
        "message": {"caption": "raw source caption"},
        "inputs": [{"kind": "photo", "local_name": "photo_1"}, {"kind": "photo", "local_name": "photo_2"}],
    }
    out = mod.build_messages(payload, input_dir=tmp_path)
    assert out[0].get("caption") == "برد خوب تیم\n@dest"
    assert [item.get("path") for item in out] == ["photo_1", "photo_2"]


def test_football_script_raises_when_mandatory_ai_unavailable_even_if_source_caption_is_persian(
    tmp_path: Path, monkeypatch
) -> None:
    mod = _load_module()
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_ENABLED", "true")
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_MANDATORY", "true")
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_FAIL_OPEN", "true")
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_ENDPOINT", "http://fake.local/proxy/gemini")

    def fake_call(*, endpoint: str, body: dict, timeout_sec: float):
        return None

    monkeypatch.setattr(mod, "_call_gemini_text", fake_call)
    payload = {
        "route": {"destination_target": "@dest"},
        "message": {"caption": "برد خوب تیم\n@dest"},
        "inputs": [{"kind": "photo", "local_name": "photo_1"}],
    }
    try:
        mod.build_messages(payload, input_dir=tmp_path)
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "ai_generation_required_failed" in str(exc)


def test_football_script_raises_when_mandatory_ai_unavailable_with_non_persian_source_caption(
    tmp_path: Path, monkeypatch
) -> None:
    mod = _load_module()
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_ENABLED", "true")
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_MANDATORY", "true")
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_FAIL_OPEN", "true")
    monkeypatch.setenv("CHANNEL_SCRIPT_AI_ENDPOINT", "http://fake.local/proxy/gemini")

    def fake_call(*, endpoint: str, body: dict, timeout_sec: float):
        return None

    monkeypatch.setattr(mod, "_call_gemini_text", fake_call)
    payload = {
        "route": {"destination_target": "@dest"},
        "message": {"caption": "Fabinho says Neymar is still Brazil's most talented player."},
        "inputs": [{"kind": "photo", "local_name": "photo_1"}, {"kind": "photo", "local_name": "photo_2"}],
    }
    try:
        mod.build_messages(payload, input_dir=tmp_path)
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "ai_generation_required_failed" in str(exc)
