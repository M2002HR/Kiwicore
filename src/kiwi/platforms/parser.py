from __future__ import annotations

from kiwi.types import AdminInboundMessage, IncomingChannelMessage, IncomingMedia, MediaKind
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
                title=str(part.get("title")) if (key == "audio" and part.get("title")) else None,
                performer=str(part.get("performer")) if (key == "audio" and part.get("performer")) else None,
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
                title=str(file_part.get("title")) if file_part.get("title") else None,
                performer=str(file_part.get("performer")) if file_part.get("performer") else None,
            )
        )

    text = _extract_text(raw_message.get("text"), raw_message.get("entities"))
    caption = _extract_text(raw_message.get("caption"), raw_message.get("caption_entities"))
    if not text and not caption:
        poll_text = _extract_poll_text(raw_message.get("poll"))
        if poll_text:
            text = poll_text

    reply_summary = _extract_reply_summary(raw_message.get("reply_to_message"))
    if reply_summary:
        if text:
            text = f"🖊 نقل‌قول:\n«{reply_summary}»\n\n{text}"
        elif caption:
            caption = f"🖊 نقل‌قول:\n«{reply_summary}»\n\n{caption}"

    media_group_id_raw = raw_message.get("media_group_id")
    media_group_id = str(media_group_id_raw).strip() if media_group_id_raw is not None else None
    if media_group_id == "":
        media_group_id = None

    return IncomingChannelMessage(
        update_id=int(update_id),
        source_channel_id=chat_id,
        source_channel_username=channel_username,
        message_id=int(raw_message.get("message_id") or 0),
        date=_to_int_or_none(raw_message.get("date")),
        text=text,
        caption=caption,
        medias=medias,
        raw=update,
        media_group_id=media_group_id,
    )


def parse_telegram_private_message_update(update: dict) -> AdminInboundMessage | None:
    update_id = _to_int_or_none(update.get("update_id"))
    if update_id is None:
        return None

    callback_query = update.get("callback_query")
    if isinstance(callback_query, dict):
        raw_message = callback_query.get("message")
        if not isinstance(raw_message, dict):
            return None
        chat = raw_message.get("chat") or {}
        if (chat.get("type") or "").strip().lower() != "private":
            return None

        from_user = callback_query.get("from") or {}
        chat_id = normalize_channel_id(chat.get("id"))
        user_id = normalize_channel_id(from_user.get("id"))
        if chat_id is None or user_id is None:
            return None

        username = normalize_channel_username(from_user.get("username"))
        callback_query_id_raw = callback_query.get("id")
        callback_query_id = str(callback_query_id_raw).strip() if callback_query_id_raw is not None else None
        if not callback_query_id:
            return None
        callback_data_raw = callback_query.get("data")
        callback_data = callback_data_raw.strip() if isinstance(callback_data_raw, str) and callback_data_raw.strip() else None
        return AdminInboundMessage(
            update_id=update_id,
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            message_id=_to_int_or_none(raw_message.get("message_id")),
            text=None,
            callback_query_id=callback_query_id,
            callback_data=callback_data,
            callback_message_id=_to_int_or_none(raw_message.get("message_id")),
            raw=update,
        )

    raw_message = update.get("message")
    if raw_message is None:
        raw_message = update.get("edited_message")
    if not isinstance(raw_message, dict):
        return None

    chat = raw_message.get("chat") or {}
    if (chat.get("type") or "").strip().lower() != "private":
        return None

    chat_id = normalize_channel_id(chat.get("id"))
    from_user = raw_message.get("from") or {}
    user_id = normalize_channel_id(from_user.get("id"))
    if update_id is None or chat_id is None or user_id is None:
        return None

    username = normalize_channel_username(from_user.get("username"))
    text_raw = raw_message.get("text")
    text = text_raw.strip() if isinstance(text_raw, str) and text_raw.strip() else None
    return AdminInboundMessage(
        update_id=update_id,
        chat_id=chat_id,
        user_id=user_id,
        username=username,
        message_id=_to_int_or_none(raw_message.get("message_id")),
        text=text,
        callback_query_id=None,
        callback_data=None,
        callback_message_id=None,
        raw=update,
    )


def _extract_text(raw_text: object, raw_entities: object) -> str | None:
    if not isinstance(raw_text, str):
        return None
    text = raw_text
    entities = raw_entities if isinstance(raw_entities, list) else []

    text = _apply_text_link_entities(text, entities)
    text = _apply_blockquote_entities(text, entities)

    stripped = text.strip()
    return stripped if stripped else None


def _extract_reply_summary(reply: object) -> str | None:
    if not isinstance(reply, dict):
        return None

    raw = reply.get("text")
    if not isinstance(raw, str) or not raw.strip():
        raw = reply.get("caption")
        if not isinstance(raw, str) or not raw.strip():
            return None

    text = " ".join(raw.strip().splitlines())
    if len(text) > 220:
        text = text[:217].rstrip() + "..."
    return text


def _apply_text_link_entities(text: str, entities: list[object]) -> str:
    replacements: list[tuple[int, int, str]] = []
    for ent in entities:
        if not isinstance(ent, dict):
            continue
        if str(ent.get("type") or "").lower() != "text_link":
            continue
        url = ent.get("url")
        if not isinstance(url, str) or not url.strip():
            continue

        start_u16 = _to_int_or_none(ent.get("offset"))
        length_u16 = _to_int_or_none(ent.get("length"))
        if start_u16 is None or length_u16 is None or length_u16 <= 0:
            continue

        start, end = _utf16_slice_to_py_indices(text, start_u16, length_u16)
        if start is None or end is None or start >= end:
            continue

        label = text[start:end]
        safe_url = url.strip()
        replacement = f"{label} ({safe_url})"
        replacements.append((start, end, replacement))

    if not replacements:
        return text

    replacements.sort(key=lambda item: item[0], reverse=True)
    out = text
    for start, end, value in replacements:
        out = out[:start] + value + out[end:]
    return out


def _apply_blockquote_entities(text: str, entities: list[object]) -> str:
    blocks: list[tuple[int, int]] = []
    for ent in entities:
        if not isinstance(ent, dict):
            continue
        ent_type = str(ent.get("type") or "").lower()
        if ent_type not in {"blockquote", "expandable_blockquote"}:
            continue

        start_u16 = _to_int_or_none(ent.get("offset"))
        length_u16 = _to_int_or_none(ent.get("length"))
        if start_u16 is None or length_u16 is None or length_u16 <= 0:
            continue

        start, end = _utf16_slice_to_py_indices(text, start_u16, length_u16)
        if start is None or end is None or start >= end:
            continue
        blocks.append((start, end))

    if not blocks:
        return text

    blocks.sort(key=lambda item: item[0])
    merged: list[tuple[int, int]] = []
    for start, end in blocks:
        if not merged or start > merged[-1][1]:
            merged.append((start, end))
            continue
        merged[-1] = (merged[-1][0], max(merged[-1][1], end))

    chunks: list[str] = []
    cursor = 0
    for start, end in merged:
        if cursor < start:
            chunks.append(text[cursor:start])

        block = text[start:end]
        lines = block.splitlines() or [block]
        quoted = "\n".join(("🖊 " + line) if line else "🖊" for line in lines)
        chunks.append(quoted)
        cursor = end

    if cursor < len(text):
        chunks.append(text[cursor:])

    return "".join(chunks)


def _utf16_slice_to_py_indices(text: str, start_u16: int, length_u16: int) -> tuple[int | None, int | None]:
    if start_u16 < 0 or length_u16 < 0:
        return (None, None)

    end_u16 = start_u16 + length_u16
    current_u16 = 0
    start_idx: int | None = None
    end_idx: int | None = None

    for idx, ch in enumerate(text):
        if start_idx is None and current_u16 == start_u16:
            start_idx = idx
        if end_idx is None and current_u16 == end_u16:
            end_idx = idx

        current_u16 += 2 if ord(ch) > 0xFFFF else 1

        if start_idx is None and current_u16 > start_u16:
            start_idx = idx
        if end_idx is None and current_u16 >= end_u16:
            end_idx = idx + 1

    if start_idx is None and start_u16 == current_u16:
        start_idx = len(text)
    if end_idx is None and end_u16 == current_u16:
        end_idx = len(text)

    return (start_idx, end_idx)


def _extract_poll_text(raw_poll: object) -> str | None:
    if not isinstance(raw_poll, dict):
        return None
    question = raw_poll.get("question")
    options = raw_poll.get("options")
    if not isinstance(question, str) or not question.strip():
        return None
    if not isinstance(options, list) or not options:
        return None

    lines = [f"📊 نظرسنجی: {question.strip()}"]
    for idx, option in enumerate(options, start=1):
        if not isinstance(option, dict):
            continue
        option_text = option.get("text")
        if not isinstance(option_text, str) or not option_text.strip():
            continue
        lines.append(f"{idx}. {option_text.strip()}")
    if len(lines) <= 1:
        return None

    poll_type = str(raw_poll.get("type") or "").strip().lower()
    if poll_type == "quiz":
        lines.append("نوع: کوییز")
    elif poll_type:
        lines.append("نوع: نظرسنجی")
    if bool(raw_poll.get("allows_multiple_answers")):
        lines.append("چندگزینه‌ای: بله")

    return "\n".join(lines)


def _to_int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except Exception:
        return None
