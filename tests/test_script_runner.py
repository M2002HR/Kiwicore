from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from kiwi.errors import ScriptExecutionError
from kiwi.script_runner import ScriptRunner
from kiwi.types import ChannelRoute


def test_script_runner_reads_stdout_json(tmp_path: Path) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    script = scripts_dir / "x.py"
    script.write_text(
        """
import json
print(json.dumps({"messages": [{"type": "text", "text": "ok"}]}))
""".strip(),
        encoding="utf-8",
    )

    runner = ScriptRunner(str(scripts_dir), timeout_sec=5)
    route = ChannelRoute("n", True, "-1", None, "-2", None, "x.py", None)

    payload_path = tmp_path / "payload.json"
    payload_path.write_text(json.dumps({"a": 1}), encoding="utf-8")
    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    input_dir.mkdir()
    output_dir.mkdir()

    result = asyncio.run(runner.run(route, payload_path=payload_path, input_dir=input_dir, output_dir=output_dir))
    assert len(result.messages) == 1
    assert result.messages[0].type.value == "text"
    assert result.messages[0].text == "ok"


def test_script_runner_invalid_output_raises(tmp_path: Path) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    script = scripts_dir / "bad.py"
    script.write_text("print('not-json')", encoding="utf-8")

    runner = ScriptRunner(str(scripts_dir), timeout_sec=5)
    route = ChannelRoute("n", True, "-1", None, "-2", None, "bad.py", None)

    payload_path = tmp_path / "payload.json"
    payload_path.write_text("{}", encoding="utf-8")
    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    input_dir.mkdir()
    output_dir.mkdir()

    with pytest.raises(ScriptExecutionError):
        asyncio.run(runner.run(route, payload_path=payload_path, input_dir=input_dir, output_dir=output_dir))


def test_script_runner_filters_sticker_output(tmp_path: Path) -> None:
    scripts_dir = tmp_path / "scripts"
    scripts_dir.mkdir()
    script = scripts_dir / "st.py"
    script.write_text(
        """
import json
print(json.dumps({"messages": [{"type": "sticker", "path": "a.webp"}, {"type": "text", "text": "ok"}]}))
""".strip(),
        encoding="utf-8",
    )

    runner = ScriptRunner(str(scripts_dir), timeout_sec=5)
    route = ChannelRoute("n", True, "-1", None, "-2", None, "st.py", None)
    payload_path = tmp_path / "payload.json"
    payload_path.write_text("{}", encoding="utf-8")
    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    input_dir.mkdir()
    output_dir.mkdir()

    result = asyncio.run(runner.run(route, payload_path=payload_path, input_dir=input_dir, output_dir=output_dir))
    assert len(result.messages) == 1
    assert result.messages[0].type.value == "text"
