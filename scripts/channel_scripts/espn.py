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

try:
    from default_channel_script import build_passthrough_messages as _default_build_passthrough_messages
except Exception:
    _default_build_passthrough_messages = None

MENTION_RE = re.compile(r"(?<!\w)@[A-Za-z0-9_]{3,}(?!\w)")
PLACEHOLDER_RE = re.compile(r"__MENTION_(\d+)__")
PERSIAN_CHAR_RE = re.compile(r"[آ-ی]")
LATIN_CHAR_RE = re.compile(r"[A-Za-z]")
NAME_SEQ_RE = re.compile(r"\b([A-Z][A-Za-z'-]{1,}(?:\s+[A-Z][A-Za-z'-]{1,}){0,2}(?:\s+Jr\.?)?)\b")
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
_MENTION_AI_CALL_COUNT = 0
_MENTION_AI_BROKEN = False
_ENTITY_FA_MAP: dict[str, str] = {
    "bruno fernandes": "برونو فرناندس",
    "fernandes": "فرناندس",
    "marcus rashford": "مارکوس رشفورد",
    "rashford": "رشفورد",
    "jude bellingham": "جود بلینگهم",
    "bellingham": "بلینگهم",
    "eric garcia": "اریک گارسیا",
    "messi": "مسی",
    "lionel messi": "لیونل مسی",
    "vini jr": "وینی جونیور",
    "vinicius jr": "وینیسیوس جونیور",
    "hansi flick": "هانسی فلیک",
    "lamine yamal": "لامین یامال",
    "cristiano ronaldo": "کریستیانو رونالدو",
    "real madrid": "رئال مادرید",
    "barcelona": "بارسلونا",
    "barca": "بارسا",
    "laliga": "لالیگا",
    "bundesliga": "بوندس‌لیگا",
    "ucl": "لیگ قهرمانان اروپا",
}
_USERNAME_FA_MAP: dict[str, str] = {
    "realmadrid": "رئال مادرید",
    "real_madrid": "رئال مادرید",
    "barcelonaen": "بارسلونا",
    "barcelona": "بارسلونا",
    "barca": "بارسا",
    "cristiano": "کریستیانو رونالدو",
    "lamine_yamal_official": "لامین یامال",
    "espnfc_news": "ESPN FC",
}
_GENERIC_NAME_SKIP = {
    "The",
    "A",
    "An",
    "After",
    "Before",
    "With",
    "Without",
    "Once",
    "Again",
}
_FALLBACK_PHRASE_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\binstagram story\b", flags=re.IGNORECASE), "استوری اینستاگرام"),
    (re.compile(r"\bback[\s-]?to[\s-]?back\b", flags=re.IGNORECASE), "پیاپی"),
    (re.compile(r"\bchampions of spain\b", flags=re.IGNORECASE), "قهرمان اسپانیا"),
    (re.compile(r"\bwarrior mentality\b", flags=re.IGNORECASE), "روحیه جنگندگی"),
    (re.compile(r"\broyalty\b", flags=re.IGNORECASE), "سلطنتی"),
    (
        re.compile(
            r"\bthree\s+laliga'?s?\s+in\s+four\s+years\b",
            flags=re.IGNORECASE,
        ),
        "سه قهرمانی لالیگا در چهار سال",
    ),
    (
        re.compile(
            r"\bhas won the league in every season as a coach at the highest level\b",
            flags=re.IGNORECASE,
        ),
        "در همه فصل‌هایی که در بالاترین سطح مربیگری کرده قهرمان لیگ شده است",
    ),
    (
        re.compile(
            r"\bappeared to lose consciousness momentarily\b",
            flags=re.IGNORECASE,
        ),
        "برای لحظاتی هوشیاری‌اش را از دست داد",
    ),
    (
        re.compile(
            r"\bbriefly left the pitch before returning shortly after\b",
            flags=re.IGNORECASE,
        ),
        "برای لحظاتی زمین را ترک کرد و کمی بعد برگشت",
    ),
    (
        re.compile(r"\bmoment of silence was held for\b", flags=re.IGNORECASE),
        "یک دقیقه سکوت برای",
    ),
    (re.compile(r"\bpassed away today\b", flags=re.IGNORECASE), "امروز درگذشت"),
    (
        re.compile(r"\breminding barca fans that\b", flags=re.IGNORECASE),
        "به هواداران بارسا یادآوری کرد که",
    ),
    (
        re.compile(
            r"\bare once again the champions of spain\b",
            flags=re.IGNORECASE,
        ),
        "بار دیگر قهرمان اسپانیا شدند",
    ),
    (re.compile(r"\bwins his third laliga title\b", flags=re.IGNORECASE), "سومین قهرمانی لالیگای خود را کسب کرد"),
    (
        re.compile(r"\bhave\s+(\d+)\s+ucl\s+troph(?:y|ies)\b", flags=re.IGNORECASE),
        r"\1 قهرمانی لیگ قهرمانان اروپا دارند",
    ),
    (re.compile(r"\bwon the laliga title\b", flags=re.IGNORECASE), "قهرمان لالیگا شد"),
    (re.compile(r"\bwon the league title\b", flags=re.IGNORECASE), "قهرمان لیگ شد"),
    (re.compile(r"\bsuffering a clash with\b", flags=re.IGNORECASE), "پس از برخورد با"),
]
_FALLBACK_TOKEN_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\blaliga'?s?\b", flags=re.IGNORECASE), "لالیگا"),
    (re.compile(r"\bbundesliga\b", flags=re.IGNORECASE), "بوندس‌لیگا"),
    (re.compile(r"\bucl\b", flags=re.IGNORECASE), "لیگ قهرمانان اروپا"),
    (re.compile(r"\breal[\s_]?madrid\b", flags=re.IGNORECASE), "رئال مادرید"),
    (re.compile(r"\bbarcelona(?:en)?\b", flags=re.IGNORECASE), "بارسلونا"),
    (re.compile(r"\bbarca\b", flags=re.IGNORECASE), "بارسا"),
    (re.compile(r"\bvs\.?\b", flags=re.IGNORECASE), "مقابل"),
    (re.compile(r"\bagainst\b", flags=re.IGNORECASE), "مقابل"),
    (re.compile(r"\bafter\b", flags=re.IGNORECASE), "بعد از"),
    (re.compile(r"\bbefore\b", flags=re.IGNORECASE), "قبل از"),
    (re.compile(r"\bwith\b", flags=re.IGNORECASE), "با"),
    (re.compile(r"\bwithout\b", flags=re.IGNORECASE), "بدون"),
    (re.compile(r"\bvia\b", flags=re.IGNORECASE), "از طریق"),
    (re.compile(r"\bonce again\b", flags=re.IGNORECASE), "بار دیگر"),
    (re.compile(r"\bthe\b", flags=re.IGNORECASE), ""),
    (re.compile(r"\ba\b", flags=re.IGNORECASE), ""),
    (re.compile(r"\ban\b", flags=re.IGNORECASE), ""),
    (re.compile(r"\bchampions?\b", flags=re.IGNORECASE), "قهرمانان"),
    (re.compile(r"\btitle\b", flags=re.IGNORECASE), "قهرمانی"),
    (re.compile(r"\bleague\b", flags=re.IGNORECASE), "لیگ"),
    (re.compile(r"\bcoach\b", flags=re.IGNORECASE), "مربی"),
    (re.compile(r"\bseason\b", flags=re.IGNORECASE), "فصل"),
    (re.compile(r"\bseasons\b", flags=re.IGNORECASE), "فصل‌ها"),
    (re.compile(r"\bfans?\b", flags=re.IGNORECASE), "هواداران"),
    (re.compile(r"\btroph(?:y|ies)\b", flags=re.IGNORECASE), "جام"),
    (re.compile(r"\bstory\b", flags=re.IGNORECASE), "استوری"),
    (re.compile(r"\binstagram\b", flags=re.IGNORECASE), "اینستاگرام"),
    (re.compile(r"\bspain\b", flags=re.IGNORECASE), "اسپانیا"),
    (re.compile(r"\btoday\b", flags=re.IGNORECASE), "امروز"),
    (re.compile(r"\bfather\b", flags=re.IGNORECASE), "پدر"),
    (re.compile(r"\bpassed away\b", flags=re.IGNORECASE), "درگذشت"),
    (re.compile(r"\bwon\b", flags=re.IGNORECASE), "برنده شد"),
    (re.compile(r"\bwins\b", flags=re.IGNORECASE), "برنده شد"),
    (re.compile(r"\bwin\b", flags=re.IGNORECASE), "برد"),
    (re.compile(r"\bhave\b", flags=re.IGNORECASE), "دارند"),
    (re.compile(r"\bhas\b", flags=re.IGNORECASE), "دارد"),
    (re.compile(r"\bare\b", flags=re.IGNORECASE), "هستند"),
    (re.compile(r"\bis\b", flags=re.IGNORECASE), "است"),
    (re.compile(r"\bwas\b", flags=re.IGNORECASE), "بود"),
    (re.compile(r"\bwere\b", flags=re.IGNORECASE), "بودند"),
    (re.compile(r"\bonly\b", flags=re.IGNORECASE), "فقط"),
    (re.compile(r"\band\b", flags=re.IGNORECASE), "و"),
]


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
        msg_type = raw_kind if raw_kind in PASSTHROUGH_TYPES else "document"
        local_name = _resolve_local_name(item)
        if not local_name:
            continue
        obj: dict[str, str] = {"type": msg_type, "path": local_name}
        if not caption_assigned and msg_type in CAPTION_TYPES and (caption or text):
            obj["caption"] = caption or text or ""
            caption_assigned = True
        out.append(obj)

    if text and not out:
        out.append({"type": "text", "text": text})
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
            # Fallback to local passthrough if shared default script fails.
            pass
    return _build_fallback_passthrough_messages(payload)


def _load_allowed_mentions(payload: dict) -> set[str]:
    allowed: set[str] = set()

    def _add_from_route(route: dict) -> None:
        for key in ("destination_channel_username", "destination_target"):
            value = route.get(key)
            if isinstance(value, str) and value.strip().startswith("@"):
                allowed.add(value.strip().lower())

    route = payload.get("route")
    if isinstance(route, dict):
        _add_from_route(route)

    config_paths = [
        os.getenv("CHANNELS_CONFIG_PATH", "").strip() or "./config/channels.json",
        "./config/channels.example.json",
    ]

    seen_paths: set[str] = set()
    for raw_path in config_paths:
        path = str(raw_path).strip()
        if not path or path in seen_paths:
            continue
        seen_paths.add(path)
        cfg = Path(path)
        if not cfg.exists() or not cfg.is_file():
            continue
        try:
            data = json.loads(cfg.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(data, list):
            continue
        for item in data:
            if isinstance(item, dict):
                _add_from_route(item)

    return allowed


def _pick_ai_endpoint() -> str:
    return (
        os.getenv("ESPN_AI_ENDPOINT", "").strip()
        or os.getenv("SCRIPT_CLEAN_AI_ENDPOINT", "").strip()
        or os.getenv("FINAL_SCRIPT_AI_ENDPOINT", "").strip()
        or os.getenv("GUARD_AI_ENDPOINT", "").strip()
    )


def _pick_ai_model() -> str:
    return (
        os.getenv("ESPN_AI_MODEL", "").strip()
        or os.getenv("SCRIPT_CLEAN_AI_MODEL", "").strip()
        or os.getenv("FINAL_SCRIPT_AI_MODEL", "").strip()
        or os.getenv("GUARD_AI_MODEL", "").strip()
    )


def _ai_timeout_sec() -> float:
    return float(os.getenv("ESPN_AI_TIMEOUT_SEC", "8").strip() or "8")


def _mention_ai_max_calls() -> int:
    raw = os.getenv("ESPN_MENTION_AI_MAX_CALLS", "2").strip() or "2"
    try:
        return max(0, int(raw))
    except Exception:
        return 2


def _ai_retry_count() -> int:
    raw = os.getenv("ESPN_AI_RETRY_COUNT", "2").strip() or "2"
    try:
        return max(0, int(raw))
    except Exception:
        return 2


def _mention_ai_enabled() -> bool:
    return os.getenv("ESPN_MENTION_AI_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}


def _ai_fail_open() -> bool:
    return os.getenv("ESPN_AI_FAIL_OPEN", "true").strip().lower() in {"1", "true", "yes", "on"}


def _call_gemini_text(*, endpoint: str, body: dict, timeout_sec: float) -> str | None:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    retries = _ai_retry_count()
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


def _strip_md_fence(text: str) -> str:
    out = text.strip()
    if out.startswith("```"):
        out = re.sub(r"^```[a-zA-Z]*\n?", "", out)
        out = re.sub(r"\n?```$", "", out)
    return out.strip()


def _normalize_entity_key(text: str) -> str:
    out = text.strip().lower()
    out = out.replace("_", " ")
    out = re.sub(r"[^\w\s'-]+", " ", out)
    out = re.sub(r"\s+", " ", out)
    return out.strip()


def _lookup_entity_fa(text: str) -> str | None:
    key = _normalize_entity_key(text)
    if not key:
        return None
    if key in _ENTITY_FA_MAP:
        return _ENTITY_FA_MAP[key]
    key_no_space = key.replace(" ", "")
    if key_no_space in _USERNAME_FA_MAP:
        return _USERNAME_FA_MAP[key_no_space]
    if key in _USERNAME_FA_MAP:
        return _USERNAME_FA_MAP[key]
    return None


def _roman_token_to_persian(token: str) -> str:
    clean = token.strip().strip(".,:;!?")
    if not clean:
        return clean
    direct = _lookup_entity_fa(clean)
    if direct:
        return direct

    t = clean.lower().replace("'", "")
    t = t.replace("jr.", "junior").replace("jr", "junior")
    pairs = [
        ("sch", "ش"),
        ("sh", "ش"),
        ("ch", "چ"),
        ("kh", "خ"),
        ("gh", "غ"),
        ("zh", "ژ"),
        ("ph", "ف"),
        ("th", "ت"),
        ("qu", "کو"),
        ("ck", "ک"),
        ("oo", "و"),
        ("ee", "ی"),
        ("ou", "و"),
        ("ow", "او"),
        ("ai", "ای"),
        ("ay", "ی"),
        ("ey", "ی"),
        ("ie", "ی"),
    ]
    for src, dst in pairs:
        t = t.replace(src, dst)

    char_map = {
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
        " ": " ",
    }
    out_chars: list[str] = []
    for ch in t:
        out_chars.append(char_map.get(ch, ch))
    out = "".join(out_chars)
    out = re.sub(r"ِ+", "ِ", out)
    out = re.sub(r"\s+", " ", out).strip()
    return out or clean


def _to_persian_name(name: str) -> str:
    direct = _lookup_entity_fa(name)
    if direct:
        return direct
    parts = [p for p in re.split(r"\s+", name.strip()) if p]
    if not parts:
        return name
    return " ".join(_roman_token_to_persian(p) for p in parts)


def _looks_meta_or_prompt_like(text: str) -> bool:
    lowered = text.strip().lower()
    if not lowered:
        return True
    bad_fragments = (
        "you are ",
        "role:",
        "input text:",
        "translated text:",
        "protected placeholders:",
        "return only",
        "here is",
        "i translated",
        "translation:",
    )
    return any(fragment in lowered for fragment in bad_fragments)


def _has_enough_persian(text: str) -> bool:
    persian_chars = len(PERSIAN_CHAR_RE.findall(text))
    latin_chars = len(LATIN_CHAR_RE.findall(text))
    if persian_chars <= 0:
        return False
    if latin_chars <= 0:
        return True
    return persian_chars >= max(8, int(latin_chars * 0.35))


def _fallback_translate_football_text(text: str) -> str:
    out = text
    out = re.sub(r"\b([A-Za-z][A-Za-z0-9 .-]{1,40})'s\b", r"\1", out)
    for pattern, repl in _FALLBACK_PHRASE_RULES:
        out = pattern.sub(repl, out)
    for pattern, repl in _FALLBACK_TOKEN_RULES:
        out = pattern.sub(repl, out)

    # Lightweight grammar shaping for common English fragments left in football captions.
    out = re.sub(r"\bhe'?s only (\d+) years old\b", r"او فقط \1 سال دارد", out, flags=re.IGNORECASE)
    out = re.sub(r"\b(\d+)\s+years old\b", r"\1 ساله", out, flags=re.IGNORECASE)
    out = re.sub(r"\bبعد از پس از\b", "پس از", out)
    out = out.replace("مقابل.", "مقابل")
    out = re.sub(r"\s+([،؛:!؟.,])", r"\1", out)
    out = re.sub(r"([،؛:!؟.,])(\S)", r"\1 \2", out)
    out = re.sub(r"\s{2,}", " ", out)
    out = re.sub(r"\s+", " ", out)
    return _normalize_text(out)


def _polish_fluent_persian(text: str, *, allowed_mentions: set[str]) -> str:
    if os.getenv("ESPN_FLUENT_POLISH_ENABLED", "false").strip().lower() not in {"1", "true", "yes", "on"}:
        return text
    endpoint = _pick_ai_endpoint()
    model = _pick_ai_model()
    if not endpoint:
        return text

    protected: dict[str, str] = {}
    work = text
    for idx, mention in enumerate(sorted(allowed_mentions)):
        ph = f"__ALLOWED_MENTION_{idx}__"
        protected[ph] = mention
        work = work.replace(mention, ph)

    prompt = (
        "متن زیر را برای مخاطب فارسی‌زبان به شکل روان و طبیعی بازنویسی کن.\n"
        "ترجمه تحت‌اللفظی نکن.\n"
        "معنی، اطلاعات، اعداد و لحن خبری را حفظ کن.\n"
        "اسم بازیکن‌ها و تیم‌ها را با نوشتار فارسی رایج بنویس.\n"
        "هیچ اطلاعاتی اضافه یا حذف نکن.\n"
        "اگر placeholder وجود دارد، دقیقا همان را نگه دار.\n"
        "فقط متن نهایی را برگردان.\n\n"
        f"متن:\n{work}"
    )
    body: dict = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2},
    }
    if model:
        body["model"] = model

    out = _call_gemini_text(endpoint=endpoint, body=body, timeout_sec=_ai_timeout_sec())
    if out is None:
        return text
    out = _normalize_text(_strip_md_fence(out))
    if not out or _looks_meta_or_prompt_like(out):
        return text
    if not _has_enough_persian(out):
        strict_prompt = (
            "متن زیر را برای انتشار در کانال فارسی بازنویسی کن.\n"
            "خروجی باید کاملا فارسی و روان باشد.\n"
            "از واژه انگلیسی استفاده نکن، مگر URL یا placeholder.\n"
            "فقط متن نهایی را برگردان.\n\n"
            f"متن:\n{work}"
        )
        strict_body: dict = {
            "contents": [{"role": "user", "parts": [{"text": strict_prompt}]}],
            "generationConfig": {"temperature": 0},
        }
        if model:
            strict_body["model"] = model
        strict_out = _call_gemini_text(endpoint=endpoint, body=strict_body, timeout_sec=_ai_timeout_sec())
        if strict_out is not None:
            candidate = _normalize_text(_strip_md_fence(strict_out))
            if candidate and not _looks_meta_or_prompt_like(candidate):
                out = candidate
    for ph, mention in protected.items():
        out = out.replace(ph, mention)
    return out if _has_enough_persian(out) else text


def _postprocess_general_farsi(text: str, *, allowed_mentions: set[str]) -> str:
    out = _normalize_text(text)
    if not out:
        return ""

    protected: dict[str, str] = {}
    for idx, mention in enumerate(sorted(allowed_mentions)):
        ph = f"__ALLOWED_{idx}__"
        protected[ph] = mention
        out = out.replace(mention, ph)

    def _replace_name_seq(match: re.Match[str]) -> str:
        candidate = match.group(1)
        first = candidate.split(" ", 1)[0]
        if first in _GENERIC_NAME_SKIP:
            return candidate
        return _to_persian_name(candidate)

    out = NAME_SEQ_RE.sub(_replace_name_seq, out)
    out = re.sub(r"@+[A-Za-z0-9_]{3,}", "", out)
    out = out.replace(" .", ".").replace(" ،", "،")
    out = re.sub(r"\s+([،؛:!؟.,])", r"\1", out)
    out = re.sub(r"([،؛:!؟.,])([^\s])", r"\1 \2", out)
    out = re.sub(r"\s+", " ", out).strip()

    for ph, mention in protected.items():
        out = out.replace(ph, mention)
    return _normalize_text(out)


def _translate_football_text(text: str, *, placeholders: list[str]) -> str:
    endpoint = _pick_ai_endpoint()
    model = _pick_ai_model()
    timeout_sec = _ai_timeout_sec()
    fail_open = _ai_fail_open()

    if not endpoint:
        return _fallback_translate_football_text(text)

    placeholder_hint = ""
    if placeholders:
        placeholder_hint = "\n".join(f"- {token}" for token in placeholders)

    prompt = (
        "You are a professional football news editor.\n"
        "Rewrite and translate the text into fluent Persian for a Persian-speaking audience.\n"
        "Do NOT do literal translation; make it natural and readable while preserving the exact meaning.\n"
        "Keep all facts, numbers and entities; do not add or remove information.\n"
        "Write player/team names in common Persian script.\n"
        "Keep line breaks structure as much as possible.\n"
        "If placeholders exist, keep them EXACTLY unchanged.\n"
        "Return only the final Persian text.\n"
    )
    if placeholder_hint:
        prompt += f"\nProtected placeholders:\n{placeholder_hint}\n"
    prompt += f"\nInput text:\n{text}"

    body: dict = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2},
    }
    if model:
        body["model"] = model

    out = _call_gemini_text(endpoint=endpoint, body=body, timeout_sec=timeout_sec)
    if out is None:
        fb = _fallback_translate_football_text(text)
        return fb or (text if fail_open else "")

    out = _strip_md_fence(out)
    out = _normalize_text(out)
    if out and not _looks_meta_or_prompt_like(out) and _looks_like_expected_persian(out, source=text):
        return out

    # Retry once with stricter constraint when model answers in non-Persian script.
    strict_prompt = prompt + "\nIMPORTANT: Final answer MUST be in Persian script."
    strict_body: dict = {
        "contents": [{"role": "user", "parts": [{"text": strict_prompt}]}],
        "generationConfig": {"temperature": 0},
    }
    if model:
        strict_body["model"] = model

    strict_out = _call_gemini_text(endpoint=endpoint, body=strict_body, timeout_sec=timeout_sec)
    if strict_out is None:
        fb = _fallback_translate_football_text(text)
        return fb or (text if fail_open else "")
    strict_out = _normalize_text(_strip_md_fence(strict_out))
    if strict_out and not _looks_meta_or_prompt_like(strict_out) and _looks_like_expected_persian(strict_out, source=text):
        return strict_out
    fb = _fallback_translate_football_text(text)
    if fb:
        return fb
    return out or (text if fail_open else "")


def _looks_like_expected_persian(output_text: str, *, source: str) -> bool:
    if not output_text.strip():
        return False
    if _has_enough_persian(output_text):
        return True
    if _looks_meta_or_prompt_like(output_text):
        return False
    # If source is not latin/english-heavy, accept non-Persian output as-is.
    return not LATIN_CHAR_RE.search(source)


def _replace_unknown_mention(*, mention: str, translated_with_placeholders: str, original_text: str) -> str:
    global _MENTION_AI_CALL_COUNT, _MENTION_AI_BROKEN
    endpoint = _pick_ai_endpoint()
    model = _pick_ai_model()
    timeout_sec = _ai_timeout_sec()

    fallback = mention.lstrip("@").replace("_", " ").strip()
    by_map = _lookup_entity_fa(fallback)
    if by_map:
        fallback = by_map
    lowered = mention.strip().lower()
    if "real" in lowered and "madrid" in lowered:
        fallback = "رئال مادرید"
    elif "barcelona" in lowered or lowered in {"@barca", "@barcelonaen", "@barcelona"}:
        fallback = "بارسلونا"
    elif "espn" in lowered:
        fallback = "ESPN"
    elif not fallback:
        fallback = "بازیکن"
    else:
        fallback = _to_persian_name(fallback)

    if not endpoint:
        return fallback
    if not _mention_ai_enabled():
        return fallback

    if _MENTION_AI_BROKEN:
        return fallback
    if _MENTION_AI_CALL_COUNT >= _mention_ai_max_calls():
        return fallback

    prompt = (
        "You are fixing a football text in Persian.\n"
        "There is a username mention that must be replaced with a natural football entity label.\n"
        "It can be a player name, team name, club, or football media source according to context.\n"
        "Return only a short Persian phrase (1 to 4 words), no @, no hashtag, no explanation.\n"
        f"Mention: {mention}\n"
        f"Original text:\n{original_text}\n\n"
        f"Translated text:\n{translated_with_placeholders}"
    )

    body: dict = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2},
    }
    if model:
        body["model"] = model

    out = _call_gemini_text(endpoint=endpoint, body=body, timeout_sec=timeout_sec)
    if out is None:
        _MENTION_AI_BROKEN = True
        return fallback
    _MENTION_AI_CALL_COUNT += 1

    out = _strip_md_fence(out)
    out = out.replace("@", " ")
    out = re.sub(r"[#\n\r\t]+", " ", out)
    out = re.sub(r"\s+", " ", out).strip(" .,:;!?؟،")
    if not out:
        return fallback

    words = out.split()
    if len(words) > 4:
        out = " ".join(words[:4])
    return out


def _localize_text(text: str, *, allowed_mentions: set[str]) -> str:
    normalized = _normalize_text(text)
    if not normalized:
        return ""

    mentions = MENTION_RE.findall(normalized)
    if not mentions:
        translated_plain = _translate_football_text(normalized, placeholders=[])
        polished_plain = _polish_fluent_persian(translated_plain, allowed_mentions=allowed_mentions)
        return _postprocess_general_farsi(polished_plain, allowed_mentions=allowed_mentions)

    mentions_unique: list[str] = []
    for token in mentions:
        if token not in mentions_unique:
            mentions_unique.append(token)

    placeholder_map: dict[str, str] = {}
    replaced = normalized
    for idx, token in enumerate(mentions_unique):
        ph = f"__MENTION_{idx}__"
        placeholder_map[ph] = token
        replaced = replaced.replace(token, ph)

    translated = _translate_football_text(replaced, placeholders=list(placeholder_map.keys()))

    def _replace_placeholder(match: re.Match[str]) -> str:
        ph = match.group(0)
        mention = placeholder_map.get(ph)
        if mention is None:
            return ph
        if mention.lower() in allowed_mentions:
            return mention
        return _replace_unknown_mention(
            mention=mention,
            translated_with_placeholders=translated,
            original_text=normalized,
        )

    translated = PLACEHOLDER_RE.sub(_replace_placeholder, translated)
    translated = _normalize_text(translated)
    polished = _polish_fluent_persian(translated, allowed_mentions=allowed_mentions)
    return _postprocess_general_farsi(polished, allowed_mentions=allowed_mentions)


def _process_messages(messages: list[dict], *, allowed_mentions: set[str]) -> list[dict]:
    out: list[dict] = []
    for item in messages:
        if not isinstance(item, dict):
            continue

        cloned = dict(item)
        text = cloned.get("text")
        if isinstance(text, str) and text.strip():
            cleaned = _localize_text(text, allowed_mentions=allowed_mentions)
            if cleaned:
                cloned["text"] = cleaned

        caption = cloned.get("caption")
        if isinstance(caption, str) and caption.strip():
            cleaned_caption = _localize_text(caption, allowed_mentions=allowed_mentions)
            if cleaned_caption:
                cloned["caption"] = cleaned_caption

        out.append(cloned)

    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--payload", required=True)
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()

    payload = _load_payload(Path(args.payload))
    base_messages = _build_base_messages(payload)
    allowed_mentions = _load_allowed_mentions(payload)
    messages = _process_messages(base_messages, allowed_mentions=allowed_mentions)

    print(json.dumps({"messages": messages}, ensure_ascii=False))


if __name__ == "__main__":
    main()
