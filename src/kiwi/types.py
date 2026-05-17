from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from kiwi.utils import normalize_channel_id, normalize_channel_username


class MediaKind(str, Enum):
    PHOTO = "photo"
    VIDEO = "video"
    VOICE = "voice"
    AUDIO = "audio"
    DOCUMENT = "document"
    ANIMATION = "animation"
    STICKER = "sticker"
    VIDEO_NOTE = "video_note"


class OutputMessageKind(str, Enum):
    TEXT = "text"
    PHOTO = "photo"
    VIDEO = "video"
    VOICE = "voice"
    AUDIO = "audio"
    DOCUMENT = "document"
    ANIMATION = "animation"
    STICKER = "sticker"
    VIDEO_NOTE = "video_note"


@dataclass(slots=True)
class IncomingMedia:
    kind: MediaKind
    file_id: str
    file_size: int | None = None
    file_name: str | None = None
    mime_type: str | None = None
    duration: int | None = None
    source: str | None = None
    source_ref: dict | None = None


@dataclass(slots=True)
class IncomingChannelMessage:
    update_id: int
    source_channel_id: str
    source_channel_username: str | None
    message_id: int
    date: int | None
    text: str | None
    caption: str | None
    medias: list[IncomingMedia]
    raw: dict
    media_group_id: str | None = None


@dataclass(slots=True)
class ChannelRoute:
    name: str
    enabled: bool = True
    source_channel_id: str | None = None
    source_channel_username: str | None = None
    destination_channel_id: str | None = None
    destination_channel_username: str | None = None
    channel_script: str | None = None
    max_message_mb: int | None = None
    gaurd_script: str | None = "default_guard.py"
    sync_enabled: bool = False
    sync_status: str = "synced"
    sync_backfill_count: int = 100
    sync_interval_sec: int = 1
    sync_batch_size: int = 1
    sync_retry_attempts: int = 2
    sync_seeded: bool = False
    status: str | None = None

    def __post_init__(self) -> None:
        source_username = str(self.source_channel_username or "").strip()
        source_id = normalize_channel_id(self.source_channel_id)
        if source_id is None and source_username and not source_username.startswith("@"):
            lowered = source_username.lower()
            if source_username.lstrip("-").isdigit() and not lowered.startswith("https://t.me/") and not lowered.startswith(
                "http://t.me/"
            ):
                source_id = normalize_channel_id(source_username)
                source_username = ""
        self.source_channel_id = source_id
        self.source_channel_username = normalize_channel_username(source_username) if source_username else None

        if self.status is None or str(self.status).strip() == "":
            legacy_sync_status = str(self.sync_status or "").strip().lower()
            if bool(self.sync_enabled) and (legacy_sync_status in {"syncing", "active"} or not bool(self.sync_seeded)):
                normalized = "syncing"
            else:
                normalized = "synced" if bool(self.enabled) else "deactive"
        else:
            normalized = str(self.status or "").strip().lower()
        if normalized not in {"deactive", "syncing", "synced"}:
            normalized = "deactive"
        self.status = normalized
        self.enabled = normalized != "deactive"
        self.sync_enabled = normalized == "syncing"
        self.sync_status = normalized
        self.sync_seeded = normalized != "deactive"

    def destination_target(self) -> str:
        if self.destination_channel_username:
            return self.destination_channel_username
        if self.destination_channel_id:
            return self.destination_channel_id
        raise ValueError("Route has no destination target")

    def is_deactive(self) -> bool:
        return self.status == "deactive"

    def is_synced(self) -> bool:
        return self.status == "synced"

    def is_syncing(self) -> bool:
        return self.status == "syncing"

    def is_active(self) -> bool:
        return self.status in {"syncing", "synced"}

    @property
    def script(self) -> str | None:
        # Backward compatibility for older call-sites/tests.
        return self.channel_script

    @script.setter
    def script(self, value: str | None) -> None:
        self.channel_script = value


@dataclass(slots=True)
class PreparedMessagePaths:
    base_dir: str
    input_dir: str
    output_dir: str
    payload_path: str
    raw_update_path: str


@dataclass(slots=True)
class ScriptOutputMessage:
    type: OutputMessageKind
    text: str | None = None
    path: str | None = None
    caption: str | None = None


@dataclass(slots=True)
class ScriptRunResult:
    messages: list[ScriptOutputMessage]
    stdout: str
    stderr: str


@dataclass(slots=True)
class AdminInboundMessage:
    update_id: int
    chat_id: str
    user_id: str
    username: str | None
    message_id: int | None
    text: str | None
    callback_query_id: str | None
    callback_data: str | None
    callback_message_id: int | None
    raw: dict
