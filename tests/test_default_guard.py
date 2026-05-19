from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_guard_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "gaurd_scrpts" / "default_guard.py"
    spec = importlib.util.spec_from_file_location("default_guard", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_default_guard_ad_detection_blocks_sponsor_text() -> None:
    m = _load_guard_module()
    payload = {
        "message": {
            "text": "⭐️ اسپانسر : صرافی Arz3.com\nکد تخفیف ویژه",
            "caption": None,
        }
    }
    assert m._is_obvious_advertisement(payload) is True


def test_default_guard_news_not_detected_as_ad_or_vpn() -> None:
    m = _load_guard_module()
    payload = {
        "message": {
            "text": None,
            "caption": "دادگاه نظامی چین دو وزیر دفاع سابق را به دلیل فساد، به اعدام محکوم کرد\n@Squad_iran | #T",
        }
    }
    assert m._is_obvious_advertisement(payload) is False
    assert m._is_obvious_vpn_config(payload) is False


def test_default_guard_clash_word_is_not_vpn_by_itself() -> None:
    m = _load_guard_module()
    payload = {
        "message": {
            "text": "Player suffered a clash with defender and returned to pitch.",
            "caption": None,
        }
    }
    assert m._is_obvious_vpn_config(payload) is False


def test_default_guard_blocks_channel_promo_list_style_message() -> None:
    m = _load_guard_module()
    payload = {
        "message": {
            "text": (
                "Channels for TRUE football fans to follow:\n\n"
                "🏴 Sky Sports Football - biggest football channel on Telegram\n"
                "⚽️ @jfball - just football\n"
                "💪 @Cristiano - Cristiano Ronaldo's account on Telegram\n"
                "🤣 @Soccer_Memes - funniest football memes\n"
                "📊 SofaScore - live scores and football updates"
            ),
            "caption": None,
        }
    }
    assert m._is_obvious_advertisement(payload) is True


def test_default_guard_blocks_betting_site_brand_names() -> None:
    m = _load_guard_module()
    payload = {
        "message": {
            "text": "Join 1xbet now and claim your welcome bonus!",
            "caption": None,
        }
    }
    assert m._is_obvious_advertisement(payload) is True


def test_default_guard_blocks_casino_promo_with_link() -> None:
    m = _load_guard_module()
    payload = {
        "message": {
            "text": "کازینو آنلاین با بونوس ویژه و ثبت نام سریع\nhttps://example.com",
            "caption": None,
        }
    }
    assert m._is_obvious_advertisement(payload) is True


def test_default_guard_does_not_block_normal_sports_news_with_odds_word() -> None:
    m = _load_guard_module()
    payload = {
        "message": {
            "text": "The odds of winning the title changed after the derby, but this is just a match report.",
            "caption": None,
        }
    }
    assert m._is_obvious_advertisement(payload) is False


def test_default_guard_blocks_spinarium_free_spins_ad_style_message() -> None:
    m = _load_guard_module()
    payload = {
        "message": {
            "text": None,
            "caption": (
                "Welcome to Spinarium\n\n"
                "Spinarium 🎰\n\n"
                "The reels are set. The gift is wrapped.\n"
                "50 FS are yours — just walk in and claim them.\n"
                "Ad. 18+"
            ),
        }
    }
    assert m._is_obvious_advertisement(payload) is True


def test_default_guard_blocks_explicit_ad_disclosure_for_ptxai_style_caption() -> None:
    m = _load_guard_module()
    payload = {
        "message": {
            "text": None,
            "caption": (
                "Smart Trading With AI\n\n"
                "Ptxai\n\n"
                "Download the ptxai app and try the demo now.\n"
                "Ad. 18+"
            ),
        }
    }
    assert m._is_obvious_advertisement(payload) is True


def test_default_guard_does_not_flag_adidas_or_regular_news_as_explicit_ad() -> None:
    m = _load_guard_module()
    payload = {
        "message": {
            "text": "Adidas unveiled the new Real Madrid kit for the 2026 season.",
            "caption": None,
        }
    }
    assert m._is_obvious_advertisement(payload) is False
