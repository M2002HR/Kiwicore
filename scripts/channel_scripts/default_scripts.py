#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
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

        local_name = _resolve_local_name(item)
        if not local_name:
            continue

        obj: dict[str, object] = {
            "type": msg_type,
            "path": local_name,
        }

        if not caption_assigned and caption and msg_type in CAPTION_TYPES:
            obj["caption"] = caption
            caption_assigned = True

        out.append(obj)

    # If there was message text that is not represented as caption/media, forward it too.
    if text and (not out or text != caption):
        out.insert(0, {"type": "text", "text": text})
    elif caption and not out:
        # Fallback: message only had caption but media inputs were empty/unusable.
        out.append({"type": "text", "text": caption})

    return out


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
