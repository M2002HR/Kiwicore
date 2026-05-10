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
    script: str
    max_message_mb: int | None
    gaurd_script: str = "default_guard.py"

    def destination_target(self) -> str:
        if self.destination_channel_username:
            return self.destination_channel_username
        if self.destination_channel_id:
            return self.destination_channel_id
        raise ValueError("Route has no destination target")


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
