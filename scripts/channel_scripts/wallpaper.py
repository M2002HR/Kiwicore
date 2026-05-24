#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

def _load_payload(path: Path) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("payload must be a JSON object")
    return raw


def _normalize_text(value: str) -> str:
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in str(value).splitlines()]
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


def _is_image_input(item: dict) -> bool:
    kind = str(item.get("kind") or "").strip().lower()
    return kind == "photo"


def _extract_destination_username(payload: dict) -> str | None:
    route = payload.get("route")
    if not isinstance(route, dict):
        route = {}
    candidates = (
        route.get("destination_channel_username"),
        route.get("destination_target"),
    )
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        raw = candidate.strip()
        if not raw:
            continue
        if raw.startswith("@"):
            username = raw[1:].strip()
            if username:
                return username
        lowered = raw.lower()
        if lowered.startswith("https://t.me/") or lowered.startswith("http://t.me/"):
            username = raw.split("/")[-1].strip().lstrip("@")
            if username:
                return username
        if re.fullmatch(r"[A-Za-z0-9_]{3,}", raw):
            return raw
    return None


def _destination_target(payload: dict) -> str | None:
    route = payload.get("route")
    if not isinstance(route, dict):
        return None
    for key in ("destination_target", "destination_channel_username", "destination_channel_id"):
        value = route.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _build_destination_caption(payload: dict) -> str:
    base = "به چنل سرزمین والپیپر بپیوندید..."
    hashtags = _extract_hashtags(payload)
    if not hashtags:
        return base
    return f"{' '.join(hashtags)}\n{base}"


def _extract_hashtags(payload: dict) -> list[str]:
    message = payload.get("message")
    if not isinstance(message, dict):
        return []

    texts = [
        str(message.get("caption") or ""),
        str(message.get("text") or ""),
    ]
    if not any(part.strip() for part in texts):
        return []

    out: list[str] = []
    seen: set[str] = set()
    for part in texts:
        for match in re.findall(r"(?<!\S)#[^\s#]+", part):
            token = match.strip()
            token = token.rstrip(".,!?:;،؛؟)]}»\"'")
            if len(token) <= 1 or not token.startswith("#"):
                continue
            key = token.casefold()
            if key in seen:
                continue
            seen.add(key)
            out.append(token)
    return out


def build_messages(payload: dict, *, input_dir: Path) -> list[dict]:
    del input_dir  # Path is part of script contract; not needed in this script.

    inputs = payload.get("inputs")
    if not isinstance(inputs, list):
        inputs = []

    images: list[dict] = []
    for item in inputs:
        if not isinstance(item, dict):
            continue
        if not _is_image_input(item):
            continue
        local_name = _resolve_local_name(item)
        if not local_name:
            continue
        images.append({"type": "photo", "path": local_name})

    if not images:
        return []

    caption = _build_destination_caption(payload)
    images[0]["caption"] = caption
    images[0]["append_destination_footer"] = False
    return images


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
