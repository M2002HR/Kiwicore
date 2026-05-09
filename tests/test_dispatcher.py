from __future__ import annotations

import asyncio
from pathlib import Path

from kiwi.dispatcher import BaleDispatcher
from kiwi.types import OutputMessageKind, ScriptOutputMessage


class FakeBaleClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    async def send_message(self, chat_id: str, text: str):
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

    assert client.calls[0] == ("text", "-200", "hello")
    assert client.calls[1] == ("photo", "-200", "cap")


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
