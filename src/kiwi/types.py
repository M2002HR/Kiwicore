from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


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
    enabled: bool
    source_channel_id: str | None
    source_channel_username: str | None
    destination_channel_id: str | None
    destination_channel_username: str | None
    channel_script: str | None
    max_message_mb: int | None
    gaurd_script: str | None = "default_guard.py"
    sync_enabled: bool = False
    sync_status: str = "active"
    sync_backfill_count: int = 100
    sync_interval_sec: int = 300
    sync_batch_size: int = 1
    sync_retry_attempts: int = 2
    sync_pending_count: int = 0
    sync_processed_count: int = 0
    sync_seeded: bool = False

    def destination_target(self) -> str:
        if self.destination_channel_username:
            return self.destination_channel_username
        if self.destination_channel_id:
            return self.destination_channel_id
        raise ValueError("Route has no destination target")

    def is_syncing(self) -> bool:
        if not bool(self.sync_enabled):
            return False
        status = str(self.sync_status).strip().lower()
        return status == "syncing" or not bool(self.sync_seeded)

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
