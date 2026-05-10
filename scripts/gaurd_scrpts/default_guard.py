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


def _call_guard_api(endpoint: str, body: dict, timeout_sec: float, *, fail_open: bool) -> bool:
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
        return True if fail_open else False

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return True if fail_open else False
    if not isinstance(data, dict):
        return True if fail_open else False

    model_text = _extract_text_from_response(data).strip().lower()
    if model_text.startswith("1"):
        return True
    if model_text.startswith("0"):
        return False
    return True if fail_open else False


def _is_obvious_advertisement(payload: dict) -> bool:
    message = payload.get("message") or {}
    text = str(message.get("text") or "")
    caption = str(message.get("caption") or "")
    combined = f"{text}\n{caption}".lower()
    if not combined.strip():
        return False

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
        "follow us",
        "subscribe",
        "sign up",
        "register now",
        "خرید",
        "سفارش",
        "ثبت نام",
        "عضویت",
        "فالوش کن",
        "به ما بپیوند",
        "از لینک",
        "لینک خرید",
        "تخفیف",
    )
    cta_hits = sum(1 for token in cta_signals if token in combined)

    has_link = bool(re.search(r"(https?://|t\.me/|telegram\.me/|instagram\.com/|bit\.ly/)", combined))
    has_price = bool(re.search(r"(\$|€|£|تومان|ریال|\d+\s*%|\d+\s*(k|m|b)?)", combined))

    # Keep this strict for obvious ads while reducing random false positives.
    if has_link and cta_hits >= 1:
        return True
    if cta_hits >= 2 and has_price:
        return True
    if cta_hits >= 3:
        return True

    return False


def _is_obvious_vpn_config(payload: dict) -> bool:
    message = payload.get("message") or {}
    text = str(message.get("text") or "")
    caption = str(message.get("caption") or "")
    combined = f"{text}\n{caption}".lower()
    if not combined.strip():
        return False

    keywords = (
        "v2ray",
        "xray",
        "sing-box",
        "singbox",
        "clash",
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
    if any(token in combined for token in keywords):
        return True

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
    if any(token in combined for token in uri_like):
        return True

    # Common format of VPN sub links/config endpoints.
    if re.search(r"(sub|subscription|config|cfg)[-_:/]", combined):
        if "http://" in combined or "https://" in combined:
            return True

    return False


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

    if not enabled:
        print("true")
        return

    payload = _load_payload(Path(args.payload))
    if _is_obvious_advertisement(payload):
        print("false: obvious_advertisement")
        return
    if _is_obvious_vpn_config(payload):
        print("false: obvious_vpn_config")
        return

    body = _build_request(payload, Path(args.input_dir), model=model)
    allow = _call_guard_api(endpoint=endpoint, body=body, timeout_sec=timeout_sec, fail_open=fail_open)
    print("true" if allow else "false: ai_guard_block")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("false")
        sys.exit(0)
