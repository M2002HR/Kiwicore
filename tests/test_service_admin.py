from __future__ import annotations

from kiwi.service import KiwiService


def test_split_admin_response_text_returns_single_chunk_for_short_text() -> None:
    out = KiwiService._split_admin_response_text("hello")
    assert out == ["hello"]


def test_split_admin_response_text_splits_long_text_without_losing_content() -> None:
    source = ("route-x\n" * 1200).strip()
    out = KiwiService._split_admin_response_text(source, max_chars=400)
    assert len(out) > 1
    assert all(1 <= len(chunk) <= 400 for chunk in out)
    rebuilt = "\n".join(line for chunk in out for line in chunk.splitlines())
    assert rebuilt == source
