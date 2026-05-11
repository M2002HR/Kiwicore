#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path


PERSIAN_LETTER_RE = re.compile(r"[\u0600-\u06FF]")
LATIN_WORD_RE = re.compile(r"\b[A-Za-z]{2,}\b")
MENTION_RE = re.compile(r"(?<!\w)@[A-Za-z0-9_]{3,}(?!\w)")
NAME_SEQ_RE = re.compile(r"\b([A-Z][A-Za-z'-]{1,}(?:\s+[A-Z][A-Za-z'-]{1,}){0,2}(?:\s+Jr\.?)?)\b")

ENTITY_FA_MAP: dict[str, str] = {
    "bruno fernandes": "برونو فرناندس",
    "marcus rashford": "مارکوس رشفورد",
    "jude bellingham": "جود بلینگهم",
    "eric garcia": "اریک گارسیا",
    "laliga": "لالیگا",
    "barcelona": "بارسلونا",
    "real madrid": "رئال مادرید",
}


def _load_payload(path: Path) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("payload must be a JSON object")
    return raw


def _normalize_space(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    lines = [line.strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def _basic_persian_fixes(text: str) -> str:
    # Keep this conservative: only normalize obvious spacing/punctuation issues.
    out = text
    out = out.replace("ي", "ی").replace("ك", "ک")
    out = re.sub(r"\s+([،؛:!؟.,])", r"\1", out)
    out = re.sub(r"([،؛:!؟.,])(\S)", r"\1 \2", out)
    out = re.sub(r"\s+", " ", out)
    return out.strip()


def _ai_polish_text(text: str) -> str:
    enabled = os.getenv("FINAL_SCRIPT_AI_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return text

    endpoint = os.getenv("FINAL_SCRIPT_AI_ENDPOINT", "").strip()
    if not endpoint:
        endpoint = os.getenv("SCRIPT_CLEAN_AI_ENDPOINT", "").strip() or os.getenv("GUARD_AI_ENDPOINT", "").strip()
    if not endpoint:
        return text

    model = os.getenv("FINAL_SCRIPT_AI_MODEL", "").strip()
    if not model:
        model = os.getenv("SCRIPT_CLEAN_AI_MODEL", "").strip() or os.getenv("GUARD_AI_MODEL", "").strip()

    timeout_sec = float(os.getenv("FINAL_SCRIPT_AI_TIMEOUT_SEC", "20").strip() or "20")
    fail_open = os.getenv("FINAL_SCRIPT_AI_FAIL_OPEN", "true").strip().lower() in {"1", "true", "yes", "on"}

    prompt = (
        "تو یک ویراستار حرفه‌ای فارسی برای پست‌های تلگرام هستی.\n"
        "متن زیر را فقط ویرایش نگارشی و خواناسازی کن.\n\n"
        "قوانین مهم:\n"
        "1. معنی متن را تغییر نده و هیچ اطلاعاتی اضافه/حذف نکن.\n"
        "2. ترجمه نکن؛ فقط همان محتوای موجود را تمیز و روان کن.\n"
        "3. غلط املایی، نشانه‌گذاری و فاصله‌گذاری را اصلاح کن.\n"
        "4. خروجی باید یک پست فارسی روان و قابل‌فهم باشد.\n"
        "5. فقط متن نهایی را بده و هیچ توضیحی اضافه نکن.\n\n"
        f"متن:\n{text}"
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
    if _looks_meta_or_prompt_like(cleaned):
        return text if fail_open else ""
    cleaned = _normalize_space(cleaned)
    if LATIN_WORD_RE.search(cleaned):
        strict_prompt = (
            "متن زیر را به یک پست کاملا فارسی، روان و آماده انتشار بازنویسی کن.\n"
            "هیچ واژه انگلیسی نیاور، مگر لینک یا آیدی کانال.\n"
            "معنی متن را تغییر نده.\n"
            "فقط متن نهایی را برگردان.\n\n"
            f"متن:\n{text}"
        )
        strict_body: dict = {
            "contents": [{"role": "user", "parts": [{"text": strict_prompt}]}],
            "generationConfig": {"temperature": 0},
        }
        if model:
            strict_body["model"] = model
        strict = _call_gemini_text(endpoint=endpoint, body=strict_body, timeout_sec=timeout_sec)
        if strict is not None:
            strict_clean = _normalize_space(_normalize_gemini_text(strict))
            if strict_clean and not _looks_meta_or_prompt_like(strict_clean):
                cleaned = strict_clean
    if not cleaned:
        return text if fail_open else ""
    return cleaned


def _call_gemini_text(*, endpoint: str, body: dict, timeout_sec: float) -> str | None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    retries = 2
    for attempt in range(retries + 1):
        try:
            with opener.open(req, timeout=timeout_sec) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except Exception:
            if attempt >= retries:
                return None
            time.sleep(0.35 * (attempt + 1))
            continue

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            if attempt >= retries:
                return None
            time.sleep(0.2 * (attempt + 1))
            continue
        if not isinstance(data, dict):
            if attempt >= retries:
                return None
            time.sleep(0.2 * (attempt + 1))
            continue

        candidates = data.get("candidates")
        if not isinstance(candidates, list):
            if attempt >= retries:
                return None
            time.sleep(0.2 * (attempt + 1))
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


def _looks_meta_or_prompt_like(text: str) -> bool:
    lowered = text.strip().lower()
    if not lowered:
        return True
    bad_fragments = (
        "you are ",
        "role:",
        "input text:",
        "translated text:",
        "return only",
        "here is",
        "متن زیر",
        "اصلاح‌شده",
        "نسخه ویرایش‌شده",
    )
    return any(fragment in lowered for fragment in bad_fragments)


def _lookup_entity_fa(token: str) -> str | None:
    key = re.sub(r"\s+", " ", token.strip().lower().replace("_", " "))
    return ENTITY_FA_MAP.get(key)


def _roman_token_to_persian(token: str) -> str:
    direct = _lookup_entity_fa(token)
    if direct:
        return direct
    t = token.strip().lower()
    if not t:
        return token
    pairs = [
        ("sh", "ش"),
        ("ch", "چ"),
        ("kh", "خ"),
        ("gh", "غ"),
        ("zh", "ژ"),
        ("ph", "ف"),
        ("th", "ت"),
        ("oo", "و"),
        ("ee", "ی"),
        ("ou", "و"),
        ("ai", "ای"),
    ]
    for src, dst in pairs:
        t = t.replace(src, dst)
    cmap = {
        "a": "ا",
        "b": "ب",
        "c": "ک",
        "d": "د",
        "e": "",
        "f": "ف",
        "g": "گ",
        "h": "ه",
        "i": "ی",
        "j": "ج",
        "k": "ک",
        "l": "ل",
        "m": "م",
        "n": "ن",
        "o": "و",
        "p": "پ",
        "q": "ک",
        "r": "ر",
        "s": "س",
        "t": "ت",
        "u": "و",
        "v": "و",
        "w": "و",
        "x": "کس",
        "y": "ی",
        "z": "ز",
        ".": "",
        "-": "-",
    }
    out = "".join(cmap.get(ch, ch) for ch in t)
    out = re.sub(r"\s+", " ", out).strip()
    return out or token


def _to_persian_name(name: str) -> str:
    direct = _lookup_entity_fa(name)
    if direct:
        return direct
    parts = [p for p in re.split(r"\s+", name.strip()) if p]
    if not parts:
        return name
    return " ".join(_roman_token_to_persian(p) for p in parts)


def _localize_latin_names(text: str) -> str:
    protected: dict[str, str] = {}
    out = text
    for idx, mention in enumerate(MENTION_RE.findall(text)):
        ph = f"__M_{idx}__"
        protected[ph] = mention
        out = out.replace(mention, ph)

    def _name_replace(match: re.Match[str]) -> str:
        candidate = match.group(1)
        return _to_persian_name(candidate)

    out = NAME_SEQ_RE.sub(_name_replace, out)
    out = re.sub(r"\bLALIGA\b", "لالیگا", out, flags=re.IGNORECASE)
    for ph, mention in protected.items():
        out = out.replace(ph, mention)
    return out


def _polish(text: str) -> str:
    normalized = _normalize_space(text)
    if not normalized:
        return ""

    # Basic grammar cleanup first, then optional AI pass.
    cleaned = _localize_latin_names(_basic_persian_fixes(normalized))
    if PERSIAN_LETTER_RE.search(cleaned):
        cleaned = _ai_polish_text(cleaned)
    cleaned = _localize_latin_names(cleaned)
    cleaned = _normalize_space(cleaned)
    return _normalize_space(cleaned)


def _process_messages(payload: dict) -> list[dict]:
    raw_messages = payload.get("messages")
    if not isinstance(raw_messages, list):
        return []

    out: list[dict] = []
    for item in raw_messages:
        if not isinstance(item, dict):
            continue
        updated = dict(item)

        text = updated.get("text")
        if isinstance(text, str) and text.strip():
            updated["text"] = _polish(text)

        caption = updated.get("caption")
        if isinstance(caption, str) and caption.strip():
            updated["caption"] = _polish(caption)

        out.append(updated)

    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", required=True)
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    payload = _load_payload(Path(args.payload))
    messages = _process_messages(payload)
    print(json.dumps({"messages": messages}, ensure_ascii=False))


if __name__ == "__main__":
    main()
