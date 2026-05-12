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
    return os.getenv("FOOTBALL_AI_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}


def _pick_ai_endpoint() -> str:
    return (
        os.getenv("FOOTBALL_AI_ENDPOINT", "").strip()
        or os.getenv("SCRIPT_CLEAN_AI_ENDPOINT", "").strip()
        or os.getenv("FINAL_SCRIPT_AI_ENDPOINT", "").strip()
        or os.getenv("GUARD_AI_ENDPOINT", "").strip()
    )


def _pick_ai_model() -> str:
    return (
        os.getenv("FOOTBALL_AI_MODEL", "").strip()
        or os.getenv("FINAL_SCRIPT_AI_MODEL", "").strip()
        or os.getenv("SCRIPT_CLEAN_AI_MODEL", "").strip()
        or os.getenv("GUARD_AI_MODEL", "").strip()
    )


def _ai_timeout_sec() -> float:
    raw = os.getenv("FOOTBALL_AI_TIMEOUT_SEC", "20").strip() or "20"
    try:
        return max(4.0, min(45.0, float(raw)))
    except Exception:
        return 20.0


def _ai_retry_count() -> int:
    raw = os.getenv("FOOTBALL_AI_RETRY_COUNT", "1").strip() or "1"
    try:
        # Hard cap retries for latency safety in channel-script runtime.
        return max(0, min(1, int(raw)))
    except Exception:
        return 1


def _ai_fail_open() -> bool:
    return os.getenv("FOOTBALL_AI_FAIL_OPEN", "true").strip().lower() in {"1", "true", "yes", "on"}


def _ai_max_images() -> int:
    raw = os.getenv("FOOTBALL_AI_MAX_IMAGES", "3").strip() or "3"
    try:
        return max(0, min(6, int(raw)))
    except Exception:
        return 3


def _ai_total_budget_sec() -> float:
    raw = os.getenv("FOOTBALL_AI_TOTAL_BUDGET_SEC", "75").strip() or "75"
    try:
        return max(15.0, min(110.0, float(raw)))
    except Exception:
        return 75.0


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

    inputs = payload.get("inputs") or []
    if isinstance(inputs, list) and inputs:
        media_lines: list[str] = []
        for idx, item in enumerate(inputs, start=1):
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or "unknown").strip().lower()
            file_name = str(item.get("file_name") or item.get("local_name") or "").strip()
            if file_name:
                media_lines.append(f"{idx}. {kind}: {file_name}")
            else:
                media_lines.append(f"{idx}. {kind}")
        if media_lines:
            parts.append("MEDIA:\n" + "\n".join(media_lines))

    return "\n\n".join(parts).strip()


def _is_image_input(item: dict) -> bool:
    kind = str(item.get("kind") or "").strip().lower()
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


def _football_prompt(source_text: str, destination: str) -> str:
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
        f"امضای مقصد: {destination or '<none>'}\n\n"
        f"ورودی:\n{source_text or '<empty>'}"
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
    return _normalize_text("\n".join(lines))


def _is_latin_heavy_line(line: str) -> bool:
    tokens = re.findall(r"\S+", line)
    if not tokens:
        return False
    latin_tokens = len(LATIN_WORD_RE.findall(line))
    return (latin_tokens / max(1, len(tokens))) >= 0.6


def _strip_prompt_leakage_lines(text: str) -> str:
    kept: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if PROMPT_LEAK_RE.search(line):
            continue
        if _is_latin_heavy_line(line) and not PERSIAN_CHAR_RE.search(line):
            continue
        kept.append(line)
    return _normalize_text("\n".join(kept))


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
    return _normalize_text("\n".join(out))


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
        out = _normalize_text("\n".join(out_lines))
        if had_signature and out:
            out = f"{out}\n{destination}"
        elif had_signature and not out:
            out = destination
    return out


def _postprocess_generated_text(text: str, *, destination: str) -> str:
    out = _strip_md_fence(text)
    out = _sanitize_ai_output(out)
    out = _strip_prompt_leakage_lines(out)
    out = _dedupe_lines(out)
    out = _normalize_signature(out, destination)
    return _normalize_text(out)


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


def _generate_football_text(*, payload: dict, input_dir: Path, base_messages: list[dict]) -> str | None:
    if not _football_ai_enabled():
        return None

    endpoint = _pick_ai_endpoint()
    if not endpoint:
        return None

    source_text = _collect_source_text(payload)
    if not source_text and not base_messages:
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

    destination = _destination_signature(payload)
    prompt = _football_prompt(source_text, destination=destination)

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
    text_only_body = _build_body([{"text": prompt}], temperature=0.2)
    first = _call_with_budget(text_only_body)
    if first is None:
        image_parts = _load_image_parts(payload, input_dir)
        if image_parts and _remaining_budget() > 5.0:
            image_body = _build_body([{"text": prompt}, *image_parts], temperature=0.2)
            first = _call_with_budget(image_body)
    if first is None:
        return "" if not _ai_fail_open() else None

    cleaned = _postprocess_generated_text(first, destination=destination)

    if _looks_low_quality(cleaned) and _remaining_budget() > 6.0:
        refine_prompt = (
            "متن زیر را به یک پست فوتبالی فارسی روان، طبیعی و آماده انتشار بازنویسی کن.\n"
            "هیچ توضیحی اضافه نکن و فقط نسخه نهایی را بده.\n"
            "غلط املایی/نگارشی نداشته باشد.\n"
            "حداکثر 4 جمله.\n\n"
            "هیچ خط انگلیسی یا توضیح فرامتنی نیاور.\n"
            "اگر امضای مقصد وجود دارد، فقط یک‌بار در خط آخر بیاور.\n\n"
            f"متن:\n{cleaned or source_text}"
        )
        refine_body: dict = {
            "contents": [{"role": "user", "parts": [{"text": refine_prompt}]}],
            "generationConfig": {"temperature": 0.1},
        }
        if model:
            refine_body["model"] = model

        second = _call_with_budget(refine_body)
        if second:
            candidate = _postprocess_generated_text(second, destination=destination)
            if candidate:
                cleaned = candidate

    if not cleaned:
        return "" if not _ai_fail_open() else None

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
    generated = _generate_football_text(payload=payload, input_dir=input_dir, base_messages=base)
    if generated is None:
        return base
    return _apply_generated_text(base, generated)


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
        # Never break pipeline output format; fail-open with passthrough.
        print(f"[football.py] non-fatal error: {exc}", file=sys.stderr)
        try:
            payload = _load_payload(Path(args.payload))
            messages = _build_base_messages(payload)
        except Exception:
            messages = []
    print(json.dumps({"messages": messages}, ensure_ascii=False))


if __name__ == "__main__":
    main()
