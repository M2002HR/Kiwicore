from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from kiwi.types import ScriptOutputMessage

TOKEN_CHAR_CLASS = r"A-Za-z0-9_\u0600-\u06FF"
PROTECTED_RE = re.compile(r"\[[^\]\n]+\]\([^)]+\)|https?://\S+", re.IGNORECASE)
KEYWORD_SPLIT_RE = re.compile(r"[\s\u200c\u200f\-_]+")
KEYWORD_SEP_RE = r"[\s\u200c\u200f\-_]*"
CHAR_ALIASES: dict[str, str] = {
    "ی": "یي",
    "ي": "یي",
    "ک": "کك",
    "ك": "کك",
    "ه": "هة",
    "ة": "هة",
}


class KeywordLinker:
    def __init__(self, *, channels_config_path: str) -> None:
        self._config_path = self._resolve_config_path(channels_config_path=channels_config_path)
        self._entries: dict[str, list[tuple[str, str, int]]] = {}
        self._global_keywords: list[tuple[str, str, int]] = []
        self._mtime_ns: int | None = None
        self._load_if_changed()

    @staticmethod
    def _resolve_config_path(*, channels_config_path: str) -> Path:
        raw = os.getenv("KEYWORD_LINKS_CONFIG_PATH", "").strip()
        if raw:
            return Path(raw)
        channels_path = Path(channels_config_path)
        return channels_path.parent / "keyword_links.json"

    def apply(self, destination_target: str, messages: list[ScriptOutputMessage]) -> list[ScriptOutputMessage]:
        self._load_if_changed()
        keyword_pairs = list(self._global_keywords)
        destination = str(destination_target or "").strip().lower()
        if destination:
            keyword_pairs.extend(self._entries.get(destination, []))
        if not keyword_pairs:
            return messages

        deduped: list[tuple[str, str, int]] = []
        seen: dict[tuple[str, str], int] = {}
        for keyword, link, priority in keyword_pairs:
            key = (keyword.casefold(), link)
            if key in seen:
                if priority > seen[key]:
                    seen[key] = priority
                    for idx, (existing_kw, existing_link, _) in enumerate(deduped):
                        if existing_kw.casefold() == key[0] and existing_link == key[1]:
                            deduped[idx] = (existing_kw, existing_link, priority)
                continue
            deduped.append((keyword, link, priority))
            seen[key] = priority

        patterns = [
            (
                self._build_keyword_pattern(keyword),
                link,
                int(priority),
                len(keyword),
                idx,
            )
            for idx, (keyword, link, priority) in enumerate(
                sorted(deduped, key=lambda item: (-int(item[2]), -len(item[0]), item[0].casefold(), item[1]))
            )
        ]
        if not patterns:
            return messages

        out: list[ScriptOutputMessage] = []
        for msg in messages:
            text = self._linkify_text(msg.text, patterns) if isinstance(msg.text, str) and msg.text.strip() else msg.text
            caption = self._linkify_text(msg.caption, patterns) if isinstance(msg.caption, str) and msg.caption.strip() else msg.caption
            out.append(ScriptOutputMessage(type=msg.type, text=text, path=msg.path, caption=caption))
        return out

    def _load_if_changed(self) -> None:
        path = self._config_path
        if not path.exists():
            self._entries = {}
            self._mtime_ns = None
            return
        try:
            stat = path.stat()
            mtime_ns = int(getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000)))
        except OSError:
            self._entries = {}
            self._mtime_ns = None
            return

        if self._mtime_ns is not None and mtime_ns == self._mtime_ns:
            return

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            self._entries = {}
            self._mtime_ns = mtime_ns
            return

        parsed: dict[str, list[tuple[str, str, int]]] = {}
        global_pairs: list[tuple[str, str, int]] = []
        global_seen: dict[tuple[str, str], int] = {}
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                destination = str(item.get("destination") or "").strip().lower()
                if not destination:
                    continue
                link = str(item.get("link") or "").strip()
                if not link:
                    if destination.startswith("@") and len(destination) > 1:
                        link = f"https://ble.ir/{destination[1:]}"
                    else:
                        continue
                entry_priority = _safe_int(item.get("priority"), default=0)
                keywords = item.get("keywords")
                if not isinstance(keywords, list):
                    continue
                bucket = parsed.setdefault(destination, [])
                seen: dict[str, int] = {k.casefold(): p for k, _, p in bucket}
                for keyword in keywords:
                    cleaned, kw_priority = _normalize_keyword_item(keyword, default_priority=entry_priority)
                    if not cleaned:
                        continue
                    kw_priority = int(kw_priority)
                    key = cleaned.casefold()
                    if key in seen:
                        if kw_priority > int(seen[key]):
                            seen[key] = kw_priority
                            for idx, (existing_kw, existing_link, _) in enumerate(bucket):
                                if existing_kw.casefold() == key and existing_link == link:
                                    bucket[idx] = (existing_kw, existing_link, kw_priority)
                        continue
                    bucket.append((cleaned, link, kw_priority))
                    seen[key] = kw_priority
                    global_key = (cleaned.casefold(), link)
                    if global_key in global_seen:
                        if kw_priority > int(global_seen[global_key]):
                            global_seen[global_key] = kw_priority
                            for idx, (existing_kw, existing_link, _) in enumerate(global_pairs):
                                if existing_kw.casefold() == global_key[0] and existing_link == global_key[1]:
                                    global_pairs[idx] = (existing_kw, existing_link, kw_priority)
                        continue
                    global_pairs.append((cleaned, link, kw_priority))
                    global_seen[global_key] = kw_priority

        self._entries = parsed
        self._global_keywords = global_pairs
        self._mtime_ns = mtime_ns

    @staticmethod
    def _linkify_text(text: str, patterns: list[tuple[re.Pattern[str], str, int, int, int]]) -> str:
        parts: list[str] = []
        last = 0
        for match in PROTECTED_RE.finditer(text):
            if match.start() > last:
                parts.append(KeywordLinker._apply_patterns(text[last : match.start()], patterns))
            parts.append(match.group(0))
            last = match.end()
        if last < len(text):
            parts.append(KeywordLinker._apply_patterns(text[last:], patterns))
        return "".join(parts)

    @staticmethod
    def _apply_patterns(chunk: str, patterns: list[tuple[re.Pattern[str], str, int, int, int]]) -> str:
        if not chunk:
            return chunk
        candidates: list[tuple[int, int, int, int, int, str, str]] = []
        for pattern, link, priority, keyword_len, order in patterns:
            for m in pattern.finditer(chunk):
                start = int(m.start(1))
                end = int(m.end(1))
                if end <= start:
                    continue
                candidates.append((start, end, int(priority), int(keyword_len), int(order), m.group(1), link))
        if not candidates:
            return chunk
        candidates.sort(key=lambda row: (row[0], -row[2], -row[3], row[4]))

        accepted: list[tuple[int, int, str, str]] = []
        cursor = 0
        idx = 0
        n = len(candidates)
        while idx < n:
            start = candidates[idx][0]
            if start < cursor:
                idx += 1
                continue
            best = candidates[idx]
            j = idx + 1
            while j < n and candidates[j][0] == start:
                contender = candidates[j]
                if contender[2] > best[2]:
                    best = contender
                elif contender[2] == best[2] and contender[3] > best[3]:
                    best = contender
                elif contender[2] == best[2] and contender[3] == best[3] and contender[4] < best[4]:
                    best = contender
                j += 1
            if best[0] >= cursor:
                accepted.append((best[0], best[1], best[5], best[6]))
                cursor = best[1]
            idx = j

        if not accepted:
            return chunk

        out_parts: list[str] = []
        last = 0
        for start, end, label, link in accepted:
            if start > last:
                out_parts.append(chunk[last:start])
            out_parts.append(f"[{label}]({link})")
            last = end
        if last < len(chunk):
            out_parts.append(chunk[last:])
        return "".join(out_parts)

    @staticmethod
    def _char_pattern(ch: str) -> str:
        alias = CHAR_ALIASES.get(ch)
        if alias:
            return f"[{re.escape(alias)}]"
        return re.escape(ch)

    @classmethod
    def _token_pattern(cls, token: str) -> str:
        return "".join(cls._char_pattern(ch) for ch in token)

    @classmethod
    def _build_keyword_pattern(cls, keyword: str) -> re.Pattern[str]:
        parts = [part for part in KEYWORD_SPLIT_RE.split(keyword.strip()) if part]
        if not parts:
            escaped = re.escape(keyword.strip())
        else:
            escaped = KEYWORD_SEP_RE.join(cls._token_pattern(part) for part in parts)
        return re.compile(rf"(?<![{TOKEN_CHAR_CLASS}\[@])({escaped})(?![{TOKEN_CHAR_CLASS}\]])", re.IGNORECASE)


def _safe_int(value: object, *, default: int = 0) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except Exception:
        return int(default)


def _normalize_keyword_item(value: Any, *, default_priority: int = 0) -> tuple[str, int]:
    if isinstance(value, str):
        return value.strip(), int(default_priority)
    if isinstance(value, dict):
        text = str(value.get("keyword") or value.get("text") or value.get("term") or "").strip()
        pr = _safe_int(value.get("priority"), default=default_priority)
        return text, int(pr)
    return "", int(default_priority)
