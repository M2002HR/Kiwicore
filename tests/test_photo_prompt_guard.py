from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_guard_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "gaurd_scrpts" / "photo_prompt_guard.py"
    spec = importlib.util.spec_from_file_location("photo_prompt_guard", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_photo_prompt_guard_detects_image_presence() -> None:
    m = _load_guard_module()
    payload = {
        "inputs": [
            {"kind": "photo", "local_name": "a.jpg"},
            {"kind": "video", "local_name": "b.mp4"},
        ]
    }
    assert m._has_image(payload) is True


def test_photo_prompt_guard_blocks_without_image() -> None:
    m = _load_guard_module()
    payload = {
        "message": {"caption": "Prompt: cinematic portrait, 85mm lens"},
        "inputs": [{"kind": "video", "local_name": "a.mp4"}],
    }
    assert m._looks_like_prompt_payload(payload) is False


def test_photo_prompt_guard_accepts_prompt_like_text_with_marker() -> None:
    m = _load_guard_module()
    text = "نسخه زنانه: Ultra-realistic cinematic portrait, 85mm lens, depth of field"
    assert m._looks_like_prompt_text(text) is True


def test_photo_prompt_guard_accepts_dense_prompt_without_keyword() -> None:
    m = _load_guard_module()
    text = (
        "ultra realistic close-up portrait, dramatic studio lighting, skin texture visible, "
        "soft bokeh background, fashion editorial look, natural color grading"
    )
    assert m._looks_like_prompt_text(text) is True


def test_photo_prompt_guard_rejects_announcement_text() -> None:
    m = _load_guard_module()
    text = "اختلال بخش ویرایش عکس رفع شد و مدل‌ها به‌روزرسانی شدند"
    assert m._looks_like_prompt_text(text) is False
