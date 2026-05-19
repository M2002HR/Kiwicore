#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path


def _load_payload(path: Path) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("payload must be a JSON object")
    return raw


def _is_image_input(item: dict) -> bool:
    kind = str(item.get("kind") or "").strip().lower()
    if kind in {"video", "video_note"}:
        return False
    if kind == "photo":
        return True
    mime = str(item.get("mime_type") or "").strip().lower()
    if mime.startswith("image/"):
        return True
    name = str(item.get("local_name") or item.get("file_name") or "").strip().lower()
    return name.endswith((".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif"))


def _load_image_parts(payload: dict, input_dir: Path) -> list[dict]:
    parts: list[dict] = []
    inputs = payload.get("inputs") or []
    if not isinstance(inputs, list):
        return parts

    for item in inputs:
        if not isinstance(item, dict) or not _is_image_input(item):
            continue
        local_name = str(item.get("local_name") or "").strip()
        local_path = str(item.get("local_path") or "").strip()
        path: Path | None = None
        if local_name:
            path = input_dir / local_name
        elif local_path:
            path = Path(local_path)
        if path is None or not path.exists() or not path.is_file():
            continue

        # Keep prompt payload bounded.
        if path.stat().st_size > 2 * 1024 * 1024:
            continue

        mime = str(item.get("mime_type") or "").strip().lower()
        if not mime:
            guessed, _ = mimetypes.guess_type(path.name)
            mime = guessed or "image/jpeg"

        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        parts.append({"inlineData": {"mimeType": mime, "data": encoded}})
        if len(parts) >= 3:
            break

    return parts


def _build_request(payload: dict, input_dir: Path, model: str | None) -> dict:
    message = payload.get("message") or {}
    text = str(message.get("text") or "").strip()
    caption = str(message.get("caption") or "").strip()

    user_text = (
        "Task: safety moderation for forwarding.\n"
        "Return 0 (block) ONLY for high-confidence unsafe content:\n"
        "1) explicit sexual/pornographic content,\n"
        "2) sexual exploitation,\n"
        "3) severe targeted hate/abuse/slurs,\n"
        "4) direct criminal instructions (hacking, weapon/drug making, fraud steps),\n"
        "5) clearly illegal actionable content.\n"
        "Return 1 (allow) for normal safe content.\n\n"
        "Important allow rules:\n"
        "- News/political/religious discussion is usually ALLOW.\n"
        "- Mentioning channels/usernames/hashtags/links alone is ALLOW.\n"
        "- Non-graphic reports about crime, war, courts, or punishment are ALLOW.\n"
        "- Do not block unless you are highly confident it is unsafe.\n\n"
        "Output strictly one character only: 1 or 0.\n\n"
        f"Message text:\n{text or '<empty>'}\n\n"
        f"Message caption:\n{caption or '<empty>'}\n"
    )

    parts: list[dict] = [{"text": user_text}]
    parts.extend(_load_image_parts(payload, input_dir))

    req: dict = {
        "contents": [
            {
                "role": "user",
                "parts": parts,
            }
        ],
        "generationConfig": {"temperature": 0},
    }
    if model:
        req["model"] = model
    return req


def _extract_text_from_response(data: dict) -> str:
    candidates = data.get("candidates")
    if not isinstance(candidates, list):
        return ""
    for cand in candidates:
        if not isinstance(cand, dict):
            continue
        content = cand.get("content")
        if not isinstance(content, dict):
            continue
        parts = content.get("parts")
        if not isinstance(parts, list):
            continue
        for part in parts:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                return text.strip()
    return ""


def _call_guard_api(endpoint: str, body: dict, timeout_sec: float, *, fail_open: bool) -> tuple[bool, str]:
    # Do not use process proxy env vars for container-local guard endpoint calls.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with opener.open(req, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except urllib.error.URLError:
        return (True, "ai_api_unreachable_fail_open") if fail_open else (False, "ai_api_unreachable")
    except Exception as exc:
        name = exc.__class__.__name__
        return (True, f"ai_api_exception_fail_open:{name}") if fail_open else (False, f"ai_api_exception:{name}")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return (True, "ai_api_invalid_json_fail_open") if fail_open else (False, "ai_api_invalid_json")
    if not isinstance(data, dict):
        return (True, "ai_api_invalid_payload_fail_open") if fail_open else (False, "ai_api_invalid_payload")

    model_text = _extract_text_from_response(data).strip().lower()
    if model_text.startswith("1"):
        return True, "ai_guard_allow"
    if model_text.startswith("0"):
        token = model_text.splitlines()[0].strip()
        reason = "ai_guard_block"
        if ":" in token:
            _, right = token.split(":", 1)
            right = right.strip()
            if right:
                reason = right
        return False, reason
    return (True, "ai_guard_unknown_fail_open") if fail_open else (False, "ai_guard_unknown")


def _is_obvious_advertisement(payload: dict) -> bool:
    message = payload.get("message") or {}
    text = str(message.get("text") or "")
    caption = str(message.get("caption") or "")
    combined = _normalize_ad_text(f"{text}\n{caption}")
    if not combined.strip():
        return False

    if _has_explicit_ad_disclosure(combined):
        return True

    strong_signals = (
        "#ad",
        "#sponsored",
        "sponsored",
        "sponsor",
        "affiliate",
        "referral",
        "تبلیغ",
        "تبلیغات",
        "اسپانسر",
        "اسپانسری",
        "حامی مالی",
        "پروموشن",
        "کد تخفیف",
        "تخفیف ویژه",
    )
    if any(token in combined for token in strong_signals):
        return True

    cta_signals = (
        "buy now",
        "shop now",
        "order now",
        "limited offer",
        "join now",
        "download",
        "download app",
        "try demo",
        "demo now",
        "join",
        "follow us",
        "follow",
        "subscribe",
        "sign up",
        "register now",
        "don’t miss",
        "don't miss",
        "deal",
        "exclusive",
        "vip",
        "خرید",
        "سفارش",
        "ثبت نام",
        "عضویت",
        "فالوش کن",
        "به ما بپیوند",
        "از لینک",
        "لینک خرید",
        "تخفیف",
        "ویژه",
    )
    cta_hits = sum(1 for token in cta_signals if token in combined)

    has_link = bool(re.search(r"(https?://|t\.me/|telegram\.me/|instagram\.com/|bit\.ly/)", combined))
    has_handle = bool(re.search(r"(^|\s)@\w{3,}", combined))
    has_price = bool(re.search(r"(\$|€|£|تومان|ریال|\d+\s*%|\d+\s*(k|m|b)?)", combined))

    # Block explicit commercial ads.
    if has_link and cta_hits >= 1:
        return True
    if has_handle and cta_hits >= 2:
        return True
    if cta_hits >= 2 and has_price:
        return True
    if cta_hits >= 3:
        return True

    if _is_signal_promo_advertisement(combined):
        return True

    if _is_obvious_gambling_advertisement(combined):
        return True

    if _is_obvious_crypto_platform_advertisement(combined):
        return True

    # Channel-promo bundles: only block when multiple promo rows exist.
    list_lines = [ln.strip().lower() for ln in combined.splitlines() if ln.strip()]
    promo_line_hits = 0
    for ln in list_lines:
        has_desc = " - " in ln
        has_ref = ("@" in ln) or ("t.me/" in ln) or ("channel" in ln) or ("telegram" in ln)
        if has_desc and has_ref:
            promo_line_hits += 1
    has_bundle_header = (
        ("channels for" in combined and "follow" in combined)
        or ("follow these" in combined)
        or ("کانال" in combined and "دنبال" in combined)
    )
    if promo_line_hits >= 4:
        return True
    if has_bundle_header and promo_line_hits >= 2:
        return True

    return False


def _has_explicit_ad_disclosure(combined: str) -> bool:
    # Many Telegram ad injections carry explicit disclosure lines like "Ad. 18+" or "Ad by ...".
    disclosure_patterns = (
        r"\bad\s*[.:-]?\s*18\+\b",
        r"\bad\s+by\b",
        r"\badvertisement\b",
        r"\bsponsored\b",
        r"\bpromoted\s+post\b",
    )
    return any(re.search(pattern, combined) for pattern in disclosure_patterns)


def _normalize_ad_text(text: str) -> str:
    normalized = str(text or "").lower()
    normalized = normalized.replace("\u200c", "").replace("\u200d", "").replace("\u200f", "")
    normalized = normalized.replace("٫", ".").replace("。", ".")
    normalized = re.sub(r"[ \t]+", " ", normalized)
    return normalized.strip()


def _is_signal_promo_advertisement(combined: str) -> bool:
    # Covers common pump/signal promos that often evade generic ad keywords.
    signal_terms = (
        "signal",
        "signals",
        "profit",
        "profit margin",
        "high throughput",
        "win rate",
        "100%",
        "100 %",
        "community",
        "deal",
        "vip",
        "trading",
        "trade",
        "forex",
        "crypto signal",
        "premium channel",
        "کانال ویژه",
        "سیگنال",
        "سود",
        "درصد سود",
        "وین ریت",
    )
    signal_hits = sum(1 for token in signal_terms if token in combined)
    if signal_hits <= 0:
        return False

    has_link = bool(re.search(r"(https?://|t\.me/|telegram\.me/|bit\.ly/)", combined))
    has_handle = bool(re.search(r"(^|\s)@\w{3,}", combined))
    cta_terms = (
        "join",
        "register",
        "sign up",
        "don't miss",
        "deal",
        "subscribe",
        "community",
        "عضویت",
        "ثبت نام",
        "فرصت",
        "همین حالا",
    )
    cta_hits = sum(1 for token in cta_terms if token in combined)

    if signal_hits >= 2 and (has_link or has_handle):
        return True
    if signal_hits >= 3:
        return True
    if signal_hits >= 1 and cta_hits >= 2 and (has_link or has_handle):
        return True
    return False


def _is_obvious_gambling_advertisement(combined: str) -> bool:
    strong_brand_signals = (
        "spinarium",
        "bc.game",
        "bc game",
        "bcgame",
        "1xbet",
        "1x bet",
        "bet365",
        "betway",
        "mostbet",
        "melbet",
        "parimatch",
        "favbet",
        "22bet",
        "linebet",
        "betwinner",
        "ggbet",
        "1win",
        "pin-up",
        "pinnacle",
        "stake.com",
        "casino",
        "online casino",
        "sportsbook",
        "bookmaker",
        "bookie",
        "gambling",
        "roulette",
        "blackjack",
        "poker",
        "slot",
        "slots",
        "jackpot",
        "شرط بندی",
        "شرط‌بندی",
        "قمار",
        "کازینو",
        "بوک میکر",
        "بوک‌میکر",
        "کازینو",
    )
    if any(token in combined for token in strong_brand_signals):
        # Brand/site names are strong enough to block on their own.
        return True

    has_age_gate = bool(re.search(r"(\bad[\.\s]*18\+|18\+)", combined))
    has_free_spins = bool(re.search(r"(\bfree\s*spins?\b|\b\d+\s*fs\b)", combined))
    has_slot_context = any(
        token in combined
        for token in (
            "reel",
            "reels",
            "spin",
            "spins",
            "slot machine",
            "jackpot",
            "🎰",
        )
    )
    has_claim_cta = any(token in combined for token in ("claim", "welcome", "gift", "join now", "register now", "sign up"))
    if has_age_gate and (has_free_spins or has_slot_context):
        return True
    if has_free_spins and (has_slot_context or has_claim_cta):
        return True

    generic_gambling_signals = (
        "bet",
        "bets",
        "betting",
        "wager",
        "odds",
        "promo code",
        "bonus",
        "welcome bonus",
        "deposit",
        "withdraw",
        "predictions",
        "prediction",
        "پیش بینی",
        "پیش‌بینی",
        "ضرایب",
        "برد شرط",
        "کد بونوس",
        "بونوس",
    )
    if not any(token in combined for token in generic_gambling_signals):
        return False

    has_link = bool(re.search(r"(https?://|t\.me/|telegram\.me/|instagram\.com/|bit\.ly/)", combined))
    cta_signals = (
        "join now",
        "register now",
        "sign up",
        "claim",
        "promo",
        "bonus",
        "کلیک",
        "ثبت نام",
        "عضویت",
        "همین حالا",
        "از لینک",
        "لینک",
        "واریز",
    )
    cta_hits = sum(1 for token in cta_signals if token in combined)
    promo_code = bool(re.search(r"\b(code|promo|ref|referral)\b", combined))
    if has_link and (cta_hits >= 1 or promo_code):
        return True
    if cta_hits >= 2:
        return True

    return False


def _is_obvious_crypto_platform_advertisement(combined: str) -> bool:
    # Block explicit crypto-platform promotions (exchanges, referral/airdrop campaigns, join CTAs).
    # Normal non-promotional mentions should not be blocked unless ad signals are present.
    crypto_platforms = (
        "binance",
        "bybit",
        "okx",
        "ok-ex",
        "okex",
        "kucoin",
        "bitget",
        "coinex",
        "bingx",
        "mexc",
        "lbank",
        "toobit",
        "xt.com",
        "coinbase",
        "kraken",
        "gate.io",
        "gateio",
        "nobitex",
        "wallex",
        "tabdeal",
        "bitpin",
        "ramzinex",
        "excoino",
        "crypto.com",
        "coinmarketcap",
    )
    crypto_keywords = (
        "crypto",
        "cryptocurrency",
        "blockchain",
        "bitcoin",
        "btc",
        "ethereum",
        "eth",
        "usdt",
        "تتر",
        "صرافی",
        "رمزارز",
        "ارز دیجیتال",
        "کریپتو",
        "کوین",
        "توکن",
        "کیف پول",
        "wallet",
        "airdrop",
        "launchpool",
        "copy trade",
        "copytrade",
        "futures",
        "leverage",
    )
    ad_cta = (
        "register",
        "sign up",
        "join now",
        "join",
        "claim",
        "deposit",
        "withdraw",
        "trade now",
        "buy now",
        "get bonus",
        "invite",
        "invitation",
        "referral",
        "promo",
        "promo code",
        "affiliate",
        "uid",
        "kyc",
        "ثبت نام",
        "ثبت‌نام",
        "عضویت",
        "واریز",
        "برداشت",
        "معامله",
        "بونوس",
        "بوناس",
        "کد دعوت",
        "کد معرف",
        "کد رفرال",
        "رفرال",
        "همین حالا",
        "از لینک",
    )

    has_link = bool(re.search(r"(https?://|t\.me/|telegram\.me/|bit\.ly/|tinyurl\.com/|[a-z0-9-]+\.(com|io|net|org))", combined))
    has_handle = bool(re.search(r"(^|\s)@\w{3,}", combined))
    has_platform = any(token in combined for token in crypto_platforms)
    has_crypto = any(token in combined for token in crypto_keywords)
    cta_hits = sum(1 for token in ad_cta if token in combined)
    has_ref_code = bool(
        re.search(
            r"(\b(ref|referral|promo|invite|code|uid)\b|کد\s*(دعوت|معرف|رفرال))",
            combined,
        )
    )

    # Hard block: explicit exchange/platform promo campaigns.
    if has_platform and (has_link or has_handle or cta_hits >= 1 or has_ref_code):
        return True
    if has_platform and "airdrop" in combined:
        return True

    # Generic crypto ad patterns with clear conversion intent.
    if has_crypto and (has_link or has_handle) and (cta_hits >= 2 or has_ref_code):
        return True
    if ("airdrop" in combined or "launchpool" in combined) and (has_link or has_handle):
        return True
    if ("futures" in combined or "leverage" in combined or "copy trade" in combined or "copytrade" in combined) and (
        has_link or has_handle
    ):
        return True

    return False


def _is_obvious_vpn_config(payload: dict) -> bool:
    return _vpn_match_reason(payload) is not None


def _vpn_match_reason(payload: dict) -> str | None:
    message = payload.get("message") or {}
    text = str(message.get("text") or "")
    caption = str(message.get("caption") or "")
    combined = f"{text}\n{caption}".lower()
    if not combined.strip():
        return None

    strong_keywords = (
        "v2ray",
        "xray",
        "sing-box",
        "singbox",
        "shadowrocket",
        "shadowsocks",
        "outline vpn",
        "wireguard",
        "openvpn",
        "hiddify",
        "nekoray",
        "vless",
        "vmess",
        "trojan",
        "hysteria",
        "tuic",
        "subscription link",
        "sub link",
        "فیلترشکن",
        "وی پی ان",
        "vpn",
        "v p n",
        "پروکسی",
        "کانفیگ",
        "کانفیگ رایگان",
        "خرید کانفیگ",
        "اشتراک v2ray",
        "سابسکریپشن",
    )
    for token in strong_keywords:
        if token in combined:
            return f"keyword={token}"

    # Ambiguous tokens (e.g. clash) need extra VPN context.
    weak_keywords = ("clash", "trojan")
    weak_context = (
        "vpn",
        "v2ray",
        "proxy",
        "پروکسی",
        "config",
        "کانفیگ",
        "subscription",
        "سابسکریپشن",
    )
    for token in weak_keywords:
        if token in combined and any(ctx in combined for ctx in weak_context):
            return f"weak_keyword={token}+context"

    uri_like = (
        "vmess://",
        "vless://",
        "trojan://",
        "ss://",
        "ssr://",
        "hysteria://",
        "hy2://",
        "tuic://",
        "wg://",
    )
    for token in uri_like:
        if token in combined:
            return f"uri={token}"

    # Common format of VPN sub links/config endpoints.
    if re.search(r"(sub|subscription|config|cfg)[-_:/]", combined):
        if "http://" in combined or "https://" in combined:
            return "subscription_or_config_link"

    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", required=True)
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    endpoint = os.getenv("GUARD_AI_ENDPOINT", "http://127.0.0.1:8000/proxy/gemini").strip()
    model_raw = os.getenv("GUARD_AI_MODEL", "").strip()
    model = model_raw or None
    timeout_sec = float(os.getenv("GUARD_AI_TIMEOUT_SEC", "30").strip() or "30")
    enabled = os.getenv("GUARD_AI_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    fail_open = os.getenv("GUARD_AI_FAIL_OPEN", "true").strip().lower() in {"1", "true", "yes", "on"}
    block_ads = os.getenv("GUARD_BLOCK_ADS", "true").strip().lower() in {"1", "true", "yes", "on"}

    if not enabled:
        print("true")
        return

    payload = _load_payload(Path(args.payload))
    if block_ads and _is_obvious_advertisement(payload):
        print("false: obvious_advertisement")
        return
    vpn_reason = _vpn_match_reason(payload)
    if vpn_reason:
        print(f"false: obvious_vpn_config:{vpn_reason}")
        return

    body = _build_request(payload, Path(args.input_dir), model=model)
    allow, reason = _call_guard_api(endpoint=endpoint, body=body, timeout_sec=timeout_sec, fail_open=fail_open)
    if allow:
        print("true")
        return
    print(f"false: {reason}")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        etype = exc.__class__.__name__
        emsg = str(exc).strip().replace("\n", " ")[:240]
        if emsg:
            print(f"false: guard_exception:{etype}:{emsg}")
        else:
            print(f"false: guard_exception:{etype}")
        sys.exit(0)
