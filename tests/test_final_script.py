from __future__ import annotations

import importlib.util
import re
from pathlib import Path


def _load_final_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "final_scripts" / "default_final_script.py"
    spec = importlib.util.spec_from_file_location("final_script_test_module", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_final_script_strips_latin_residue_when_ai_disabled(monkeypatch) -> None:
    m = _load_final_module()
    monkeypatch.setenv("FINAL_SCRIPT_AI_ENABLED", "false")

    text = "برونو فرناندس congratulating مارکوس راشفورد on his لالیگا قهرمانی برد ❤️"
    out = m._polish(text)
    assert "برونو فرناندس" in out
    assert "مارکوس راشفورد" in out
    assert "لالیگا" in out
    assert re.search(r"\bcongratulating\b", out, flags=re.IGNORECASE) is None
    assert re.search(r"\bon\b", out, flags=re.IGNORECASE) is None
