#!/usr/bin/env python3
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
from pathlib import Path

IMAGE_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif")
PROMPT_MARKERS = (
    "prompt",
    "پرامپت",
    "متن پرامپت",
    "نسخه زنانه",
    "نسخه مردانه",
    "پرامپت کاپلی",
    "portrait",
    "photorealistic",
    "cinematic",
    "aspect ratio",
    "ratio",
)
_STYLE_HINTS = (
    "depth of field",
    "bokeh",
    "studio lighting",
    "skin texture",
    "editorial",
    "fashion",
    "close-up",
    "headshot",
    "8k",
)
_DEFAULT_GUARD_MODULE = None


def _load_default_guard_module():
    global _DEFAULT_GUARD_MODULE
    if _DEFAULT_GUARD_MODULE is not None:
        return _DEFAULT_GUARD_MODULE

    path = Path(__file__).with_name("default_guard.py")
    spec = importlib.util.spec_from_file_location("default_guard_for_photo_prompt", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("failed_to_load_default_guard")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _DEFAULT_GUARD_MODULE = module
    return module


def _load_payload(path: Path) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("payload must be a JSON object")
    return raw


def _normalize_text(value: str) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    lines = [line for line in lines if line]
    return "\n".join(lines).strip()


def _collect_text(payload: dict) -> str:
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


def _has_image(payload: dict) -> bool:
    inputs = payload.get("inputs")
    if not isinstance(inputs, list):
        return False
    return any(isinstance(item, dict) and _is_image_input(item) for item in inputs)


def _looks_like_prompt_text(text: str) -> bool:
    normalized = _normalize_text(text)
    if not normalized:
        return False

    lower = normalized.lower()
    if any(token in lower for token in PROMPT_MARKERS):
        return True

    tokens = re.findall(r"\S+", normalized)
    comma_count = normalized.count(",") + normalized.count("،") + normalized.count(";") + normalized.count("؛")
    style_hits = sum(1 for hint in _STYLE_HINTS if hint in lower)
    return len(tokens) >= 12 and comma_count >= 3 and style_hits >= 2


def _looks_like_prompt_payload(payload: dict) -> bool:
    if not _has_image(payload):
        return False
    return _looks_like_prompt_text(_collect_text(payload))


def _build_prompt_relevance_request(payload: dict, input_dir: Path, model: str | None) -> dict:
    default_guard = _load_default_guard_module()

    text = _collect_text(payload)
    image_parts = default_guard._load_image_parts(payload, input_dir)

    user_text = (
        "Task: Classify whether this Telegram post is an IMAGE-GENERATION PROMPT POST.\n"
        "Return 1 (allow) ONLY if ALL are true:\n"
        "1) The post is about generating/editing an image (prompt/instruction template).\n"
        "2) It includes prompt-like text (English or Persian).\n"
        "3) Attached image(s) are sample/result/reference related to that prompt.\n\n"
        "Return 0 (block) for announcements, bot updates, ads, random photos, or non-prompt content.\n"
        "Output strictly one character: 1 or 0.\n\n"
        f"Post text/caption:\n{text or '<empty>'}\n"
    )

    parts: list[dict] = [{"text": user_text}, *image_parts]
    body: dict = {
        "contents": [{"role": "user", "parts": parts}],
        "generationConfig": {"temperature": 0},
    }
    if model:
        body["model"] = model
    return body


def _is_ai_technical_failure_reason(reason: str) -> bool:
    token = str(reason or "").strip().lower()
    if not token:
        return False
    prefixes = (
        "ai_api_exception:",
        "ai_api_unreachable",
        "ai_api_invalid_json",
        "ai_api_invalid_payload",
        "ai_guard_unknown",
    )
    return any(token.startswith(prefix) for prefix in prefixes)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", required=True)
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    default_guard = _load_default_guard_module()

    endpoint = os.getenv("GUARD_AI_ENDPOINT", "http://127.0.0.1:8000/proxy/gemini").strip()
    model_raw = os.getenv("GUARD_AI_MODEL", "").strip()
    model = model_raw or None
    timeout_sec = float(os.getenv("GUARD_AI_TIMEOUT_SEC", "30").strip() or "30")
    enabled = os.getenv("GUARD_AI_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    fail_open = os.getenv("GUARD_AI_FAIL_OPEN", "true").strip().lower() in {"1", "true", "yes", "on"}
    block_ads = os.getenv("GUARD_BLOCK_ADS", "true").strip().lower() in {"1", "true", "yes", "on"}

    prompt_guard_enabled = os.getenv("GUARD_PHOTO_PROMPT_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    prompt_ai_enabled = os.getenv("GUARD_PHOTO_PROMPT_AI_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    prompt_ai_fail_open = os.getenv("GUARD_PHOTO_PROMPT_AI_FAIL_OPEN", "false").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }
    prompt_ai_tech_fail_soft_allow = (
        os.getenv("GUARD_PHOTO_PROMPT_AI_TECH_FAIL_SOFT_ALLOW", "false").strip().lower() in {"1", "true", "yes", "on"}
    )

    if not enabled:
        print("true")
        return

    payload = _load_payload(Path(args.payload))

    if prompt_guard_enabled:
        if not _has_image(payload):
            print("false: missing_image")
            return

        source_text = _collect_text(payload)
        if not source_text:
            print("false: missing_prompt_text")
            return

        if not _looks_like_prompt_text(source_text):
            print("false: prompt_pattern_not_detected")
            return

    if block_ads and default_guard._is_obvious_advertisement(payload):
        print("false: obvious_advertisement")
        return

    vpn_reason = default_guard._vpn_match_reason(payload)
    if vpn_reason:
        print(f"false: obvious_vpn_config:{vpn_reason}")
        return

    if prompt_guard_enabled and prompt_ai_enabled:
        relevance_body = _build_prompt_relevance_request(payload, Path(args.input_dir), model=model)
        relevant, relevance_reason = default_guard._call_guard_api(
            endpoint=endpoint,
            body=relevance_body,
            timeout_sec=timeout_sec,
            fail_open=prompt_ai_fail_open,
        )
        if not relevant:
            if prompt_ai_tech_fail_soft_allow and _is_ai_technical_failure_reason(relevance_reason) and _looks_like_prompt_payload(payload):
                pass
            else:
                print(f"false: prompt_relevance:{relevance_reason}")
                return

    # Run default safety moderation after prompt relevance gate.
    safety_body = default_guard._build_request(payload, Path(args.input_dir), model=model)
    allow, reason = default_guard._call_guard_api(endpoint=endpoint, body=safety_body, timeout_sec=timeout_sec, fail_open=fail_open)
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
