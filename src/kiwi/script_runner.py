from __future__ import annotations

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

from kiwi.errors import ScriptExecutionError
from kiwi.types import ChannelRoute, OutputMessageKind, ScriptOutputMessage, ScriptRunResult

logger = logging.getLogger(__name__)


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
        script_name: str | None = None,
        stage_name: str = "script",
        trace_id: str | None = None,
    ) -> ScriptRunResult:
        selected_script = script_name or route.channel_script
        script_path = self.scripts_dir / selected_script
        if not script_path.exists():
            logger.error(
                "Script not found",
                extra={
                    "details": {
                        "trace_id": trace_id,
                        "stage": stage_name,
                        "route": route.name,
                        "script_name": selected_script,
                        "script_path": str(script_path),
                    }
                },
            )
            raise ScriptExecutionError(f"{stage_name} not found: {script_path}")

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
        start = time.monotonic()
        logger.info(
            "Script execution started",
            extra={
                "details": {
                    "trace_id": trace_id,
                    "stage": stage_name,
                    "route": route.name,
                    "script_name": selected_script,
                    "script_path": str(script_path),
                    "payload_path": str(payload_path),
                    "input_dir": str(input_dir),
                    "output_dir": str(output_dir),
                }
            },
        )
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
            logger.error(
                "Script execution timeout",
                extra={
                    "details": {
                        "trace_id": trace_id,
                        "stage": stage_name,
                        "route": route.name,
                        "script_name": selected_script,
                        "timeout_sec": self.timeout_sec,
                        "duration_ms": round((time.monotonic() - start) * 1000.0, 2),
                    }
                },
            )
            raise ScriptExecutionError(f"{stage_name} timeout after {self.timeout_sec}s: {script_path.name}")

        stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
        duration_ms = round((time.monotonic() - start) * 1000.0, 2)

        if proc.returncode != 0:
            logger.error(
                "Script execution failed",
                extra={
                    "details": {
                        "trace_id": trace_id,
                        "stage": stage_name,
                        "route": route.name,
                        "script_name": selected_script,
                        "return_code": proc.returncode,
                        "duration_ms": duration_ms,
                        "stdout_chars": len(stdout),
                        "stderr_chars": len(stderr),
                        "stdout_preview": _preview_text(stdout),
                        "stderr_preview": _preview_text(stderr),
                    }
                },
            )
            raise ScriptExecutionError(
                f"{stage_name} failed (code={proc.returncode}): {script_path.name}\n"
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

        logger.info(
            "Script execution completed",
            extra={
                "details": {
                    "trace_id": trace_id,
                    "stage": stage_name,
                    "route": route.name,
                    "script_name": selected_script,
                    "return_code": proc.returncode,
                    "duration_ms": duration_ms,
                    "stdout_chars": len(stdout),
                    "stderr_chars": len(stderr),
                    "output_messages_count": len(messages),
                }
            },
        )
        return ScriptRunResult(messages=messages, stdout=stdout, stderr=stderr)


def _preview_text(value: str, *, limit: int = 300) -> str:
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."


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
