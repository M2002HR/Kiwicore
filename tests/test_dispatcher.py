from __future__ import annotations

import asyncio
from pathlib import Path

from kiwi.errors import PlatformApiError
from kiwi.dispatcher import BaleDispatcher
from kiwi.types import OutputMessageKind, ScriptOutputMessage


class FakeBaleClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []
        self.media_group_calls: list[tuple[str, int]] = []
        self.media_group_payloads: list[list[dict]] = []

    async def send_message(self, chat_id: str, text: str, reply_markup: dict | None = None):
        self.calls.append(("text", chat_id, text))
        return {"ok": True}

    async def send_photo(self, chat_id: str, photo_path: Path, caption: str | None = None):
        self.calls.append(("photo", chat_id, caption))
        return {"ok": True}

    async def send_video(self, chat_id: str, video_path: Path, caption: str | None = None):
        self.calls.append(("video", chat_id, caption))
        return {"ok": True}

    async def send_voice(self, chat_id: str, voice_path: Path, caption: str | None = None):
        self.calls.append(("voice", chat_id, caption))
        return {"ok": True}

    async def send_audio(self, chat_id: str, audio_path: Path, caption: str | None = None):
        self.calls.append(("audio", chat_id, caption))
        return {"ok": True}

    async def send_document(self, chat_id: str, document_path: Path, caption: str | None = None):
        self.calls.append(("document", chat_id, caption))
        return {"ok": True}

    async def send_animation(self, chat_id: str, animation_path: Path, caption: str | None = None):
        self.calls.append(("animation", chat_id, caption))
        return {"ok": True}

    async def send_sticker(self, chat_id: str, sticker_path: Path):
        self.calls.append(("sticker", chat_id, None))
        return {"ok": True}

    async def send_video_note(self, chat_id: str, video_note_path: Path):
        self.calls.append(("video_note", chat_id, None))
        return {"ok": True}

    async def send_media_group(self, chat_id: str, media_group: list[dict]):
        self.media_group_calls.append((chat_id, len(media_group)))
        self.media_group_payloads.append(list(media_group))
        return [{"ok": True}]


def test_dispatcher_sends_text_and_file(tmp_path: Path) -> None:
    client = FakeBaleClient()
    dispatcher = BaleDispatcher(client)

    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    (output_dir / "x.jpg").write_bytes(b"123")

    messages = [
        ScriptOutputMessage(type=OutputMessageKind.TEXT, text="hello"),
        ScriptOutputMessage(type=OutputMessageKind.PHOTO, path="x.jpg", caption="cap"),
    ]

    asyncio.run(dispatcher.dispatch("-200", messages, output_dir=output_dir, input_dir=input_dir))

    assert client.calls[0] == ("text", "-200", "hello\n-200")
    assert client.calls[1] == ("photo", "-200", "cap\n-200")


def test_dispatcher_blocks_sticker_and_sends_video_note(tmp_path: Path) -> None:
    client = FakeBaleClient()
    dispatcher = BaleDispatcher(client)

    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    (output_dir / "st.webp").write_bytes(b"1")
    (output_dir / "vn.mp4").write_bytes(b"2")

    messages = [
        ScriptOutputMessage(type=OutputMessageKind.STICKER, path="st.webp"),
        ScriptOutputMessage(type=OutputMessageKind.VIDEO_NOTE, path="vn.mp4"),
    ]

    asyncio.run(dispatcher.dispatch("@chan", messages, output_dir=output_dir, input_dir=input_dir))

    assert client.calls[0] == ("video_note", "@chan", None)


def test_dispatcher_sends_media_group_for_consecutive_items(tmp_path: Path) -> None:
    client = FakeBaleClient()
    dispatcher = BaleDispatcher(client)

    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    (output_dir / "a.jpg").write_bytes(b"1")
    (output_dir / "b.jpg").write_bytes(b"2")

    messages = [
        ScriptOutputMessage(type=OutputMessageKind.PHOTO, path="a.jpg", caption="cap"),
        ScriptOutputMessage(type=OutputMessageKind.PHOTO, path="b.jpg"),
    ]
    asyncio.run(dispatcher.dispatch("@chan", messages, output_dir=output_dir, input_dir=input_dir))

    assert client.media_group_calls == [("@chan", 2)]
    assert client.media_group_payloads[0][0].get("caption") == "cap\n@chan"
    assert client.media_group_payloads[0][1].get("caption") in {None, ""}
    assert client.calls == []


def test_dispatcher_keeps_captionless_media_captionless(tmp_path: Path) -> None:
    client = FakeBaleClient()
    dispatcher = BaleDispatcher(client)

    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    (output_dir / "a.jpg").write_bytes(b"1")

    messages = [
        ScriptOutputMessage(type=OutputMessageKind.PHOTO, path="a.jpg"),
    ]
    asyncio.run(dispatcher.dispatch("@chan", messages, output_dir=output_dir, input_dir=input_dir))

    assert client.calls == [("photo", "@chan", None)]


def test_dispatcher_promotes_non_first_caption_to_first_in_media_group(tmp_path: Path) -> None:
    client = FakeBaleClient()
    dispatcher = BaleDispatcher(client)

    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    (output_dir / "a.jpg").write_bytes(b"1")
    (output_dir / "b.jpg").write_bytes(b"2")

    messages = [
        ScriptOutputMessage(type=OutputMessageKind.PHOTO, path="a.jpg"),
        ScriptOutputMessage(type=OutputMessageKind.PHOTO, path="b.jpg", caption="cap on second"),
    ]
    asyncio.run(dispatcher.dispatch("@chan", messages, output_dir=output_dir, input_dir=input_dir))

    payload = client.media_group_payloads[0]
    assert payload[0].get("caption") == "cap on second\n@chan"
    assert payload[1].get("caption") in {None, ""}


def test_dispatcher_fallback_preserves_caption_when_first_album_item_fails(tmp_path: Path) -> None:
    class PartialFallbackClient(FakeBaleClient):
        def __init__(self) -> None:
            super().__init__()
            self.first_send = True

        async def send_media_group(self, chat_id: str, media_group: list[dict]):
            raise PlatformApiError("sendMediaGroup HTTP 400: bad request")

        async def send_photo(self, chat_id: str, photo_path: Path, caption: str | None = None):
            if self.first_send:
                self.first_send = False
                raise PlatformApiError("sendPhoto HTTP 500: failed to upload file bytes")
            self.calls.append(("photo", chat_id, caption))
            return {"ok": True}

    client = PartialFallbackClient()
    dispatcher = BaleDispatcher(client)

    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    (output_dir / "a.jpg").write_bytes(b"1")
    (output_dir / "b.jpg").write_bytes(b"2")

    messages = [
        ScriptOutputMessage(type=OutputMessageKind.PHOTO, path="a.jpg", caption="album caption"),
        ScriptOutputMessage(type=OutputMessageKind.PHOTO, path="b.jpg"),
    ]
    asyncio.run(dispatcher.dispatch("@chan", messages, output_dir=output_dir, input_dir=input_dir))

    assert ("text", "@chan", "album caption\n@chan") in client.calls


def test_dispatcher_does_not_fallback_to_single_send_on_transient_media_group_error(tmp_path: Path) -> None:
    class TransientGroupFailClient(FakeBaleClient):
        async def send_media_group(self, chat_id: str, media_group: list[dict]):
            # Simulate ambiguous transport error where album may already be delivered upstream.
            raise PlatformApiError("sendMediaGroup network error: ReadTimeout")

    client = TransientGroupFailClient()
    dispatcher = BaleDispatcher(client)

    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    (output_dir / "a.jpg").write_bytes(b"1")
    (output_dir / "b.jpg").write_bytes(b"2")

    messages = [
        ScriptOutputMessage(type=OutputMessageKind.PHOTO, path="a.jpg", caption="cap"),
        ScriptOutputMessage(type=OutputMessageKind.PHOTO, path="b.jpg"),
    ]

    try:
        asyncio.run(dispatcher.dispatch("@chan", messages, output_dir=output_dir, input_dir=input_dir))
        assert False, "expected PlatformApiError"
    except PlatformApiError:
        pass
    # No single-media fallback should occur on transient/ambiguous media-group error.
    assert client.calls == []


def test_dispatcher_groups_audio_albums(tmp_path: Path) -> None:
    client = FakeBaleClient()
    dispatcher = BaleDispatcher(client)

    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    (output_dir / "a.mp3").write_bytes(b"1")
    (output_dir / "b.mp3").write_bytes(b"2")

    messages = [
        ScriptOutputMessage(type=OutputMessageKind.AUDIO, path="a.mp3", caption="track list"),
        ScriptOutputMessage(type=OutputMessageKind.AUDIO, path="b.mp3"),
    ]
    asyncio.run(dispatcher.dispatch("@chan", messages, output_dir=output_dir, input_dir=input_dir))

    assert client.media_group_calls == [("@chan", 2)]
    assert client.calls == []


def test_dispatcher_does_not_mix_document_and_photo_groups(tmp_path: Path) -> None:
    client = FakeBaleClient()
    dispatcher = BaleDispatcher(client)

    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    (output_dir / "a.jpg").write_bytes(b"1")
    (output_dir / "b.pdf").write_bytes(b"2")
    (output_dir / "c.pdf").write_bytes(b"3")

    messages = [
        ScriptOutputMessage(type=OutputMessageKind.PHOTO, path="a.jpg", caption="cap"),
        ScriptOutputMessage(type=OutputMessageKind.DOCUMENT, path="b.pdf"),
        ScriptOutputMessage(type=OutputMessageKind.DOCUMENT, path="c.pdf"),
    ]
    asyncio.run(dispatcher.dispatch("@chan", messages, output_dir=output_dir, input_dir=input_dir))

    assert client.media_group_calls == [("@chan", 2)]
    assert client.calls[0] == ("photo", "@chan", "cap\n@chan")


def test_dispatcher_does_not_fallback_to_document_when_send_audio_fails(tmp_path: Path) -> None:
    class FailingAudioClient(FakeBaleClient):
        async def send_audio(self, chat_id: str, audio_path: Path, caption: str | None = None):
            raise PlatformApiError("sendAudio network error: ReadTimeout")

    client = FailingAudioClient()
    dispatcher = BaleDispatcher(client)

    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    (output_dir / "x.mp3").write_bytes(b"abc")

    messages = [
        ScriptOutputMessage(type=OutputMessageKind.AUDIO, path="x.mp3", caption="c"),
    ]
    try:
        asyncio.run(dispatcher.dispatch("@chan", messages, output_dir=output_dir, input_dir=input_dir))
        assert False, "expected PlatformApiError"
    except PlatformApiError:
        pass
    assert client.calls == []


def test_dispatcher_falls_back_to_text_on_http_413_when_enabled(tmp_path: Path) -> None:
    class OversizeVideoClient(FakeBaleClient):
        async def send_video(self, chat_id: str, video_path: Path, caption: str | None = None):
            raise PlatformApiError("sendVideo HTTP 413: Request Entity Too Large")

    client = OversizeVideoClient()
    dispatcher = BaleDispatcher(client)
    dispatcher.media_upload_fallback_mode = "text"

    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    (output_dir / "big.mp4").write_bytes(b"abc")

    messages = [
        ScriptOutputMessage(type=OutputMessageKind.VIDEO, path="big.mp4", caption="video cap"),
    ]
    asyncio.run(dispatcher.dispatch("@chan", messages, output_dir=output_dir, input_dir=input_dir))
    assert client.calls == [("text", "@chan", "video cap\n@chan")]


def test_dispatcher_never_falls_back_photo_to_document(tmp_path: Path) -> None:
    class FailingPhotoClient(FakeBaleClient):
        async def send_photo(self, chat_id: str, photo_path: Path, caption: str | None = None):
            raise PlatformApiError("sendPhoto HTTP 500: failed to upload file bytes")

    client = FailingPhotoClient()
    dispatcher = BaleDispatcher(client)
    dispatcher.media_upload_fallback_mode = "document"

    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    (output_dir / "x.jpg").write_bytes(b"abc")

    messages = [
        ScriptOutputMessage(type=OutputMessageKind.PHOTO, path="x.jpg", caption="cap"),
    ]

    try:
        asyncio.run(dispatcher.dispatch("@chan", messages, output_dir=output_dir, input_dir=input_dir))
        assert False, "expected PlatformApiError"
    except PlatformApiError:
        pass
    # Must not degrade photo delivery into document delivery.
    assert client.calls == []


def test_dispatcher_document_fallback_still_applies_for_video(tmp_path: Path) -> None:
    class FailingVideoClient(FakeBaleClient):
        async def send_video(self, chat_id: str, video_path: Path, caption: str | None = None):
            raise PlatformApiError("sendVideo HTTP 500: failed to upload file bytes")

    client = FailingVideoClient()
    dispatcher = BaleDispatcher(client)
    dispatcher.media_upload_fallback_mode = "document"

    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    (output_dir / "x.mp4").write_bytes(b"abc")

    messages = [
        ScriptOutputMessage(type=OutputMessageKind.VIDEO, path="x.mp4", caption="cap"),
    ]
    asyncio.run(dispatcher.dispatch("@chan", messages, output_dir=output_dir, input_dir=input_dir))
    assert client.calls == [("document", "@chan", "cap\n@chan")]


def test_dispatcher_skips_empty_text_message(tmp_path: Path) -> None:
    client = FakeBaleClient()
    dispatcher = BaleDispatcher(client)
    input_dir = tmp_path / "input"
    output_dir = tmp_path / "output"
    input_dir.mkdir()
    output_dir.mkdir()
    messages = [ScriptOutputMessage(type=OutputMessageKind.TEXT, text="   ")]
    asyncio.run(dispatcher.dispatch("@chan", messages, output_dir=output_dir, input_dir=input_dir))
    assert client.calls == []
