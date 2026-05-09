from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

from kiwi.errors import ScriptExecutionError
from kiwi.types import ChannelRoute, OutputMessageKind, ScriptOutputMessage, ScriptRunResult


class ScriptRunner:
    def __init__(self, scripts_dir: str, timeout_sec: int) -> None:
        self.scripts_dir = Path(scripts_dir)
        self.timeout_sec = timeout_sec

    async def run(
        self,
        route: ChannelRoute,
        *,
        payload_path: Path,
        input_dir: Path,
        output_dir: Path,
    ) -> ScriptRunResult:
        script_path = self.scripts_dir / route.script
        if not script_path.exists():
            raise ScriptExecutionError(f"Script not found: {script_path}")

        cmd = [
            sys.executable,
            str(script_path),
            "--payload",
            str(payload_path),
            "--input-dir",
            str(input_dir),
            "--output-dir",
            str(output_dir),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_sec)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.communicate()
            raise ScriptExecutionError(f"Script timeout after {self.timeout_sec}s: {script_path.name}")

        stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            raise ScriptExecutionError(
                f"Script failed (code={proc.returncode}): {script_path.name}\n"
                f"stderr={stderr or '<empty>'}\nstdout={stdout or '<empty>'}"
            )

        payload = None
        if stdout:
            payload = _parse_output_json(stdout, source=f"stdout({script_path.name})")
        else:
            manifest = output_dir / "output.json"
            if manifest.exists():
                payload = _parse_output_json(manifest.read_text(encoding="utf-8"), source=str(manifest))

        messages: list[ScriptOutputMessage] = []
        if payload is not None:
            messages = _parse_messages(payload)

        return ScriptRunResult(messages=messages, stdout=stdout, stderr=stderr)


def _parse_output_json(text: str, source: str) -> dict:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ScriptExecutionError(f"Invalid JSON output in {source}: {exc}")
    if not isinstance(data, dict):
        raise ScriptExecutionError(f"Output in {source} must be a JSON object")
    return data


def _parse_messages(payload: dict) -> list[ScriptOutputMessage]:
    raw_messages = payload.get("messages", [])
    if raw_messages is None:
        raw_messages = []
    if not isinstance(raw_messages, list):
        raise ScriptExecutionError("Script output field 'messages' must be a list")

    result: list[ScriptOutputMessage] = []
    for idx, item in enumerate(raw_messages):
        if not isinstance(item, dict):
            raise ScriptExecutionError(f"messages[{idx}] must be an object")

        msg_type = str(item.get("type") or "").strip().lower()
        try:
            kind = OutputMessageKind(msg_type)
        except ValueError:
            raise ScriptExecutionError(f"messages[{idx}] has unsupported type: {msg_type}")

        # Global policy: stickers must never be sent to destination.
        if kind == OutputMessageKind.STICKER:
            continue

        text = item.get("text")
        path = item.get("path")
        caption = item.get("caption")

        if kind == OutputMessageKind.TEXT:
            if not isinstance(text, str) or not text.strip():
                raise ScriptExecutionError(f"messages[{idx}] type=text requires non-empty 'text'")
            result.append(ScriptOutputMessage(type=kind, text=text))
            continue

        if not isinstance(path, str) or not path.strip():
            raise ScriptExecutionError(f"messages[{idx}] type={kind.value} requires non-empty 'path'")

        result.append(
            ScriptOutputMessage(
                type=kind,
                path=path.strip(),
                caption=caption.strip() if isinstance(caption, str) and caption.strip() else None,
            )
        )

    return result
