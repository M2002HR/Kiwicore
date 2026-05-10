from __future__ import annotations

import importlib.util
import os
from pathlib import Path


def _load_default_script_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "channel_scripts" / "default_scripts.py"
    spec = importlib.util.spec_from_file_location("default_scripts", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ai_cleanup_path_is_used_for_reference_text(monkeypatch) -> None:
    m = _load_default_script_module()
    monkeypatch.setenv("SCRIPT_CLEAN_AI_ENABLED", "true")
    monkeypatch.setenv("SCRIPT_CLEAN_AI_ENDPOINT", "http://fake.local/proxy/gemini")
    monkeypatch.setenv("SCRIPT_CLEAN_AI_FAIL_OPEN", "false")

    def fake_call(*, endpoint: str, body: dict, timeout_sec: float):
        assert endpoint == "http://fake.local/proxy/gemini"
        return "متن پاکسازی شد"

    monkeypatch.setattr(m, "_call_gemini_text", fake_call)

    payload = {"route": {"destination_target": "@dest"}}
    out = m._sanitize_text("در شبکه x https://x.com/test", payload=payload)
    assert out == "متن پاکسازی شد\n@dest"

