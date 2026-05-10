from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from kiwi.errors import GuardExecutionError
from kiwi.guard_runner import GuardRunner
from kiwi.types import ChannelRoute


def _route(gaurd_script: str = "g.py") -> ChannelRoute:
    return ChannelRoute("n", True, "-1", None, "-2", None, "x.py", None, gaurd_script)


def test_guard_runner_true(tmp_path: Path) -> None:
    guard_dir = tmp_path / "guards"
    guard_dir.mkdir()
    (guard_dir / "g.py").write_text("print('true')", encoding="utf-8")

    runner = GuardRunner(str(guard_dir), timeout_sec=5)
    payload = tmp_path / "payload.json"
    payload.write_text("{}", encoding="utf-8")
    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    input_dir.mkdir()
    output_dir.mkdir()

    result = asyncio.run(runner.run(_route(), payload_path=payload, input_dir=input_dir, output_dir=output_dir))
    assert result is True


def test_guard_runner_false(tmp_path: Path) -> None:
    guard_dir = tmp_path / "guards"
    guard_dir.mkdir()
    (guard_dir / "g.py").write_text("print('0')", encoding="utf-8")

    runner = GuardRunner(str(guard_dir), timeout_sec=5)
    payload = tmp_path / "payload.json"
    payload.write_text("{}", encoding="utf-8")
    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    input_dir.mkdir()
    output_dir.mkdir()

    result = asyncio.run(runner.run(_route(), payload_path=payload, input_dir=input_dir, output_dir=output_dir))
    assert result is False


def test_guard_runner_invalid_output(tmp_path: Path) -> None:
    guard_dir = tmp_path / "guards"
    guard_dir.mkdir()
    (guard_dir / "g.py").write_text("print('maybe')", encoding="utf-8")

    runner = GuardRunner(str(guard_dir), timeout_sec=5)
    payload = tmp_path / "payload.json"
    payload.write_text("{}", encoding="utf-8")
    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    input_dir.mkdir()
    output_dir.mkdir()

    with pytest.raises(GuardExecutionError):
        asyncio.run(runner.run(_route(), payload_path=payload, input_dir=input_dir, output_dir=output_dir))


def test_guard_runner_false_with_reason_token(tmp_path: Path) -> None:
    guard_dir = tmp_path / "guards"
    guard_dir.mkdir()
    (guard_dir / "g.py").write_text("print('false: obvious_advertisement')", encoding="utf-8")

    runner = GuardRunner(str(guard_dir), timeout_sec=5)
    payload = tmp_path / "payload.json"
    payload.write_text("{}", encoding="utf-8")
    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    input_dir.mkdir()
    output_dir.mkdir()

    result = asyncio.run(runner.run(_route(), payload_path=payload, input_dir=input_dir, output_dir=output_dir))
    assert result is False
    assert runner.last_reason == "obvious_advertisement"
