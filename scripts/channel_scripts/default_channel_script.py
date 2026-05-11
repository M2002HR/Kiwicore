#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

CAPTION_TYPES = {"photo", "video", "voice", "audio", "document", "animation"}
PASSTHROUGH_TYPES = {
    "photo",
    "video",
    "voice",
    "audio",
    "document",
    "animation",
    "video_note",
}

URL_RE = re.compile(r"(https?://\S+|www\.\S+|t\.me/\S+|telegram\.me/\S+)", re.IGNORECASE)
MENTION_RE = re.compile(r"(?<!\w)@[A-Za-z0-9_]{3,}(?!\w)")
HASHTAG_RE = re.compile(r"(?<!\w)#[\w_]+")
LINK_WORD_RE = re.compile(r"\b(link|url)\b|لینک", re.IGNORECASE)
REFERENCE_WORD_RE = re.compile(
    r"(شبکه|کانال|پیج|صفحه|source|منبع|channel|account|اکانت|follow|join|subscribe|sponsor|اسپانسر|via|from)",
    re.IGNORECASE,
)
AI_META_LINE_RE = re.compile(
    r"^(goal:|constraint:|edge case:|edge-case:|input:|output:|core content:|removed:|does it|is there|are hashtags|`|[-*]\s|→|=>)",
    re.IGNORECASE,
)

# Emojis/symbols that should be removed from outgoing content.
EMOJI_BLOCKLIST = (
    "🇮🇱",  # Israel flag
    "🏳️‍⚧️",  # trans flag
    "⚧️",
    "⚧",
    "🏳️‍🌈",  # rainbow flag
    "🌈",
    "👰",
    "👰‍♀️",
    "👰‍♂️",
    "🤵",
    "🤵‍♀️",
    "🤵‍♂️",
)


def _load_payload(path: Path) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("payload must be a JSON object")
    return raw


def _resolve_local_name(item: dict) -> str | None:
    local_name = item.get("local_name")
    if isinstance(local_name, str) and local_name.strip():
        return local_name.strip()

    local_path = item.get("local_path")
    if isinstance(local_path, str) and local_path.strip():
        return Path(local_path).name

    return None


def build_passthrough_messages(payload: dict) -> list[dict]:
    message = payload.get("message") or {}
    if not isinstance(message, dict):
        message = {}

    text = message.get("text")
    caption = message.get("caption")
    text = text.strip() if isinstance(text, str) and text.strip() else None
    caption = caption.strip() if isinstance(caption, str) and caption.strip() else None
    text = _sanitize_text(text, payload=payload)
    caption = _sanitize_text(caption, payload=payload)

    inputs = payload.get("inputs") or []
    if not isinstance(inputs, list):
        inputs = []

    out: list[dict] = []

    # Pure text message.
    if not inputs and text:
        out.append({"type": "text", "text": text})
        return out

    caption_assigned = False
    for item in inputs:
        if not isinstance(item, dict):
            continue

        raw_kind = str(item.get("kind") or "").strip().lower()
        if raw_kind == "sticker":
            # Global policy: stickers are blocked and must never be sent.
            continue
        msg_type = raw_kind if raw_kind in PASSTHROUGH_TYPES else "document"
        if _looks_like_gif_document(item, raw_kind=raw_kind):
            msg_type = "animation"

        local_name = _resolve_local_name(item)
        if not local_name:
            continue

        obj: dict[str, object] = {
            "type": msg_type,
            "path": local_name,
        }

        if not caption_assigned and msg_type in CAPTION_TYPES:
            preferred_caption = caption or text
            if preferred_caption:
                obj["caption"] = preferred_caption
                caption_assigned = True

        out.append(obj)

    # If there was message text that is not represented as caption/media, forward it too.
    if text and (not out or (text != caption and not caption_assigned)):
        out.insert(0, {"type": "text", "text": text})
    elif caption and not out:
        # Fallback: message only had caption but media inputs were empty/unusable.
        out.append({"type": "text", "text": caption})

    return out


def _looks_like_gif_document(item: dict, *, raw_kind: str) -> bool:
    if raw_kind == "animation":
        return True
    if raw_kind != "document":
        return False

    mime_type = str(item.get("mime_type") or "").strip().lower()
    file_name = str(item.get("file_name") or "").strip().lower()

    if mime_type == "image/gif":
        return True
    if mime_type == "video/mp4" and (".gif." in file_name or file_name.endswith(".gif") or file_name.endswith(".gif.mp4")):
        return True
    return False


def _sanitize_text(value: str | None, *, payload: dict) -> str | None:
    if not value:
        return None

    text = value
    for token in EMOJI_BLOCKLIST:
        text = text.replace(token, "")

    had_reference = _has_reference(text)
    if had_reference:
        text = _ai_cleanup_reference_text(text, payload=payload)

    text = _strip_reference_tokens(text)
    text = _remove_reference_lines(text)
    text, removed_source_footer = _remove_source_footer_signature(text, payload=payload)
    text = _normalize_multiline(text)

    # If channel reference/footer existed, replace with destination target.
    destination = _destination_signature(payload)
    if (had_reference or removed_source_footer) and destination:
        if text:
            text = f"{text}\n{destination}"
        else:
            text = destination

    text = _normalize_multiline(text)
    return text or None


def _destination_signature(payload: dict) -> str | None:
    route = payload.get("route") or {}
    if not isinstance(route, dict):
        return None
    for key in ("destination_channel_id", "destination_target", "destination_channel_username"):
        value = route.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _has_reference(text: str) -> bool:
    return bool(URL_RE.search(text) or HASHTAG_RE.search(text) or LINK_WORD_RE.search(text))


def _strip_reference_tokens(text: str) -> str:
    text = URL_RE.sub(" ", text)
    text = HASHTAG_RE.sub(" ", text)
    text = LINK_WORD_RE.sub(" ", text)
    return text


def _remove_reference_lines(text: str) -> str:
    lines = _normalize_multiline(text).splitlines()
    kept: list[str] = []
    for line in lines:
        if not line:
            continue
        line_l = line.lower()
        if REFERENCE_WORD_RE.search(line_l):
            continue
        kept.append(line)
    return "\n".join(kept)


def _remove_source_footer_signature(text: str, *, payload: dict) -> tuple[str, bool]:
    lines = _normalize_multiline(text).splitlines()
    if not lines:
        return text, False

    markers = _source_footer_markers(payload)
    if not markers:
        return text, False

    removed = False
    while lines:
        tail = lines[-1].strip().lower()
        if tail in markers or tail in {"|", "｜"}:
            lines.pop()
            removed = True
            continue
        break

    return "\n".join(lines), removed


def _source_footer_markers(payload: dict) -> set[str]:
    markers: set[str] = set()
    route = payload.get("route") or {}
    message = payload.get("message") or {}
    if not isinstance(route, dict):
        route = {}
    if not isinstance(message, dict):
        message = {}

    for key in ("source_channel_username", "source_channel_id"):
        v = route.get(key)
        if isinstance(v, str) and v.strip():
            markers.add(v.strip().lower())
        v2 = message.get(key)
        if isinstance(v2, str) and v2.strip():
            markers.add(v2.strip().lower())

    return markers


def _normalize_multiline(text: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    return "\n".join(lines).strip()


def _ai_cleanup_reference_text(text: str, *, payload: dict) -> str:
    enabled = os.getenv("SCRIPT_CLEAN_AI_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return text

    endpoint = os.getenv("SCRIPT_CLEAN_AI_ENDPOINT", "").strip() or os.getenv("GUARD_AI_ENDPOINT", "").strip()
    model = os.getenv("SCRIPT_CLEAN_AI_MODEL", "").strip() or os.getenv("GUARD_AI_MODEL", "").strip()
    timeout_sec = float(os.getenv("SCRIPT_CLEAN_AI_TIMEOUT_SEC", "20").strip() or "20")
    fail_open = os.getenv("SCRIPT_CLEAN_AI_FAIL_OPEN", "true").strip().lower() in {"1", "true", "yes", "on"}
    if not endpoint:
        return text

    destination = _destination_signature(payload) or ""
    prompt = (
        "You are cleaning Persian/English channel posts.\n"
        "Remove any segment that references external channels/accounts/platforms or links.\n"
        "Delete BOTH the URL and its related reference phrase (for example: 'شبکه X', 'کانال ...', hashtags, 'منبع').\n"
        "Keep plain @handles if they are part of core informational lines.\n"
        "Keep only the core informational content.\n"
        "Do not add explanations.\n"
        "If nothing meaningful remains, return exactly: EMPTY\n"
        f"Destination signature (do not include it in answer): {destination}\n\n"
        f"Input text:\n{text}"
    )
    body: dict = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0},
    }
    if model:
        body["model"] = model

    cleaned = _call_gemini_text(endpoint=endpoint, body=body, timeout_sec=timeout_sec)
    if cleaned is None:
        return text if fail_open else ""
    cleaned = _normalize_gemini_text(cleaned)
    cleaned = _sanitize_ai_output(cleaned, original=text)
    if cleaned.upper() == "EMPTY":
        return ""
    return cleaned or ""


def _call_gemini_text(*, endpoint: str, body: dict, timeout_sec: float) -> str | None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    try:
        with opener.open(req, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception:
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    return _extract_text_from_response(data)


def _extract_text_from_response(data: dict) -> str | None:
    candidates = data.get("candidates")
    if not isinstance(candidates, list):
        return None
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
    return None


def _normalize_gemini_text(text: str) -> str:
    out = text.strip()
    if out.startswith("```"):
        out = re.sub(r"^```[a-zA-Z]*\n?", "", out)
        out = re.sub(r"\n?```$", "", out)
    return out.strip()


def _sanitize_ai_output(text: str, *, original: str) -> str:
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if AI_META_LINE_RE.search(line):
            continue
        lines.append(line)

    cleaned = "\n".join(lines).strip()
    if not cleaned:
        return ""
    if _looks_unrelated_to_original(cleaned, original):
        return ""
    return cleaned


def _looks_unrelated_to_original(cleaned: str, original: str) -> bool:
    cleaned_tokens = _tokenize_for_overlap(cleaned)
    original_tokens = _tokenize_for_overlap(original)
    if not cleaned_tokens or not original_tokens:
        return False
    overlap = len(cleaned_tokens & original_tokens) / max(1, len(cleaned_tokens))
    return overlap < 0.35


def _tokenize_for_overlap(text: str) -> set[str]:
    parts = re.findall(r"[A-Za-z0-9_آ-ی]+", text.lower())
    return {part for part in parts if len(part) >= 2}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", required=True)
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    payload = _load_payload(Path(args.payload))
    messages = build_passthrough_messages(payload)

    print(json.dumps({"messages": messages}, ensure_ascii=False))


if __name__ == "__main__":
    main()
