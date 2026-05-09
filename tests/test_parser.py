from __future__ import annotations

from kiwi.platforms.parser import parse_telegram_channel_update


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
