from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_guard_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "gaurd_scrpts" / "wallpaper_guard.py"
    spec = importlib.util.spec_from_file_location("wallpaper_guard", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_wallpaper_guard_blocks_irrelevant_instagram_promo_caption() -> None:
    m = _load_guard_module()
    payload = {
        "message": {
            "text": "",
            "caption": "Add my instagram to enjoy dope pics use link below 😍👇\n\nhttps://www.instagram.com/komot4dere",
        }
    }
    assert m._is_irrelevant_wallpaper_caption(payload) is True


def test_wallpaper_guard_allows_wallpaper_hashtag_caption() -> None:
    m = _load_guard_module()
    payload = {
        "message": {
            "text": "",
            "caption": "#Vaporwave #NeonCity\n@WallpapersArena",
        }
    }
    assert m._is_irrelevant_wallpaper_caption(payload) is False


def test_wallpaper_guard_blocks_non_wallpaper_football_caption() -> None:
    m = _load_guard_module()
    payload = {
        "message": {
            "text": "",
            "caption": "رسمی شد؛ آرسنال پس از سال ۲۰۰۴ دوباره فاتح لیگ برتر انگلیس شد! 🚨",
        }
    }
    assert m._is_irrelevant_wallpaper_caption(payload) is True


def test_wallpaper_guard_blocks_linkless_engagement_bait_caption() -> None:
    m = _load_guard_module()
    payload = {
        "message": {
            "text": "",
            "caption": "Wanna laugh?😂🤣 Click here👇",
        }
    }
    assert m._is_irrelevant_wallpaper_caption(payload) is True


def test_wallpaper_guard_allows_wallpaper_caption_with_funny_hashtag() -> None:
    m = _load_guard_module()
    payload = {
        "message": {
            "text": "",
            "caption": "#wallpaper #4k #funny",
        }
    }
    assert m._is_irrelevant_wallpaper_caption(payload) is False


def test_wallpaper_guard_allows_join_invite_for_same_source_channel() -> None:
    m = _load_guard_module()
    payload = {
        "route": {"source_channel_username": "@hd_ultra_wallpapers"},
        "message": {
            "source_channel_username": "@hd_ultra_wallpapers",
            "text": "",
            "caption": "Join Us ❤️ @hd_ultra_wallpapers",
        },
    }
    assert m._is_same_source_join_invite(payload) is True
    assert m._is_irrelevant_wallpaper_caption(payload) is False


def test_wallpaper_guard_blocks_join_invite_for_other_channel() -> None:
    m = _load_guard_module()
    payload = {
        "route": {"source_channel_username": "@hd_ultra_wallpapers"},
        "message": {
            "source_channel_username": "@hd_ultra_wallpapers",
            "text": "",
            "caption": "Join Us ❤️ @another_channel",
        },
    }
    assert m._is_same_source_join_invite(payload) is False
    assert m._is_irrelevant_wallpaper_caption(payload) is True


def test_wallpaper_guard_detects_nonempty_caption() -> None:
    m = _load_guard_module()
    payload = {"message": {"caption": "  #wallpaper  "}}
    assert m._has_nonempty_caption(payload) is True


def test_wallpaper_guard_detects_empty_caption() -> None:
    m = _load_guard_module()
    payload = {"message": {"caption": "   "}}
    assert m._has_nonempty_caption(payload) is False


def test_wallpaper_guard_detects_ai_technical_failure_reason() -> None:
    m = _load_guard_module()
    assert m._is_ai_technical_failure_reason("ai_api_exception:TimeoutError") is True
    assert m._is_ai_technical_failure_reason("ai_api_unreachable") is True
    assert m._is_ai_technical_failure_reason("ai_guard_block") is False


def test_wallpaper_guard_tech_fail_open_candidate_for_image_only_album() -> None:
    m = _load_guard_module()
    payload = {
        "message": {"text": "", "caption": ""},
        "inputs": [
            {"kind": "photo", "local_name": "photo_1"},
            {"kind": "photo", "local_name": "photo_2"},
            {"kind": "photo", "local_name": "photo_3"},
        ],
    }
    assert m._looks_like_wallpaper_post_for_tech_fail_open(payload) is True


def test_wallpaper_guard_tech_fail_open_candidate_rejects_non_image_media() -> None:
    m = _load_guard_module()
    payload = {
        "message": {"text": "", "caption": ""},
        "inputs": [
            {"kind": "photo", "local_name": "photo_1"},
            {"kind": "video", "local_name": "video_1"},
        ],
    }
    assert m._looks_like_wallpaper_post_for_tech_fail_open(payload) is False
