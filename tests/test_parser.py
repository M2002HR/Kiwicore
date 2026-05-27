from __future__ import annotations

from kiwi.platforms.parser import parse_telegram_channel_update, parse_telegram_private_message_update


def test_parse_channel_text_message() -> None:
    update = {
        "update_id": 10,
        "channel_post": {
            "message_id": 5,
            "date": 1710000000,
            "chat": {"id": -100123, "type": "channel", "username": "MyChan"},
            "text": "hello",
        },
    }

    parsed = parse_telegram_channel_update(update)
    assert parsed is not None
    assert parsed.update_id == 10
    assert parsed.source_channel_id == "-100123"
    assert parsed.source_channel_username == "@mychan"
    assert parsed.text == "hello"
    assert parsed.medias == []


def test_parse_channel_media_message() -> None:
    update = {
        "update_id": 11,
        "channel_post": {
            "message_id": 6,
            "chat": {"id": -100123, "type": "channel"},
            "caption": "cap",
            "document": {
                "file_id": "abc",
                "file_size": 123,
                "file_name": "a.pdf",
                "mime_type": "application/pdf",
            },
        },
    }

    parsed = parse_telegram_channel_update(update)
    assert parsed is not None
    assert parsed.caption == "cap"
    assert len(parsed.medias) == 1
    assert parsed.medias[0].kind.value == "document"
    assert parsed.medias[0].file_id == "abc"


def test_parse_channel_sticker_message() -> None:
    update = {
        "update_id": 13,
        "channel_post": {
            "message_id": 7,
            "chat": {"id": -100555, "type": "channel"},
            "sticker": {
                "file_id": "st1",
                "file_size": 456,
            },
        },
    }
    parsed = parse_telegram_channel_update(update)
    assert parsed is not None
    assert len(parsed.medias) == 1
    assert parsed.medias[0].kind.value == "sticker"
    assert parsed.medias[0].file_id == "st1"


def test_parse_channel_audio_metadata() -> None:
    update = {
        "update_id": 15,
        "channel_post": {
            "message_id": 10,
            "chat": {"id": -100123, "type": "channel"},
            "audio": {
                "file_id": "aud1",
                "file_size": 321,
                "file_name": "track.mp3",
                "mime_type": "audio/mpeg",
                "duration": 210,
                "title": "Song Name",
                "performer": "Artist Name",
            },
        },
    }
    parsed = parse_telegram_channel_update(update)
    assert parsed is not None
    assert len(parsed.medias) == 1
    assert parsed.medias[0].kind.value == "audio"
    assert parsed.medias[0].title == "Song Name"
    assert parsed.medias[0].performer == "Artist Name"


def test_parse_unknown_file_like_kind_falls_back_to_document() -> None:
    update = {
        "update_id": 14,
        "channel_post": {
            "message_id": 9,
            "chat": {"id": -100888, "type": "channel"},
            "some_new_kind": {"file_id": "new1", "file_size": 999},
        },
    }
    parsed = parse_telegram_channel_update(update)
    assert parsed is not None
    assert len(parsed.medias) == 1
    assert parsed.medias[0].kind.value == "document"
    assert parsed.medias[0].file_id == "new1"


def test_skip_non_channel_update() -> None:
    update = {
        "update_id": 12,
        "message": {
            "message_id": 1,
            "chat": {"id": 1, "type": "private"},
            "text": "hi",
        },
    }
    assert parse_telegram_channel_update(update) is None


def test_parse_private_message_update() -> None:
    update = {
        "update_id": 1000,
        "message": {
            "message_id": 2,
            "chat": {"id": 555, "type": "private"},
            "from": {"id": 777, "username": "admin_root"},
            "text": "/help",
        },
    }
    parsed = parse_telegram_private_message_update(update)
    assert parsed is not None
    assert parsed.update_id == 1000
    assert parsed.chat_id == "555"
    assert parsed.user_id == "777"
    assert parsed.username == "@admin_root"
    assert parsed.message_id == 2
    assert parsed.text == "/help"
    assert parsed.callback_query_id is None


def test_parse_private_callback_query_update() -> None:
    update = {
        "update_id": 1001,
        "callback_query": {
            "id": "cb-1",
            "from": {"id": 777, "username": "admin_root"},
            "message": {
                "message_id": 15,
                "chat": {"id": 555, "type": "private"},
            },
            "data": "txt:🔐 ورود",
        },
    }
    parsed = parse_telegram_private_message_update(update)
    assert parsed is not None
    assert parsed.update_id == 1001
    assert parsed.chat_id == "555"
    assert parsed.user_id == "777"
    assert parsed.message_id == 15
    assert parsed.callback_query_id == "cb-1"
    assert parsed.callback_data == "txt:🔐 ورود"


def test_parse_media_group_and_text_link_entity() -> None:
    update = {
        "update_id": 20,
        "channel_post": {
            "message_id": 50,
            "chat": {"id": -100999, "type": "channel"},
            "media_group_id": "group-1",
            "caption": "click here",
            "caption_entities": [
                {"type": "text_link", "offset": 0, "length": 5, "url": "https://example.com"},
            ],
            "photo": [{"file_id": "p1", "file_size": 12}],
        },
    }
    parsed = parse_telegram_channel_update(update)
    assert parsed is not None
    assert parsed.media_group_id == "group-1"
    assert parsed.caption == "click (https://example.com) here"


def test_parse_blockquote_entity_to_prefixed_text() -> None:
    update = {
        "update_id": 21,
        "channel_post": {
            "message_id": 51,
            "chat": {"id": -100999, "type": "channel"},
            "text": "line one\nline two\nrest",
            "entities": [
                {"type": "blockquote", "offset": 0, "length": 17},
            ],
        },
    }
    parsed = parse_telegram_channel_update(update)
    assert parsed is not None
    assert parsed.text == "🖊 line one\n🖊 line two\nrest"


def test_parse_reply_quote_is_readable() -> None:
    update = {
        "update_id": 22,
        "channel_post": {
            "message_id": 52,
            "chat": {"id": -100999, "type": "channel"},
            "caption": "جواب جدید",
            "reply_to_message": {
                "message_id": 40,
                "text": "متن پیام قبلی",
            },
            "photo": [{"file_id": "p2"}],
        },
    }
    parsed = parse_telegram_channel_update(update)
    assert parsed is not None
    assert parsed.caption is not None
    assert "🖊 نقل‌قول:" in parsed.caption
    assert "«متن پیام قبلی»" in parsed.caption


def test_parse_poll_message_to_structured_text() -> None:
    update = {
        "update_id": 23,
        "channel_post": {
            "message_id": 53,
            "chat": {"id": -100999, "type": "channel"},
            "poll": {
                "question": "کدام گزینه؟",
                "type": "regular",
                "allows_multiple_answers": True,
                "options": [
                    {"text": "گزینه اول"},
                    {"text": "گزینه دوم"},
                ],
            },
        },
    }
    parsed = parse_telegram_channel_update(update)
    assert parsed is not None
    assert parsed.text is not None
    assert "📊 نظرسنجی: کدام گزینه؟" in parsed.text
    assert "1. گزینه اول" in parsed.text
    assert "2. گزینه دوم" in parsed.text
