#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

CAPTION_TYPES = {"photo", "video", "voice", "audio", "document", "animation"}
IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif")
AI_META_LINE_RE = re.compile(
    r"^(goal:|constraint:|input:|output:|note:|explanation:|analysis:|prompt:|\*|\-|\d+\.|```)",
    re.IGNORECASE,
)
LATIN_WORD_RE = re.compile(r"\b[A-Za-z]{2,}\b")
PERSIAN_CHAR_RE = re.compile(r"[\u0600-\u06FF]")
PROMPT_LEAK_RE = re.compile(
    r"(professional persian football content writer|raw text|captions|ready-to-publish|rules|translation|rewrite)",
    re.IGNORECASE,
)
URL_RE = re.compile(r"(https?://\S+|www\.\S+|t\.me/\S+|telegram\.me/\S+)", re.IGNORECASE)
MENTION_RE = re.compile(r"(?<!\w)@[A-Za-z0-9_]{3,}(?!\w)")
HASHTAG_RE = re.compile(r"(?<!\w)#[\w_]+")
MEDIA_OUTPUT_TYPES = {"photo", "video", "voice", "audio", "document", "animation", "video_note"}
EMOJI_JOINER = "\u200d"
EMOJI_VARIATION = "\ufe0f"
EMOJI_KEYCAP = "\u20e3"

try:
    from default_channel_script import build_passthrough_messages as _default_build_passthrough_messages
except Exception:
    _default_build_passthrough_messages = None


def _load_payload(path: Path) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("payload must be a JSON object")
    return raw


def _normalize_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    lines = [line.strip() for line in text.split("\n")]
    lines = [line for line in lines if line]
    return "\n".join(lines).strip()


def _normalize_multiline_text(text: str) -> str:
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    out: list[str] = []
    prev_blank = False
    for raw in text.split("\n"):
        line = re.sub(r"[ \t]+", " ", raw).strip()
        if not line:
            if out and not prev_blank:
                out.append("")
            prev_blank = True
            continue
        out.append(line)
        prev_blank = False
    while out and out[0] == "":
        out.pop(0)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out).strip()


def _resolve_local_name(item: dict) -> str | None:
    local_name = item.get("local_name")
    if isinstance(local_name, str) and local_name.strip():
        return local_name.strip()
    local_path = item.get("local_path")
    if isinstance(local_path, str) and local_path.strip():
        return Path(local_path).name
    return None


def _build_fallback_passthrough_messages(payload: dict) -> list[dict]:
    message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    inputs = payload.get("inputs") if isinstance(payload.get("inputs"), list) else []

    text = message.get("text")
    caption = message.get("caption")
    text = _normalize_text(text) if isinstance(text, str) and text.strip() else None
    caption = _normalize_text(caption) if isinstance(caption, str) and caption.strip() else None

    out: list[dict] = []
    if not inputs and text:
        return [{"type": "text", "text": text}]

    caption_assigned = False
    for item in inputs:
        if not isinstance(item, dict):
            continue
        raw_kind = str(item.get("kind") or "").strip().lower()
        if raw_kind == "sticker":
            continue

        msg_type = raw_kind if raw_kind in {"photo", "video", "voice", "audio", "document", "animation", "video_note"} else "document"
        local_name = _resolve_local_name(item)
        if not local_name:
            continue

        obj: dict[str, object] = {"type": msg_type, "path": local_name}
        if not caption_assigned and msg_type in CAPTION_TYPES and (caption or text):
            obj["caption"] = caption or text
            caption_assigned = True
        out.append(obj)

    if text and (not out or not caption_assigned):
        out.insert(0, {"type": "text", "text": text})
    elif caption and not out:
        out.append({"type": "text", "text": caption})

    return out


def _build_base_messages(payload: dict) -> list[dict]:
    if _default_build_passthrough_messages is not None:
        try:
            out = _default_build_passthrough_messages(payload)
            if isinstance(out, list):
                return out
        except Exception:
            pass
    return _build_fallback_passthrough_messages(payload)


def _football_ai_enabled() -> bool:
    raw = os.getenv("CHANNEL_SCRIPT_AI_ENABLED", "true").strip()
    return raw.lower() in {"1", "true", "yes", "on"}


def _pick_ai_endpoint() -> str:
    return (
        os.getenv("CHANNEL_SCRIPT_AI_ENDPOINT", "").strip()
        or os.getenv("SCRIPT_CLEAN_AI_ENDPOINT", "").strip()
        or os.getenv("GUARD_AI_ENDPOINT", "").strip()
    )


def _pick_ai_model() -> str:
    return (
        os.getenv("CHANNEL_SCRIPT_AI_MODEL", "").strip()
        or os.getenv("SCRIPT_CLEAN_AI_MODEL", "").strip()
        or os.getenv("GUARD_AI_MODEL", "").strip()
    )


def _ai_timeout_sec() -> float:
    raw = os.getenv("CHANNEL_SCRIPT_AI_TIMEOUT_SEC", "20").strip() or "20"
    try:
        return max(8.0, min(60.0, float(raw)))
    except Exception:
        return 20.0


def _ai_retry_count() -> int:
    raw = os.getenv("CHANNEL_SCRIPT_AI_RETRY_COUNT", "2").strip() or "2"
    try:
        retries = max(0, min(3, int(raw)))
    except Exception:
        retries = 2
    if _ai_mandatory_mode():
        return max(2, retries)
    return retries


def _ai_fail_open() -> bool:
    # In mandatory mode, never allow silent passthrough fallback.
    # This prevents raw/unprocessed posts from being forwarded when AI is unavailable.
    if _ai_mandatory_mode():
        return False
    raw = os.getenv("CHANNEL_SCRIPT_AI_FAIL_OPEN", "false").strip()
    return raw.lower() in {"1", "true", "yes", "on"}


def _ai_max_images() -> int:
    raw = os.getenv("CHANNEL_SCRIPT_AI_MAX_IMAGES", "3").strip() or "3"
    try:
        return max(0, min(6, int(raw)))
    except Exception:
        return 3


def _ai_total_budget_sec() -> float:
    raw = os.getenv("CHANNEL_SCRIPT_AI_TOTAL_BUDGET_SEC", "75").strip() or "75"
    try:
        return max(15.0, min(110.0, float(raw)))
    except Exception:
        return 75.0


def _ai_mandatory_mode() -> bool:
    raw = os.getenv("CHANNEL_SCRIPT_AI_MANDATORY", "true").strip()
    forced = raw.lower() in {"1", "true", "yes", "on"}
    return forced and _football_ai_enabled()


def _destination_signature(payload: dict) -> str:
    route = payload.get("route") or {}
    if not isinstance(route, dict):
        return ""
    for key in ("destination_target", "destination_channel_username", "destination_channel_id"):
        value = route.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _collect_source_text(payload: dict) -> str:
    message = payload.get("message") or {}
    if not isinstance(message, dict):
        message = {}

    parts: list[str] = []
    text = message.get("text")
    caption = message.get("caption")

    if isinstance(text, str) and text.strip():
        parts.append(f"TEXT:\n{text.strip()}")
    if isinstance(caption, str) and caption.strip() and caption.strip() != (text.strip() if isinstance(text, str) else ""):
        parts.append(f"CAPTION:\n{caption.strip()}")

    return "\n\n".join(parts).strip()


def _collect_source_content_text(payload: dict) -> str:
    message = payload.get("message") or {}
    if not isinstance(message, dict):
        return ""

    text = str(message.get("text") or "").strip()
    caption = str(message.get("caption") or "").strip()
    parts: list[str] = []
    if text:
        parts.append(text)
    if caption and caption != text:
        parts.append(caption)
    return _normalize_multiline_text("\n\n".join(parts))


def _source_length_bounds(source_content: str) -> tuple[int, int, str]:
    normalized = re.sub(r"\s+", " ", str(source_content or "")).strip()
    n = len(normalized)
    if n <= 0:
        return (55, 220, "medium")
    if n < 90:
        low = max(18, int(n * 0.55))
        high = max(low + 28, int(n * 1.70))
        return (low, min(220, high), "short")
    if n <= 260:
        low = max(70, int(n * 0.65))
        high = max(low + 40, int(n * 1.45))
        return (low, min(430, high), "medium")
    low = max(170, int(n * 0.60))
    high = max(low + 70, int(n * 1.35))
    return (low, min(700, high), "long")


def _source_length_instruction(source_content: str) -> str:
    low, high, bucket = _source_length_bounds(source_content)
    if bucket == "short":
        bucket_hint = "کوتاه"
    elif bucket == "long":
        bucket_hint = "بلند"
    else:
        bucket_hint = "متوسط"
    return (
        f"طول خروجی باید نزدیک طول ورودی باشد (سطح {bucket_hint}). "
        f"حدود طول مناسب: بین {low} تا {high} کاراکتر."
    )


def _content_length_without_signature(text: str, *, destination: str = "") -> int:
    body = _normalize_for_language_quality(text, destination=destination)
    compact = re.sub(r"\s+", " ", body).strip()
    return len(compact)


def _is_length_acceptable(text: str, *, source_content: str, destination: str = "") -> bool:
    if not text:
        return False
    low, high, _ = _source_length_bounds(source_content)
    size = _content_length_without_signature(text, destination=destination)
    return low <= size <= high


def _source_layout_hint(payload: dict) -> tuple[int, bool]:
    source = _collect_source_content_text(payload)
    if not source:
        return (1, False)
    lines = [line for line in source.splitlines() if line.strip()]
    has_multiline = len(lines) >= 2 or "\n\n" in source
    target_lines = max(1, min(4, len(lines))) if has_multiline else 1
    return (target_lines, has_multiline)


def _source_layout_instruction(payload: dict) -> str:
    target_lines, multiline = _source_layout_hint(payload)
    if not multiline:
        return "خروجی را خوانا نگه دار و از شکستن بی‌دلیل خط خودداری کن."
    if target_lines <= 2:
        return "فرمت خروجی را مثل ورودی نگه دار و با دو خط مجزا بنویس."
    return f"فرمت خروجی را مثل ورودی نگه دار و حدود {target_lines} خط خوانا با newline مناسب بده."


def _source_tone_instruction(payload: dict) -> str:
    source = _collect_source_content_text(payload)
    if not source:
        return "لحن خروجی را هم‌لحن با ورودی نگه دار."

    normalized = str(source)
    lowered = normalized.lower()
    token_count = len(re.findall(r"\S+", normalized))

    formal_markers = (
        "می باشد",
        "می‌باشد",
        "خواهد شد",
        "اعلام کرد",
        "اعلام کرد که",
        "گزارش",
        "رسانه",
        "باشگاه",
        "سرمربی",
        "دیدار",
        "مسابقه",
        "بر اساس",
        "به گفته",
        "طبق",
    )
    casual_markers = (
        "خیلی",
        "باحال",
        "خفن",
        "دمش گرم",
        "بچه‌ها",
        "بچه ها",
        "رفیقا",
        "داداش",
        "وای",
        "عه",
        "وااای",
        "هههه",
        "خخخ",
        "lol",
        "lmao",
        "ngl",
        "bro",
    )

    formal_score = sum(1 for marker in formal_markers if marker in normalized or marker in lowered)
    casual_score = sum(1 for marker in casual_markers if marker in normalized or marker in lowered)

    emoji_count = len(_extract_emoji_tokens(normalized))
    exclamations = normalized.count("!") + normalized.count("！")
    question_marks = normalized.count("?") + normalized.count("؟")
    if emoji_count >= 1:
        casual_score += 1
    if exclamations >= 2:
        casual_score += 1
    if question_marks >= 2:
        casual_score += 1
    if token_count >= 14 and emoji_count == 0 and exclamations == 0:
        formal_score += 1

    if casual_score >= formal_score + 2:
        return (
            "لحن خروجی را خودمانی و غیررسمیِ کنترل‌شده نگه دار؛ "
            "جمله‌ها صمیمی و هماهنگ با ورودی باشند و بی‌دلیل رسمی‌سازی نکن."
        )
    if formal_score >= casual_score + 1:
        return (
            "لحن خروجی را رسمی، حرفه‌ای و خبری/تحلیلی نگه دار؛ "
            "از واژه‌های محاوره‌ای و خودمانیِ اضافی پرهیز کن."
        )
    return (
        "لحن خروجی را با لحن ورودی هماهنگ نگه دار؛ "
        "نه بیش از حد رسمی و نه بیش از حد محاوره‌ای."
    )


def _has_textual_source(payload: dict) -> bool:
    message = payload.get("message")
    if not isinstance(message, dict):
        return False
    text = str(message.get("text") or "").strip()
    caption = str(message.get("caption") or "").strip()
    return bool(text or caption)


def _has_video_input(payload: dict) -> bool:
    inputs = payload.get("inputs")
    if not isinstance(inputs, list):
        return False
    for item in inputs:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip().lower()
        if kind in {"video", "video_note"}:
            return True
        mime = str(item.get("mime_type") or "").strip().lower()
        if mime.startswith("video/"):
            return True
    return False


def _is_promotional_text(text: str) -> bool:
    combined = str(text or "").strip().lower()
    if not combined:
        return False
    has_link = bool(re.search(r"(https?://|t\.me/|telegram\.me/|bit\.ly/)", combined))
    has_handle = bool(re.search(r"(^|\s)@\w{3,}", combined))
    cta_terms = (
        "buy now",
        "shop now",
        "order now",
        "join",
        "join now",
        "register",
        "sign up",
        "subscribe",
        "don't miss",
        "don’t miss",
        "deal",
        "vip",
        "exclusive",
        "خرید",
        "ثبت نام",
        "عضویت",
        "فرصت",
        "همین حالا",
        "ویژه",
    )
    cta_hits = sum(1 for token in cta_terms if token in combined)

    promo_terms = (
        "#ad",
        "sponsored",
        "affiliate",
        "referral",
        "تبلیغ",
        "اسپانسر",
        "پروموشن",
        "سیگنال",
        "signal",
        "signals",
        "profit",
        "profit margin",
        "100%",
        "100 %",
        "high throughput",
        "win rate",
        "crypto signal",
        "forex",
        "trading",
        "premium channel",
        "community",
        "سود",
        "درصد سود",
        "وین ریت",
    )
    promo_hits = sum(1 for token in promo_terms if token in combined)

    if promo_hits >= 2 and (has_link or has_handle):
        return True
    if promo_hits >= 3:
        return True
    if cta_hits >= 3 and (has_link or has_handle):
        return True
    return False


def _is_promotional_payload(payload: dict) -> bool:
    message = payload.get("message")
    if not isinstance(message, dict):
        return False
    text = str(message.get("text") or "").strip()
    caption = str(message.get("caption") or "").strip()
    combined = "\n".join([text, caption]).strip()
    return _is_promotional_text(combined)


def _is_promotional_messages(messages: list[dict]) -> bool:
    for item in messages:
        if not isinstance(item, dict):
            continue
        text = str(item.get("text") or "").strip()
        caption = str(item.get("caption") or "").strip()
        if text and _is_promotional_text(text):
            return True
        if caption and _is_promotional_text(caption):
            return True
    return False


def _safe_fail_open_messages(base: list[dict], *, destination: str = "") -> list[dict]:
    # Fail-open mode must keep transfer continuity.
    # Only block clear promotional content and unsafe non-Persian content.
    # Never inject synthetic fallback captions/text.
    out: list[dict] = []
    for item in base:
        if not isinstance(item, dict):
            continue
        obj = dict(item)
        msg_type = str(obj.get("type") or "").strip().lower()
        if msg_type == "text":
            text = str(obj.get("text") or "").strip()
            if text and not _is_promotional_text(text) and _is_persian_acceptable(text, destination=destination):
                out.append(obj)
            continue
        if msg_type in CAPTION_TYPES:
            caption = str(obj.get("caption") or "").strip()
            if caption and _is_promotional_text(caption):
                # Keep media item, strip only promotional caption.
                obj.pop("caption", None)
                caption = ""
            if caption and not _is_persian_acceptable(caption, destination=destination):
                obj.pop("caption", None)
            out.append(obj)
            continue
        out.append(obj)
    return out


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
    return name.endswith(IMAGE_EXTS)


def _load_image_parts(payload: dict, input_dir: Path) -> list[dict]:
    max_images = _ai_max_images()
    if max_images <= 0:
        return []

    parts: list[dict] = []
    inputs = payload.get("inputs") or []
    if not isinstance(inputs, list):
        return parts

    for item in inputs:
        if len(parts) >= max_images:
            break
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

        if path.stat().st_size > 3 * 1024 * 1024:
            continue

        mime = str(item.get("mime_type") or "").strip().lower()
        if not mime:
            guessed, _ = mimetypes.guess_type(path.name)
            mime = guessed or "image/jpeg"

        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        parts.append({"inlineData": {"mimeType": mime, "data": encoded}})

    return parts


def _football_prompt(source_text: str, destination: str, *, length_instruction: str, layout_instruction: str) -> str:
    return (
        "تو یک نویسنده حرفه‌ای محتوای فوتبالی فارسی هستی.\n"
        "ورودی می‌تواند متن خام، کپشن، یا تصاویر فوتبال باشد.\n"
        "یک پست فارسی طبیعی، روان و آماده انتشار بنویس.\n\n"
        "قوانین قطعی:\n"
        "1) ترجمه تحت‌اللفظی ممنوع؛ مفهوم را حرفه‌ای بازنویسی کن.\n"
        "2) فقط از اطلاعات موجود در ورودی استفاده کن؛ چیزی اضافه یا جعل نکن.\n"
        "3) متن بدون غلط املایی، نگارشی و گرامری باشد.\n"
        "4) حداکثر 4 جمله و کاملا خوش‌خوان باشد.\n"
        "5) لحن خبری/تحلیلی فوتبالی داشته باشد، نه رباتی.\n"
        "6) هیچ توضیح فرامتنی، لیست، یا قالب Markdown نده.\n"
        "7) لینک و هشتگ تبلیغی نیاور.\n"
        "8) اگر امضای مقصد داده شده، فقط در خط آخر و فقط یک بار بیاور.\n\n"
        "9) ایموجی‌های معنادار ورودی را حذف نکن و در خروجی به‌شکل طبیعی حفظ کن.\n\n"
        f"10) {length_instruction}\n"
        f"11) {layout_instruction}\n\n"
        f"امضای مقصد: {destination or '<none>'}\n\n"
        f"ورودی:\n{source_text or '<empty>'}\n\n"
        "اگر متن ورودی خالی بود، فقط با اتکا به تصاویر/مدیای ورودی یک کپشن فوتبالی مرتبط تولید کن."
    )


def _call_gemini_text(*, endpoint: str, body: dict, timeout_sec: float) -> str | None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )

    retries = _ai_retry_count()
    deadline = time.monotonic() + max(1.0, float(timeout_sec))
    for attempt in range(retries + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0.8:
            return None
        try:
            with opener.open(req, timeout=max(1.0, remaining)) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except Exception:
            if attempt >= retries:
                return None
            backoff = 0.25 * (attempt + 1)
            if deadline - time.monotonic() <= backoff:
                return None
            time.sleep(backoff)
            continue

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            if attempt >= retries:
                return None
            backoff = 0.15 * (attempt + 1)
            if deadline - time.monotonic() <= backoff:
                return None
            time.sleep(backoff)
            continue

        if not isinstance(data, dict):
            if attempt >= retries:
                return None
            backoff = 0.15 * (attempt + 1)
            if deadline - time.monotonic() <= backoff:
                return None
            time.sleep(backoff)
            continue

        candidates = data.get("candidates")
        if not isinstance(candidates, list):
            if attempt >= retries:
                return None
            backoff = 0.15 * (attempt + 1)
            if deadline - time.monotonic() <= backoff:
                return None
            time.sleep(backoff)
            continue

        for cand in candidates:
            if not isinstance(cand, dict):
                continue
            content = cand.get("content")
            if not isinstance(content, dict):
                continue
            parts = content.get("parts")
            if not isinstance(parts, list):
                continue
            # Prefer non-thinking output parts when model returns internal reasoning.
            for part in parts:
                if not isinstance(part, dict):
                    continue
                if bool(part.get("thought")):
                    continue
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    return text.strip()
            for part in parts:
                if not isinstance(part, dict):
                    continue
                text = part.get("text")
                if isinstance(text, str) and text.strip():
                    return text.strip()

    return None


def _strip_md_fence(text: str) -> str:
    out = text.strip()
    if out.startswith("```"):
        out = re.sub(r"^```[a-zA-Z]*\n?", "", out)
        out = re.sub(r"\n?```$", "", out)
    return out.strip()


def _sanitize_ai_output(text: str) -> str:
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if AI_META_LINE_RE.search(line):
            continue
        if PROMPT_LEAK_RE.search(line):
            continue
        lines.append(line)
    return _normalize_multiline_text("\n".join(lines))


def _is_latin_heavy_line(line: str) -> bool:
    tokens = re.findall(r"\S+", line)
    if not tokens:
        return False
    latin_tokens = len(LATIN_WORD_RE.findall(line))
    return (latin_tokens / max(1, len(tokens))) >= 0.6


def _strip_prompt_leakage_lines(text: str, *, destination: str = "") -> str:
    destination_line = destination.strip()
    kept: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if destination_line and line == destination_line:
            kept.append(line)
            continue
        if PROMPT_LEAK_RE.search(line):
            continue
        if _is_latin_heavy_line(line) and not PERSIAN_CHAR_RE.search(line):
            continue
        kept.append(line)
    return _normalize_multiline_text("\n".join(kept))


def _dedupe_lines(text: str) -> str:
    seen: set[str] = set()
    out: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        key = re.sub(r"\s+", " ", line).strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(line)
    return _normalize_multiline_text("\n".join(out))


def _normalize_signature(text: str, destination: str) -> str:
    out = text
    if destination:
        had_signature = False
        out_lines: list[str] = []
        for raw in out.splitlines():
            line = raw.strip()
            if not line:
                continue
            if line == destination:
                had_signature = True
                continue
            out_lines.append(line)
        out = _normalize_multiline_text("\n".join(out_lines))
        if had_signature and out:
            out = f"{out}\n{destination}"
        elif had_signature and not out:
            out = destination
    return _normalize_multiline_text(out)


def _is_emoji_base_char(ch: str) -> bool:
    if not ch:
        return False
    cp = ord(ch)
    return (
        0x1F300 <= cp <= 0x1FAFF
        or 0x2600 <= cp <= 0x27BF
        or cp in {0x00A9, 0x00AE, 0x203C, 0x2049, 0x2122, 0x2139, 0x3030, 0x303D, 0x3297, 0x3299}
    )


def _is_regional_indicator(ch: str) -> bool:
    if not ch:
        return False
    cp = ord(ch)
    return 0x1F1E6 <= cp <= 0x1F1FF


def _is_skin_tone(ch: str) -> bool:
    if not ch:
        return False
    cp = ord(ch)
    return 0x1F3FB <= cp <= 0x1F3FF


def _extract_emoji_tokens(text: str) -> list[str]:
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if _is_regional_indicator(ch) and i + 1 < n and _is_regional_indicator(text[i + 1]):
            out.append(text[i : i + 2])
            i += 2
            continue

        if ch in "0123456789#*":
            if i + 1 < n and text[i + 1] == EMOJI_KEYCAP:
                out.append(text[i : i + 2])
                i += 2
                continue
            if i + 2 < n and text[i + 1] == EMOJI_VARIATION and text[i + 2] == EMOJI_KEYCAP:
                out.append(text[i : i + 3])
                i += 3
                continue

        if not _is_emoji_base_char(ch):
            i += 1
            continue

        start = i
        i += 1
        if i < n and text[i] == EMOJI_VARIATION:
            i += 1
        if i < n and _is_skin_tone(text[i]):
            i += 1

        while i + 1 < n and text[i] == EMOJI_JOINER and _is_emoji_base_char(text[i + 1]):
            i += 2
            if i < n and text[i] == EMOJI_VARIATION:
                i += 1
            if i < n and _is_skin_tone(text[i]):
                i += 1

        out.append(text[start:i])
    return out


def _source_emoji_tokens(payload: dict) -> list[str]:
    message = payload.get("message")
    if not isinstance(message, dict):
        return []

    source_parts: list[str] = []
    for key in ("text", "caption"):
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            source_parts.append(value)
    if not source_parts:
        return []

    extracted: list[str] = []
    for part in source_parts:
        extracted.extend(_extract_emoji_tokens(part))

    unique: list[str] = []
    seen: set[str] = set()
    for token in extracted:
        if token in seen:
            continue
        seen.add(token)
        unique.append(token)
    return unique


def _preserve_source_emojis(text: str, *, destination: str, source_emojis: list[str]) -> str:
    if not text or not source_emojis:
        return text

    current = set(_extract_emoji_tokens(text))
    missing = [token for token in source_emojis if token not in current]
    if not missing:
        return text

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    bundle = " ".join(missing).strip()
    if not bundle:
        return text

    if not lines:
        return bundle

    if destination and lines[-1] == destination:
        body = lines[:-1]
        if body:
            body[0] = f"{bundle} {body[0]}".strip()
        else:
            body = [bundle]
        return _normalize_multiline_text("\n".join([*body, destination]))

    lines[0] = f"{bundle} {lines[0]}".strip()
    return _normalize_multiline_text("\n".join(lines))


def _split_sentences_for_layout(text: str) -> list[str]:
    compact = _normalize_multiline_text(text)
    if not compact:
        return []
    compact = re.sub(r"\s*\n\s*", " ", compact).strip()
    parts = re.split(r"(?<=[\.\!\?؟])\s+", compact)
    out = [part.strip() for part in parts if part and part.strip()]
    return out


def _apply_layout_hint(text: str, *, target_lines: int, prefer_multiline: bool, destination: str = "") -> str:
    normalized = _normalize_multiline_text(text)
    if not normalized or not prefer_multiline:
        return normalized
    body_lines = [line for line in normalized.splitlines() if line.strip()]
    dst = str(destination or "").strip()
    has_signature = bool(dst and body_lines and body_lines[-1] == dst)
    if has_signature:
        body_lines = body_lines[:-1]
    if len(body_lines) >= 2:
        return normalized

    target = max(2, min(4, int(target_lines or 2)))
    sentences = _split_sentences_for_layout("\n".join(body_lines))
    if len(sentences) < 2:
        text_line = " ".join(body_lines).strip()
        comma_parts = [p.strip() for p in text_line.split("،") if p.strip()]
        if len(comma_parts) >= 2:
            sentences = [f"{p}." if not re.search(r"[.!؟?]$", p) else p for p in comma_parts]
    if len(sentences) < 2:
        return normalized

    line_count = min(target, len(sentences))
    chunk_size = max(1, (len(sentences) + line_count - 1) // line_count)
    new_lines: list[str] = []
    idx = 0
    while idx < len(sentences):
        chunk = sentences[idx : idx + chunk_size]
        new_lines.append(" ".join(chunk).strip())
        idx += chunk_size
    if has_signature:
        new_lines.append(dst)
    return _normalize_multiline_text("\n".join(new_lines))


def _postprocess_generated_text(text: str, *, destination: str, target_lines: int = 1, prefer_multiline: bool = False) -> str:
    out = _strip_md_fence(text)
    out = _sanitize_ai_output(out)
    out = _strip_prompt_leakage_lines(out, destination=destination)
    out = _dedupe_lines(out)
    out = _normalize_signature(out, destination)
    out = _apply_layout_hint(out, target_lines=target_lines, prefer_multiline=prefer_multiline, destination=destination)
    return _normalize_multiline_text(out)


def _looks_low_quality(text: str) -> bool:
    if not text:
        return True
    if len(text) < 18:
        return True
    if not PERSIAN_CHAR_RE.search(text):
        return True
    if PROMPT_LEAK_RE.search(text):
        return True
    latin_ratio = len(LATIN_WORD_RE.findall(text)) / max(1, len(re.findall(r"\S+", text)))
    return latin_ratio > 0.35


def _normalize_for_language_quality(text: str, *, destination: str = "") -> str:
    if not text:
        return ""
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    dst = str(destination or "").strip()
    kept: list[str] = []
    for line in lines:
        if dst and line == dst:
            continue
        kept.append(line)
    out = _normalize_text("\n".join(kept))
    out = URL_RE.sub(" ", out)
    out = HASHTAG_RE.sub(" ", out)
    out = MENTION_RE.sub(" ", out)
    out = re.sub(r"[ \t]+", " ", out).strip()
    return out


def _is_persian_acceptable(text: str, *, destination: str = "") -> bool:
    if not text:
        return False
    normalized = _normalize_for_language_quality(text, destination=destination)
    if not normalized:
        return False
    if not PERSIAN_CHAR_RE.search(normalized):
        return False
    tokens = re.findall(r"\S+", normalized)
    if not tokens:
        return False
    latin_ratio = len(LATIN_WORD_RE.findall(normalized)) / max(1, len(tokens))
    return latin_ratio <= 0.2


def _has_media_without_caption(messages: list[dict]) -> bool:
    for item in messages:
        if not isinstance(item, dict):
            continue
        msg_type = str(item.get("type") or "").strip().lower()
        if msg_type not in MEDIA_OUTPUT_TYPES:
            continue
        caption = item.get("caption")
        if not isinstance(caption, str) or not caption.strip():
            return True
    return False


def _has_any_media(messages: list[dict]) -> bool:
    for item in messages:
        if not isinstance(item, dict):
            continue
        msg_type = str(item.get("type") or "").strip().lower()
        if msg_type in MEDIA_OUTPUT_TYPES:
            return True
    return False


def _fallback_source_caption(payload: dict) -> str | None:
    message = payload.get("message")
    if not isinstance(message, dict):
        return None
    caption = message.get("caption")
    if isinstance(caption, str) and caption.strip():
        return _normalize_text(caption)
    return None


def _has_meaningful_source(payload: dict) -> bool:
    message = payload.get("message")
    if isinstance(message, dict):
        text = str(message.get("text") or "").strip()
        caption = str(message.get("caption") or "").strip()
        if text or caption:
            return True

    inputs = payload.get("inputs")
    if not isinstance(inputs, list):
        return False
    for item in inputs:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "").strip().lower()
        if kind and kind != "sticker":
            return True
    return False


def _generate_football_text(*, payload: dict, input_dir: Path, base_messages: list[dict]) -> str | None:
    source_text = _collect_source_text(payload)
    source_content = _collect_source_content_text(payload)
    destination = _destination_signature(payload)
    source_emojis = _source_emoji_tokens(payload)
    target_lines, prefer_multiline = _source_layout_hint(payload)
    length_instruction = _source_length_instruction(source_content)
    layout_instruction = _source_layout_instruction(payload)
    tone_instruction = _source_tone_instruction(payload)
    prompt = _football_prompt(
        source_text,
        destination=destination,
        length_instruction=length_instruction,
        layout_instruction=f"{layout_instruction}\n{tone_instruction}",
    )

    if not _football_ai_enabled():
        return None

    endpoint = _pick_ai_endpoint()
    if not endpoint:
        return None

    image_parts = _load_image_parts(payload, input_dir)
    active_image_parts = list(image_parts)
    has_visual_context = bool(active_image_parts)
    if not source_text and not has_visual_context:
        return None

    deadline = time.monotonic() + _ai_total_budget_sec()

    def _remaining_budget() -> float:
        return max(0.0, deadline - time.monotonic())

    def _call_with_budget(body: dict) -> str | None:
        remaining = _remaining_budget()
        if remaining <= 2.0:
            return None
        call_budget = min(_ai_timeout_sec(), remaining)
        return _call_gemini_text(endpoint=endpoint, body=body, timeout_sec=call_budget)

    def _build_body(parts: list[dict], *, temperature: float) -> dict:
        body: dict = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": {"temperature": temperature},
        }
        model = _pick_ai_model()
        if model:
            body["model"] = model
        return body

    model = _pick_ai_model()

    def _prompt_parts(prompt_text: str) -> list[dict]:
        if active_image_parts:
            return [{"text": prompt_text}, *active_image_parts]
        return [{"text": prompt_text}]

    first = _call_with_budget(_build_body(_prompt_parts(prompt), temperature=0.2))
    if first is None and active_image_parts and source_text:
        # Some image sets can be intermittently blocked/refused by model safety.
        # Keep mandatory AI flow alive by retrying the same prompt as text-only.
        active_image_parts = []
        has_visual_context = False
        first = _call_with_budget(_build_body(_prompt_parts(prompt), temperature=0.2))
    if first is None:
        return "" if not _ai_fail_open() else None

    cleaned = _postprocess_generated_text(
        first,
        destination=destination,
        target_lines=target_lines,
        prefer_multiline=prefer_multiline,
    )
    cleaned = _preserve_source_emojis(cleaned, destination=destination, source_emojis=source_emojis)

    if (
        (_looks_low_quality(cleaned) or not _is_length_acceptable(cleaned, source_content=source_content, destination=destination))
        and _remaining_budget() > 6.0
    ):
        refine_prompt = (
            "متن زیر را به یک پست فوتبالی فارسی روان، طبیعی و آماده انتشار بازنویسی کن.\n"
            "هیچ توضیحی اضافه نکن و فقط نسخه نهایی را بده.\n"
            "غلط املایی/نگارشی نداشته باشد.\n"
            "حداکثر 4 جمله.\n\n"
            "ایموجی‌های اصلی متن را حذف نکن.\n\n"
            "هیچ خط انگلیسی یا توضیح فرامتنی نیاور.\n"
            "اگر امضای مقصد وجود دارد، فقط یک‌بار در خط آخر بیاور.\n\n"
            f"{length_instruction}\n"
            f"{layout_instruction}\n"
            f"{tone_instruction}\n\n"
            f"متن:\n{cleaned or source_text or 'از روی تصویر یک کپشن فوتبالی بساز'}"
        )
        refine_parts = _prompt_parts(refine_prompt)
        refine_body: dict = {"contents": [{"role": "user", "parts": refine_parts}], "generationConfig": {"temperature": 0.1}}
        if model:
            refine_body["model"] = model

        second = _call_with_budget(refine_body)
        if second:
            candidate = _postprocess_generated_text(
                second,
                destination=destination,
                target_lines=target_lines,
                prefer_multiline=prefer_multiline,
            )
            candidate = _preserve_source_emojis(candidate, destination=destination, source_emojis=source_emojis)
            if candidate:
                cleaned = candidate

    # Hard quality gate: if still non-Persian, force strict Persian rewrite retries.
    strict_sources: list[str] = []
    if cleaned:
        strict_sources.append(cleaned)
    if source_text:
        strict_sources.append(source_text)
    if not strict_sources:
        strict_sources.append("از روی تصویر، فقط یک کپشن فوتبالی فارسی کوتاه و حرفه‌ای تولید کن.")
    for strict_source in strict_sources:
        if _is_persian_acceptable(cleaned, destination=destination):
            break
        attempts = 0
        while attempts < 2 and _remaining_budget() > 4.0:
            attempts += 1
            strict_prompt = (
                "متن زیر را فقط به فارسی روان و طبیعی بازنویسی کن.\n"
                "هیچ توضیح اضافه نده.\n"
                "هیچ خط انگلیسی نیاور.\n"
                "معنی تغییر نکند.\n"
                "حداکثر 4 جمله.\n\n"
                "ایموجی‌های موجود را حذف نکن.\n\n"
                f"{length_instruction}\n"
                f"{layout_instruction}\n"
                f"{tone_instruction}\n\n"
                f"متن:\n{strict_source}"
            )
            strict_parts = _prompt_parts(strict_prompt)
            strict_body: dict = {"contents": [{"role": "user", "parts": strict_parts}], "generationConfig": {"temperature": 0.05}}
            if model:
                strict_body["model"] = model
            strict = _call_with_budget(strict_body)
            if not strict:
                continue
            strict_clean = _postprocess_generated_text(
                strict,
                destination=destination,
                target_lines=target_lines,
                prefer_multiline=prefer_multiline,
            )
            strict_clean = _preserve_source_emojis(strict_clean, destination=destination, source_emojis=source_emojis)
            if strict_clean:
                cleaned = strict_clean
            if _is_persian_acceptable(cleaned, destination=destination):
                break

    adjust_attempts = 0
    while (
        cleaned
        and not _is_length_acceptable(cleaned, source_content=source_content, destination=destination)
        and _remaining_budget() > 4.0
        and adjust_attempts < 2
    ):
        adjust_attempts += 1
        low, high, _ = _source_length_bounds(source_content)
        adjust_prompt = (
            "متن زیر را با همان معنا بازنویسی کن و فقط طول و فرم را تنظیم کن.\n"
            "هیچ توضیح اضافه نده.\n"
            "فقط نسخه نهایی را بده.\n"
            f"طول خروجی باید بین {low} تا {high} کاراکتر باشد.\n"
            f"{layout_instruction}\n"
            f"{tone_instruction}\n\n"
            f"متن:\n{cleaned}"
        )
        adjust_parts = _prompt_parts(adjust_prompt)
        adjust_body = _build_body(adjust_parts, temperature=0.08)
        adjusted = _call_with_budget(adjust_body)
        if not adjusted:
            break
        adjusted_clean = _postprocess_generated_text(
            adjusted,
            destination=destination,
            target_lines=target_lines,
            prefer_multiline=prefer_multiline,
        )
        adjusted_clean = _preserve_source_emojis(adjusted_clean, destination=destination, source_emojis=source_emojis)
        if adjusted_clean:
            cleaned = adjusted_clean

    if not cleaned:
        return ""

    # Never allow non-Persian output to pass through when AI path is active.
    if not _is_persian_acceptable(cleaned, destination=destination):
        return ""

    return cleaned


def _apply_generated_text(base_messages: list[dict], generated_text: str) -> list[dict]:
    if not generated_text:
        return []

    out: list[dict] = []
    first_caption_idx: int | None = None

    for item in base_messages:
        if not isinstance(item, dict):
            continue
        msg_type = str(item.get("type") or "").strip().lower()
        if first_caption_idx is None and msg_type in CAPTION_TYPES:
            first_caption_idx = len(out)
        if msg_type == "text":
            continue
        out.append(dict(item))

    if first_caption_idx is not None:
        out[first_caption_idx]["caption"] = generated_text
        return out

    return [{"type": "text", "text": generated_text}, *out] if out else [{"type": "text", "text": generated_text}]


def build_messages(payload: dict, *, input_dir: Path) -> list[dict]:
    base = _build_base_messages(payload)
    if _is_promotional_payload(payload):
        return []
    # Some channel updates do not carry any usable text/caption/media payload.
    # Treat them as a safe skip instead of failing the sync pipeline.
    if not _has_meaningful_source(payload):
        return []

    has_textual_source = _has_textual_source(payload)

    # For video posts, only send textual context (caption/text) to AI.
    # Video-only updates without text/caption should stay passthrough.
    if _has_video_input(payload) and not has_textual_source:
        if _is_promotional_messages(base):
            return []
        return base

    generated = _generate_football_text(payload=payload, input_dir=input_dir, base_messages=base)
    ai_mandatory = _ai_mandatory_mode()

    destination = _destination_signature(payload)

    if ai_mandatory and (generated is None or not generated):
        # Mandatory AI should not hard-fail media-only updates that have no
        # textual source to rewrite. In that case passthrough media safely.
        if not has_textual_source and _has_any_media(base):
            out = base
        else:
            raise RuntimeError("ai_generation_required_failed")
    else:
        if generated is None:
            out = base
        elif generated:
            out = _apply_generated_text(base, generated)
        else:
            out = base

    # Last safety net: only reuse source caption/text when textual source exists.
    if _has_media_without_caption(out) and not ai_mandatory and has_textual_source:
        fallback_caption = _fallback_source_caption(payload)
        if fallback_caption:
            injected: list[dict] = []
            assigned = False
            for item in out:
                if not isinstance(item, dict):
                    continue
                obj = dict(item)
                msg_type = str(obj.get("type") or "").strip().lower()
                if (not assigned) and msg_type in CAPTION_TYPES and not str(obj.get("caption") or "").strip():
                    obj["caption"] = fallback_caption
                    assigned = True
                injected.append(obj)
            out = injected

    if _is_promotional_messages(out):
        return []

    # AI-enabled mandatory route: reject invalid output instead of injecting fallbacks.
    if ai_mandatory:
        sanitized: list[dict] = []
        for item in out:
            if not isinstance(item, dict):
                continue
            obj = dict(item)
            msg_type = str(item.get("type") or "").strip().lower()
            if msg_type == "text":
                text = str(item.get("text") or "").strip()
                if text and not _is_persian_acceptable(text, destination=destination):
                    raise RuntimeError("ai_output_not_acceptable")
                sanitized.append(obj)
                continue
            if msg_type in CAPTION_TYPES:
                caption = str(obj.get("caption") or "").strip()
                if caption and not _is_persian_acceptable(caption, destination=destination):
                    raise RuntimeError("ai_output_not_acceptable")
                sanitized.append(obj)
                continue
            sanitized.append(obj)
        out = sanitized
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", required=True)
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    try:
        payload = _load_payload(Path(args.payload))
        messages = build_messages(payload, input_dir=Path(args.input_dir))
    except Exception as exc:
        lowered = str(exc).lower()
        if "ai_generation_required_failed" in lowered:
            print("ai_generation_required_failed", file=sys.stderr)
            raise SystemExit(2)
        if "ai_output_not_acceptable" in lowered:
            print("ai_output_not_acceptable", file=sys.stderr)
            raise SystemExit(2)
        print(f"[football.py] non-fatal error: {exc}", file=sys.stderr)
        try:
            payload = _load_payload(Path(args.payload))
            messages = _safe_fail_open_messages(
                _build_base_messages(payload),
                destination=_destination_signature(payload),
            )
        except Exception:
            messages = []
    print(json.dumps({"messages": messages}, ensure_ascii=False))


if __name__ == "__main__":
    main()
