from __future__ import annotations

import asyncio
import logging
import sys
import time
from pathlib import Path

from kiwi.errors import GuardExecutionError
from kiwi.types import ChannelRoute

logger = logging.getLogger(__name__)


class GuardRunner:
    def __init__(self, gaurd_scripts_dir: str, timeout_sec: int) -> None:
        self.gaurd_scripts_dir = Path(gaurd_scripts_dir)
        self.timeout_sec = timeout_sec
        self.last_reason: str | None = None
        self.last_stdout: str = ""
        self.last_stderr: str = ""
        self.last_token: str | None = None
        self.last_duration_ms: float | None = None

    async def run(
        self,
        route: ChannelRoute,
        *,
        payload_path: Path,
        input_dir: Path,
        output_dir: Path,
        trace_id: str | None = None,
    ) -> bool:
        self.last_reason = None
        self.last_stdout = ""
        self.last_stderr = ""
        self.last_token = None
        self.last_duration_ms = None
        if not route.gaurd_script:
            raise GuardExecutionError("Guard script name is empty")
        script_path = self.gaurd_scripts_dir / route.gaurd_script
        if not script_path.exists():
            logger.error(
                "Guard script not found",
                extra={
                    "details": {
                        "trace_id": trace_id,
                        "route": route.name,
                        "guard_script": route.gaurd_script,
                        "script_path": str(script_path),
                    }
                },
            )
            raise GuardExecutionError(f"Guard script not found: {script_path}")

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
        started = time.monotonic()
        logger.info(
            "Guard execution started",
            extra={
                "details": {
                    "trace_id": trace_id,
                    "route": route.name,
                    "guard_script": route.gaurd_script,
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
                "Guard execution timeout",
                extra={
                    "details": {
                        "trace_id": trace_id,
                        "route": route.name,
                        "guard_script": route.gaurd_script,
                        "timeout_sec": self.timeout_sec,
                        "duration_ms": round((time.monotonic() - started) * 1000.0, 2),
                    }
                },
            )
            raise GuardExecutionError(f"Guard script timeout after {self.timeout_sec}s: {script_path.name}")

        stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip()
        duration_ms = round((time.monotonic() - started) * 1000.0, 2)
        self.last_stdout = stdout
        self.last_stderr = stderr
        self.last_duration_ms = duration_ms

        if proc.returncode != 0:
            logger.error(
                "Guard execution failed",
                extra={
                    "details": {
                        "trace_id": trace_id,
                        "route": route.name,
                        "guard_script": route.gaurd_script,
                        "return_code": proc.returncode,
                        "duration_ms": duration_ms,
                        "stdout_preview": _preview_text(stdout),
                        "stderr_preview": _preview_text(stderr),
                    }
                },
            )
            raise GuardExecutionError(
                f"Guard script failed (code={proc.returncode}): {script_path.name}\n"
                f"stderr={stderr or '<empty>'}\nstdout={stdout or '<empty>'}"
            )

        if not stdout:
            logger.error(
                "Guard execution returned empty output",
                extra={
                    "details": {
                        "trace_id": trace_id,
                        "route": route.name,
                        "guard_script": route.gaurd_script,
                        "duration_ms": duration_ms,
                    }
                },
            )
            raise GuardExecutionError(f"Guard script returned empty output: {script_path.name}")

        token_raw = stdout.splitlines()[-1].strip()
        self.last_token = token_raw
        token = token_raw.lower()
        if ":" in token_raw:
            left, right = token_raw.split(":", 1)
            token = left.strip().lower()
            reason = right.strip()
            self.last_reason = reason or None
        if token in {"true", "1", "yes", "allow", "allowed"}:
            logger.info(
                "Guard execution completed",
                extra={
                    "details": {
                        "trace_id": trace_id,
                        "route": route.name,
                        "guard_script": route.gaurd_script,
                        "decision": "allow",
                        "duration_ms": duration_ms,
                    }
                },
            )
            return True
        if token in {"false", "0", "no", "deny", "denied"}:
            if not self.last_reason:
                self.last_reason = _extract_reason_from_stderr(stderr) or "guard_denied"
            logger.info(
                "Guard execution completed",
                extra={
                    "details": {
                        "trace_id": trace_id,
                        "route": route.name,
                        "guard_script": route.gaurd_script,
                        "decision": "deny",
                        "reason": self.last_reason,
                        "duration_ms": duration_ms,
                    }
                },
            )
            return False

        # Guard scripts may emit extra text after decision token.
        compact = token.split()[0] if token else ""
        if compact in {"true", "1", "yes", "allow", "allowed"}:
            logger.info(
                "Guard execution completed",
                extra={
                    "details": {
                        "trace_id": trace_id,
                        "route": route.name,
                        "guard_script": route.gaurd_script,
                        "decision": "allow",
                        "duration_ms": duration_ms,
                    }
                },
            )
            return True
        if compact in {"false", "0", "no", "deny", "denied"}:
            if not self.last_reason:
                self.last_reason = _extract_reason_from_stderr(stderr) or "guard_denied"
            logger.info(
                "Guard execution completed",
                extra={
                    "details": {
                        "trace_id": trace_id,
                        "route": route.name,
                        "guard_script": route.gaurd_script,
                        "decision": "deny",
                        "reason": self.last_reason,
                        "duration_ms": duration_ms,
                    }
                },
            )
            return False

        logger.error(
            "Guard execution returned invalid token",
            extra={
                "details": {
                    "trace_id": trace_id,
                    "route": route.name,
                    "guard_script": route.gaurd_script,
                    "duration_ms": duration_ms,
                    "stdout_preview": _preview_text(stdout),
                }
            },
        )
        raise GuardExecutionError(
            f"Guard script output must be true/false (or 1/0), got: {stdout!r} in {script_path.name}"
        )


def _extract_reason_from_stderr(stderr: str) -> str | None:
    line = stderr.strip().splitlines()[-1].strip() if stderr.strip() else ""
    if not line:
        return None
    return line[:300]


def _preview_text(value: str, *, limit: int = 300) -> str:
    compact = " ".join(value.split())
    if len(compact) <= limit:
        return compact
    return compact[: limit - 3] + "..."
