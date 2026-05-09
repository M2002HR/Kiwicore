from __future__ import annotations

import logging
import os
import shlex
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from dotenv import load_dotenv
from watchfiles import Change, DefaultFilter, watch

from kiwi.logging_setup import configure_logging

logger = logging.getLogger(__name__)

DEFAULT_WATCH_PATHS = ("src", "scripts", "config", ".env", "pyproject.toml", "requirements.txt")
DEFAULT_IGNORE_DIRS = (".git", ".venv", ".pytest_cache", "__pycache__", "app_data", "app_tmp", "data")


@dataclass(slots=True)
class DevConfig:
    root_dir: Path
    command: list[str]
    watch_paths: list[Path]
    ignore_dirs: set[str]
    debounce_ms: int
    poll_delay_ms: int
    force_polling: bool | None
    term_timeout_sec: float
    rust_timeout_ms: int


class DevFilter(DefaultFilter):
    def __init__(self, ignore_dirs: set[str]) -> None:
        super().__init__()
        self.ignore_dirs = ignore_dirs

    def __call__(self, change: Change, path: str) -> bool:
        if not super().__call__(change, path):
            return False
        parts = set(Path(path).parts)
        return not bool(parts & self.ignore_dirs)


def _csv_items(value: str | None, default: Iterable[str]) -> list[str]:
    if not value:
        return list(default)
    return [item.strip() for item in value.split(",") if item.strip()]


def _bool_env(value: str | None) -> bool | None:
    if value is None or value == "":
        return None
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_watch_paths(root_dir: Path, raw_paths: Iterable[str]) -> list[Path]:
    paths: list[Path] = []
    for raw in raw_paths:
        candidate = (root_dir / raw).resolve()
        if candidate.exists():
            paths.append(candidate)
    if not paths:
        raise ValueError("No valid watch paths found")
    return paths


def load_dev_config(env_file: str = ".env") -> DevConfig:
    root_dir = Path.cwd()
    load_dotenv(root_dir / env_file, override=True)

    raw_command = os.getenv("DEV_WATCH_COMMAND")
    command = shlex.split(raw_command) if raw_command else [sys.executable, "-m", "kiwi.main"]
    watch_paths = _resolve_watch_paths(root_dir, _csv_items(os.getenv("DEV_WATCH_PATHS"), DEFAULT_WATCH_PATHS))
    ignore_dirs = set(_csv_items(os.getenv("DEV_WATCH_IGNORE_DIRS"), DEFAULT_IGNORE_DIRS))

    return DevConfig(
        root_dir=root_dir,
        command=command,
        watch_paths=watch_paths,
        ignore_dirs=ignore_dirs,
        debounce_ms=int(os.getenv("DEV_WATCH_DEBOUNCE_MS", "700")),
        poll_delay_ms=int(os.getenv("DEV_WATCH_POLL_DELAY_MS", "300")),
        force_polling=_bool_env(os.getenv("DEV_WATCH_FORCE_POLLING")),
        term_timeout_sec=float(os.getenv("DEV_WATCH_TERM_TIMEOUT_SEC", "10")),
        rust_timeout_ms=int(os.getenv("DEV_WATCH_RUST_TIMEOUT_MS", "1000")),
    )


def _format_changes(changes: set[tuple[Change, str]]) -> str:
    paths = sorted({Path(path).name for _, path in changes})
    return ", ".join(paths[:6]) + (" ..." if len(paths) > 6 else "")


def _start_process(config: DevConfig) -> subprocess.Popen[bytes]:
    logger.info("Starting process: %s", " ".join(config.command))
    return subprocess.Popen(
        config.command,
        cwd=config.root_dir,
        env=os.environ.copy(),
        start_new_session=True,
    )


def _stop_process(process: subprocess.Popen[bytes], timeout_sec: float) -> None:
    if process.poll() is not None:
        return

    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    try:
        process.wait(timeout=timeout_sec)
        return
    except subprocess.TimeoutExpired:
        logger.warning("Process did not stop after %.1fs, force killing", timeout_sec)

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    process.wait(timeout=timeout_sec)


def main() -> None:
    configure_logging(os.getenv("LOG_LEVEL", "INFO"), os.getenv("LOG_FORMAT", "json"))
    config = load_dev_config()
    logger.info("Watching paths: %s", ", ".join(str(path.relative_to(config.root_dir)) for path in config.watch_paths))

    stop_requested = False

    def _request_stop(*_: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _request_stop)
        except ValueError:
            pass

    child = _start_process(config)

    watcher = watch(
        *config.watch_paths,
        watch_filter=DevFilter(config.ignore_dirs),
        debounce=config.debounce_ms,
        rust_timeout=config.rust_timeout_ms,
        yield_on_timeout=True,
        force_polling=config.force_polling,
        poll_delay_ms=config.poll_delay_ms,
        ignore_permission_denied=True,
    )

    try:
        for changes in watcher:
            if stop_requested:
                break

            if child.poll() is not None:
                logger.warning("Child exited with code %s, restarting", child.returncode)
                child = _start_process(config)
                continue

            if not changes:
                continue

            logger.info("Change detected: %s", _format_changes(changes))
            _stop_process(child, config.term_timeout_sec)
            child = _start_process(config)
    finally:
        _stop_process(child, config.term_timeout_sec)


if __name__ == "__main__":
    main()
