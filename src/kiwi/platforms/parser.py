from __future__ import annotations

from kiwi.types import IncomingChannelMessage, IncomingMedia, MediaKind
from kiwi.utils import normalize_channel_id, normalize_channel_username


def parse_telegram_channel_update(update: dict) -> IncomingChannelMessage | None:
    raw_message = update.get("channel_post")
    if raw_message is None:
        raw_message = update.get("edited_channel_post")
    if not isinstance(raw_message, dict):
        return None

    chat = raw_message.get("chat") or {}
    if (chat.get("type") or "").strip().lower() != "channel":
        return None

    update_id = update.get("update_id")
    chat_id = normalize_channel_id(chat.get("id"))
    if update_id is None or chat_id is None:
        return None

    channel_username = normalize_channel_username(chat.get("username"))

    medias: list[IncomingMedia] = []
    consumed_keys: set[str] = set()

    photos = raw_message.get("photo")
    if isinstance(photos, list) and photos:
        consumed_keys.add("photo")
        largest = photos[-1]
        file_id = largest.get("file_id")
        if isinstance(file_id, str) and file_id:
            medias.append(
                IncomingMedia(
                    kind=MediaKind.PHOTO,
                    file_id=file_id,
                    file_size=_to_int_or_none(largest.get("file_size")),
                )
            )

    for key, kind in (
        ("video", MediaKind.VIDEO),
        ("voice", MediaKind.VOICE),
        ("audio", MediaKind.AUDIO),
        ("document", MediaKind.DOCUMENT),
        ("animation", MediaKind.ANIMATION),
        ("sticker", MediaKind.STICKER),
        ("video_note", MediaKind.VIDEO_NOTE),
    ):
        part = raw_message.get(key)
        if not isinstance(part, dict):
            continue
        consumed_keys.add(key)
        file_id = part.get("file_id")
        if not isinstance(file_id, str) or not file_id:
            continue
        medias.append(
            IncomingMedia(
                kind=kind,
                file_id=file_id,
                file_size=_to_int_or_none(part.get("file_size")),
                file_name=str(part.get("file_name")) if part.get("file_name") else None,
                mime_type=str(part.get("mime_type")) if part.get("mime_type") else None,
                duration=_to_int_or_none(part.get("duration")),
            )
        )

    # Fallback for new/unknown media objects that still carry a file_id.
    skip_fallback_keys = consumed_keys | {
        "chat",
        "from",
        "sender_chat",
        "entities",
        "caption_entities",
        "forward_origin",
        "reply_markup",
    }
    for key, value in raw_message.items():
        if key in skip_fallback_keys:
            continue

        file_part: dict | None = None
        if isinstance(value, dict) and isinstance(value.get("file_id"), str):
            file_part = value
        elif isinstance(value, list) and value and isinstance(value[-1], dict) and isinstance(value[-1].get("file_id"), str):
            file_part = value[-1]

        if not file_part:
            continue

        medias.append(
            IncomingMedia(
                kind=MediaKind.DOCUMENT,
                file_id=str(file_part.get("file_id")),
                file_size=_to_int_or_none(file_part.get("file_size")),
                file_name=str(file_part.get("file_name")) if file_part.get("file_name") else None,
                mime_type=str(file_part.get("mime_type")) if file_part.get("mime_type") else None,
                duration=_to_int_or_none(file_part.get("duration")),
            )
        )

    text = raw_message.get("text")
    caption = raw_message.get("caption")

    return IncomingChannelMessage(
        update_id=int(update_id),
        source_channel_id=chat_id,
        source_channel_username=channel_username,
        message_id=int(raw_message.get("message_id") or 0),
        date=_to_int_or_none(raw_message.get("date")),
        text=text.strip() if isinstance(text, str) and text.strip() else None,
        caption=caption.strip() if isinstance(caption, str) and caption.strip() else None,
        medias=medias,
        raw=update,
    )


def _to_int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None
