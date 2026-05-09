from __future__ import annotations

from pathlib import Path

from kiwi.types import OutputMessageKind, ScriptOutputMessage


class BaleDispatcher:
    def __init__(self, bale_client) -> None:
        self.bale_client = bale_client

    async def dispatch(
        self,
        destination_target: str,
        messages: list[ScriptOutputMessage],
        *,
        output_dir: Path,
        input_dir: Path,
    ) -> None:
        for message in messages:
            await self._send_one(destination_target, message, output_dir=output_dir, input_dir=input_dir)

    async def _send_one(
        self,
        destination_target: str,
        message: ScriptOutputMessage,
        *,
        output_dir: Path,
        input_dir: Path,
    ) -> None:
        if message.type == OutputMessageKind.TEXT:
            await self.bale_client.send_message(destination_target, message.text or "")
            return

        path = self._resolve_path(message.path or "", output_dir=output_dir, input_dir=input_dir)

        if message.type == OutputMessageKind.PHOTO:
            await self.bale_client.send_photo(destination_target, path, caption=message.caption)
            return
        if message.type == OutputMessageKind.VIDEO:
            await self.bale_client.send_video(destination_target, path, caption=message.caption)
            return
        if message.type == OutputMessageKind.VOICE:
            await self.bale_client.send_voice(destination_target, path, caption=message.caption)
            return
        if message.type == OutputMessageKind.AUDIO:
            await self.bale_client.send_audio(destination_target, path, caption=message.caption)
            return
        if message.type == OutputMessageKind.DOCUMENT:
            await self.bale_client.send_document(destination_target, path, caption=message.caption)
            return
        if message.type == OutputMessageKind.ANIMATION:
            await self.bale_client.send_animation(destination_target, path, caption=message.caption)
            return
        if message.type == OutputMessageKind.STICKER:
            # Global policy: stickers are blocked and never sent.
            return
        if message.type == OutputMessageKind.VIDEO_NOTE:
            await self.bale_client.send_video_note(destination_target, path)
            return

        raise ValueError(f"Unsupported output message type: {message.type}")

    @staticmethod
    def _resolve_path(value: str, *, output_dir: Path, input_dir: Path) -> Path:
        candidate = Path(value)
        if candidate.is_absolute():
            resolved = candidate
        else:
            out_path = output_dir / candidate
            if out_path.exists():
                resolved = out_path
            else:
                resolved = input_dir / candidate

        if not resolved.exists():
            raise FileNotFoundError(str(resolved))
        return resolved
