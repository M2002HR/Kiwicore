from __future__ import annotations

import json
import os
import re
from pathlib import Path

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
        self._entries: dict[str, list[tuple[str, str]]] = {}
        self._global_keywords: list[tuple[str, str]] = []
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

        deduped: list[tuple[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for keyword, link in keyword_pairs:
            key = (keyword.casefold(), link)
            if key in seen:
                continue
            deduped.append((keyword, link))
            seen.add(key)

        patterns = [
            (
                self._build_keyword_pattern(keyword),
                link,
            )
            for keyword, link in sorted(deduped, key=lambda item: len(item[0]), reverse=True)
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

        parsed: dict[str, list[tuple[str, str]]] = {}
        global_pairs: list[tuple[str, str]] = []
        global_seen: set[tuple[str, str]] = set()
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
                keywords = item.get("keywords")
                if not isinstance(keywords, list):
                    continue
                bucket = parsed.setdefault(destination, [])
                seen: set[str] = {k.casefold() for k, _ in bucket}
                for keyword in keywords:
                    if not isinstance(keyword, str):
                        continue
                    cleaned = keyword.strip()
                    if not cleaned:
                        continue
                    key = cleaned.casefold()
                    if key in seen:
                        continue
                    bucket.append((cleaned, link))
                    seen.add(key)
                    global_key = (cleaned.casefold(), link)
                    if global_key in global_seen:
                        continue
                    global_pairs.append((cleaned, link))
                    global_seen.add(global_key)

        self._entries = parsed
        self._global_keywords = global_pairs
        self._mtime_ns = mtime_ns

    @staticmethod
    def _linkify_text(text: str, patterns: list[tuple[re.Pattern[str], str]]) -> str:
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
    def _apply_patterns(chunk: str, patterns: list[tuple[re.Pattern[str], str]]) -> str:
        out = chunk
        for pattern, link in patterns:
            out = pattern.sub(lambda m: f"[{m.group(1)}]({link})", out)
        return out

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
