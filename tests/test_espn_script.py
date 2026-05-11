from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_espn_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "channel_scripts" / "espn.py"
    spec = importlib.util.spec_from_file_location("espn_script_test_module", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_espn_fallback_translation_without_ai(monkeypatch) -> None:
    m = _load_espn_module()
    monkeypatch.delenv("ESPN_AI_ENDPOINT", raising=False)
    monkeypatch.delenv("SCRIPT_CLEAN_AI_ENDPOINT", raising=False)
    monkeypatch.delenv("FINAL_SCRIPT_AI_ENDPOINT", raising=False)
    monkeypatch.delenv("GUARD_AI_ENDPOINT", raising=False)

    text = "Vini Jr. reminding Barca fans that @Real_Madrid have 15 UCL trophies 😅"
    out = m._localize_text(text, allowed_mentions=set())
    assert "@Real_Madrid" not in out
    assert "رئال مادرید" in out
    assert m.PERSIAN_CHAR_RE.search(out)


def test_espn_rejects_meta_ai_response_and_uses_fallback(monkeypatch) -> None:
    m = _load_espn_module()
    monkeypatch.setenv("ESPN_AI_ENDPOINT", "http://fake.local/proxy/gemini")
    monkeypatch.setenv("ESPN_AI_FAIL_OPEN", "true")

    def fake_call(*, endpoint: str, body: dict, timeout_sec: float):
        return "Role: football editor\nInput text:\n..."

    monkeypatch.setattr(m, "_call_gemini_text", fake_call)
    out = m._translate_football_text(
        "Messi's Instagram story after Barcelona won the LaLiga title vs. Real Madrid",
        placeholders=[],
    )
    assert "Role:" not in out
    assert m.PERSIAN_CHAR_RE.search(out)


def test_espn_keeps_allowed_mentions(monkeypatch) -> None:
    m = _load_espn_module()
    monkeypatch.delenv("ESPN_AI_ENDPOINT", raising=False)
    text = "خبر کوتاه @kiwi_kiwi_test"
    out = m._localize_text(text, allowed_mentions={"@kiwi_kiwi_test"})
    assert "@kiwi_kiwi_test" in out


def test_espn_sample_caption_names_and_fluency(monkeypatch) -> None:
    m = _load_espn_module()
    monkeypatch.delenv("ESPN_AI_ENDPOINT", raising=False)
    monkeypatch.delenv("SCRIPT_CLEAN_AI_ENDPOINT", raising=False)
    monkeypatch.delenv("FINAL_SCRIPT_AI_ENDPOINT", raising=False)
    monkeypatch.delenv("GUARD_AI_ENDPOINT", raising=False)

    text = (
        "Jude Bellingham appeared to lose consciousness momentarily after suffering a clash with "
        "@BarcelonaEn Eric Garcia.\n\n"
        "Bellingham briefly left the pitch before returning shortly after.\n\n"
        "Warrior mentality 😤"
    )
    out = m._localize_text(text, allowed_mentions=set())
    assert "جود بلینگهم" in out
    assert "اریک گارسیا" in out
    assert "از بارسلونا" in out or "بارسلونا" in out
    assert "@BarcelonaEn" not in out
    assert m.PERSIAN_CHAR_RE.search(out)


def test_espn_accepts_mixed_ai_output_without_special_case_rewrite(monkeypatch) -> None:
    m = _load_espn_module()
    monkeypatch.setenv("ESPN_AI_ENDPOINT", "http://fake.local/proxy/gemini")
    monkeypatch.setenv("ESPN_AI_FAIL_OPEN", "true")

    mixed_output = (
        "برونو فرناندس congratulating مارکوس راشفورد on his لالیگا قهرمانی برد ❤️"
    )

    def fake_call(*, endpoint: str, body: dict, timeout_sec: float):
        return mixed_output

    monkeypatch.setattr(m, "_call_gemini_text", fake_call)

    text = "Bruno Fernandes congratulating Marcus Rashford on his LALIGA title win ❤️"
    out = m._localize_text(text, allowed_mentions=set())
    assert "برونو فرناندس" in out
    assert "مارکوس راشفورد" in out
    assert "لالیگا" in out
