from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from kiwi.errors import GuardExecutionError
from kiwi.types import ChannelRoute


class GuardRunner:
    def __init__(self, gaurd_scripts_dir: str, timeout_sec: int) -> None:
        self.gaurd_scripts_dir = Path(gaurd_scripts_dir)
        self.timeout_sec = timeout_sec
        self.last_reason: str | None = None

    async def run(
        self,
        route: ChannelRoute,
        *,
        payload_path: Path,
        input_dir: Path,
        output_dir: Path,
    ) -> bool:
        self.last_reason = None
        script_path = self.gaurd_scripts_dir / route.gaurd_script
        if not script_path.exists():
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
            raise GuardExecutionError(f"Guard script timeout after {self.timeout_sec}s: {script_path.name}")

        stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            raise GuardExecutionError(
                f"Guard script failed (code={proc.returncode}): {script_path.name}\n"
                f"stderr={stderr or '<empty>'}\nstdout={stdout or '<empty>'}"
            )

        if not stdout:
            raise GuardExecutionError(f"Guard script returned empty output: {script_path.name}")

        token_raw = stdout.splitlines()[-1].strip()
        token = token_raw.lower()
        if ":" in token_raw:
            left, right = token_raw.split(":", 1)
            token = left.strip().lower()
            reason = right.strip()
            self.last_reason = reason or None
        if token in {"true", "1", "yes", "allow", "allowed"}:
            return True
        if token in {"false", "0", "no", "deny", "denied"}:
            return False

        # Guard scripts may emit extra text after decision token.
        compact = token.split()[0] if token else ""
        if compact in {"true", "1", "yes", "allow", "allowed"}:
            return True
        if compact in {"false", "0", "no", "deny", "denied"}:
            return False

        raise GuardExecutionError(
            f"Guard script output must be true/false (or 1/0), got: {stdout!r} in {script_path.name}"
        )
