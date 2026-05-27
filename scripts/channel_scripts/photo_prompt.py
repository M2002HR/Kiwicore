#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
from pathlib import Path

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif")
PROMPT_KEYWORDS = (
    "prompt",
    "پرامپت",
    "متن پرامپت",
    "نسخه زنانه",
    "نسخه مردانه",
    "پرامپت کاپلی",
    "portrait",
    "photorealistic",
    "realistic",
    "cinematic",
    "ratio",
    "aspect ratio",
)
CTA_RE = re.compile(r"برای\s+استفاده\s+مستقیم|کلیک\s+کنید|عکستون\s*رو\s*بفرستید", re.IGNORECASE)
SOURCE_TAG_RE = re.compile(r"^@\w{3,}$")
URL_RE = re.compile(r"(https?://\S+|t\.me/\S+|telegram\.me/\S+)", re.IGNORECASE)
SECTION_START_RE = re.compile(r"(?im)^\s*(نسخه\s*زنانه|female(?:\s*version)?)\s*:\s*$|^\s*(نسخه\s*مردانه|male(?:\s*version)?)\s*:\s*$")
HEADER_ONLY_RE = re.compile(r"(?im)^\s*(متن\s*پرامپت|پرامپت\s*کاپلی|prompt)\s*:\s*$")
PROMPT_CTA_LINE = "ساخت عکس با این پرامپت 👇"


def _load_payload(path: Path) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("payload must be a JSON object")
    return raw


def _normalize_text(text: str) -> str:
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    # keep intentional blank lines (single)
    out: list[str] = []
    prev_blank = False
    for line in lines:
        if not line:
            if out and not prev_blank:
                out.append("")
            prev_blank = True
            continue
        out.append(line)
        prev_blank = False
    while out and out[-1] == "":
        out.pop()
    while out and out[0] == "":
        out.pop(0)
    return "\n".join(out).strip()


def _collect_post_text(payload: dict) -> str:
    message = payload.get("message")
    if not isinstance(message, dict):
        return ""
    text = _normalize_text(str(message.get("text") or ""))
    caption = _normalize_text(str(message.get("caption") or ""))
    if text and caption and text != caption:
        return _normalize_text(f"{text}\n\n{caption}")
    return text or caption


def _is_image_input(item: dict) -> bool:
    kind = str(item.get("kind") or "").strip().lower()
    if kind == "photo":
        return True
    if kind in {"video", "video_note", "voice", "audio", "animation", "sticker"}:
        return False
    mime = str(item.get("mime_type") or "").strip().lower()
    if mime.startswith("image/"):
        return True
    name = str(item.get("local_name") or item.get("file_name") or "").strip().lower()
    return name.endswith(IMAGE_EXTS)


def _resolve_local_name(item: dict) -> str | None:
    local_name = item.get("local_name")
    if isinstance(local_name, str) and local_name.strip():
        return local_name.strip()
    local_path = item.get("local_path")
    if isinstance(local_path, str) and local_path.strip():
        return Path(local_path).name
    return None


def _looks_like_prompt_text(text: str) -> bool:
    normalized = _normalize_text(text)
    if not normalized:
        return False
    lower = normalized.lower()
    if any(token in lower for token in PROMPT_KEYWORDS):
        return True

    # Fallback: dense descriptive prompt-like text.
    tokens = re.findall(r"\S+", normalized)
    comma_count = normalized.count(",") + normalized.count("،") + normalized.count(";") + normalized.count("؛")
    style_hits = sum(
        1
        for token in (
            "lighting",
            "studio",
            "depth of field",
            "bokeh",
            "8k",
            "close-up",
            "headshot",
            "skin texture",
            "editorial",
            "fashion",
            "portrait",
            "ultra-realistic",
        )
        if token in lower
    )
    return len(tokens) >= 12 and comma_count >= 3 and style_hits >= 2


def _clean_prompt_section(text: str) -> str:
    normalized = _normalize_text(text)
    if not normalized:
        return ""
    lines = [ln.strip() for ln in normalized.splitlines()]
    out: list[str] = []
    for line in lines:
        if not line:
            if out and out[-1] != "":
                out.append("")
            continue
        if SOURCE_TAG_RE.match(line):
            continue
        if CTA_RE.search(line):
            continue
        cleaned = URL_RE.sub(" ", line)
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if cleaned:
            out.append(cleaned)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out).strip()


def _extract_gender_prompts(text: str) -> dict[str, str]:
    normalized = _normalize_text(text)
    if not normalized:
        return {}

    matches = list(SECTION_START_RE.finditer(normalized))
    if not matches:
        return {}

    out: dict[str, str] = {}
    for i, match in enumerate(matches):
        start = match.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(normalized)
        section = _clean_prompt_section(normalized[start:end])
        if not section:
            continue
        marker = (match.group(1) or match.group(2) or "").lower()
        if "زن" in marker or "female" in marker:
            out["female"] = section
        elif "مرد" in marker or "male" in marker:
            out["male"] = section
    return out


def _extract_single_prompt(text: str) -> str:
    normalized = _normalize_text(text)
    if not normalized:
        return ""
    cleaned = _clean_prompt_section(normalized)
    if not cleaned:
        return ""
    cleaned = HEADER_ONLY_RE.sub("", cleaned).strip()
    if not cleaned:
        return ""
    return cleaned if _looks_like_prompt_text(cleaned) else ""


def _clean_caption_text(text: str) -> str:
    normalized = _normalize_text(text)
    if not normalized:
        return ""
    lines = normalized.splitlines()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if out and out[-1] != "":
                out.append("")
            continue
        if SOURCE_TAG_RE.match(stripped):
            continue
        if CTA_RE.search(stripped):
            continue
        if URL_RE.search(stripped) and len(stripped.split()) <= 8:
            continue
        out.append(stripped)
    while out and out[-1] == "":
        out.pop()
    return "\n".join(out).strip()


def _token_store_path() -> Path:
    raw = os.getenv("PHOTO_PROMPT_TOKEN_STORE_PATH", "../Pirashki_AI/data/prompt_tokens.json").strip()
    return Path(raw)


def _build_token(variant: str, prompt_text: str, source_ref: str) -> str:
    h = hashlib.sha256(f"{variant}\n{source_ref}\n{prompt_text}".encode("utf-8")).hexdigest()
    return f"pp_{h[:24]}"


def _store_prompt_token(token: str, payload: dict) -> None:
    path = _token_store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    current: dict[str, object] = {}
    if path.exists():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                current = raw
        except Exception:
            current = {}
    current[token] = payload
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _build_deep_link(token: str) -> str:
    base = os.getenv("PHOTO_PROMPT_BOT_DEEPLINK_BASE", "https://ble.ir/pirashki_bot?start=").strip()
    if not base:
        base = "https://ble.ir/pirashki_bot?start="
    if "{payload}" in base:
        return base.replace("{payload}", token)
    if base.endswith("=") or base.endswith("/"):
        return f"{base}{token}"
    if "?" in base:
        joiner = "&" if not base.endswith("&") else ""
        return f"{base}{joiner}start={token}"
    return f"{base}?start={token}"


def _build_reply_markup(payload: dict, post_text: str) -> dict | None:
    gender_prompts = _extract_gender_prompts(post_text)
    route = payload.get("route") if isinstance(payload.get("route"), dict) else {}
    source_ref = str((route or {}).get("source_channel_username") or "@AiFreeRoPrompt")
    post_hint = str((payload.get("message") or {}).get("message_id") or "")

    buttons: list[dict[str, str]] = []
    now = int(time.time())
    for variant, label in (("female", "نسخه زنانه"), ("male", "نسخه مردانه")):
        prompt_text = gender_prompts.get(variant)
        if not prompt_text:
            continue
        token = _build_token(variant, prompt_text, f"{source_ref}:{post_hint}")
        _store_prompt_token(
            token,
            {
                "variant": variant,
                "title": label,
                "prompt_text": prompt_text,
                "source": source_ref,
                "post_hint": post_hint,
                "updated_at": now,
            },
        )
        buttons.append({"text": label, "url": _build_deep_link(token)})

    if not buttons:
        single_prompt = _extract_single_prompt(post_text)
        if single_prompt:
            token = _build_token("single", single_prompt, f"{source_ref}:{post_hint}")
            _store_prompt_token(
                token,
                {
                    "variant": "single",
                    "title": "پرامپت",
                    "prompt_text": single_prompt,
                    "source": source_ref,
                    "post_hint": post_hint,
                    "updated_at": now,
                },
            )
            buttons.append({"text": "👁 استفاده از پرامپت", "url": _build_deep_link(token)})

    if len(buttons) >= 2:
        return {"inline_keyboard": [buttons[:2]]}
    if len(buttons) == 1:
        return {"inline_keyboard": [[buttons[0]]]}
    return None


def build_messages(payload: dict, *, input_dir: Path) -> list[dict]:
    del input_dir

    post_text = _collect_post_text(payload)
    if not _looks_like_prompt_text(post_text):
        return []
    caption_text = _clean_caption_text(post_text) or post_text

    inputs = payload.get("inputs")
    if not isinstance(inputs, list):
        return []

    out: list[dict] = []
    caption_assigned = False
    reply_markup = _build_reply_markup(payload, post_text)

    for item in inputs:
        if not isinstance(item, dict):
            continue
        if not _is_image_input(item):
            continue
        local_name = _resolve_local_name(item)
        if not local_name:
            continue
        msg: dict[str, object] = {"type": "photo", "path": local_name}
        if not caption_assigned and caption_text:
            if reply_markup is not None:
                caption_with_cta = caption_text
                if PROMPT_CTA_LINE not in caption_with_cta:
                    caption_with_cta = f"{caption_with_cta}\n\n{PROMPT_CTA_LINE}"
                msg["caption"] = caption_with_cta
                msg["reply_markup"] = reply_markup
                msg["append_destination_footer"] = False
            else:
                msg["caption"] = caption_text
            caption_assigned = True
        out.append(msg)

    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", required=True)
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    payload = _load_payload(Path(args.payload))
    messages = build_messages(payload, input_dir=Path(args.input_dir))
    print(json.dumps({"messages": messages}, ensure_ascii=False))


if __name__ == "__main__":
    main()
