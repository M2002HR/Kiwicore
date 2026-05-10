from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from kiwi.types import ChannelRoute
from kiwi.utils import normalize_channel_id, normalize_channel_username, safe_script_name


@dataclass(slots=True)
class Settings:
    app_env: str
    log_level: str
    log_format: str
    tz: str

    telegram_bot_token: str
    telegram_api_base_url: str
    telegram_file_base_url: str
    telegram_poll_timeout_sec: int
    telegram_allowed_updates: list[str]

    bale_bot_token: str
    bale_api_base_url: str
    bale_file_base_url: str
    http_trust_env: bool

    channels_config_path: str
    scripts_dir: str
    gaurd_scripts_dir: str
    storage_dir: str
    state_path: str
    default_max_message_mb: int
    script_timeout_sec: int
    gaurd_script_timeout_sec: int
    poll_idle_sleep_sec: float
    poll_error_sleep_sec: float
    log_channel_target: str | None
    media_group_wait_sec: float
    admin_bot_enabled: bool
    admin_users_config_path: str
    admin_sessions_path: str


@dataclass(slots=True)
class RouteRegistry:
    routes: list[ChannelRoute]
    by_channel_id: dict[str, list[ChannelRoute]]
    by_channel_username: dict[str, list[ChannelRoute]]

    def match(self, source_channel_id: str, source_channel_username: str | None) -> ChannelRoute | None:
        matches = self.match_all(source_channel_id, source_channel_username)
        return matches[0] if matches else None

    def match_all(self, source_channel_id: str, source_channel_username: str | None) -> list[ChannelRoute]:
        out: list[ChannelRoute] = []
        seen: set[int] = set()

        if source_channel_username:
            for route in self.by_channel_username.get(source_channel_username, []):
                key = id(route)
                if key in seen:
                    continue
                seen.add(key)
                out.append(route)

        for route in self.by_channel_id.get(source_channel_id, []):
            key = id(route)
            if key in seen:
                continue
            seen.add(key)
            out.append(route)

        return out


def _str(name: str, default: str) -> str:
    value = os.getenv(name)
    return default if value is None else value


def _int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


def _float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return float(value)


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _list(name: str, default: list[str]) -> list[str]:
    value = os.getenv(name)
    if not value:
        return default
    value = value.strip()
    if value.startswith("["):
        parsed = json.loads(value)
        return [str(item) for item in parsed]
    return [item.strip() for item in value.split(",") if item.strip()]


def load_settings(env_file: str = ".env") -> Settings:
    load_dotenv(env_file, override=True)
    settings = Settings(
        app_env=_str("APP_ENV", "development"),
        log_level=_str("LOG_LEVEL", "INFO"),
        log_format=_str("LOG_FORMAT", "json"),
        tz=_str("TZ", "Asia/Tehran"),
        telegram_bot_token=_str("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_api_base_url=_str("TELEGRAM_API_BASE_URL", "https://api.telegram.org").strip(),
        telegram_file_base_url=_str("TELEGRAM_FILE_BASE_URL", "https://api.telegram.org/file").strip(),
        telegram_poll_timeout_sec=max(1, _int("TELEGRAM_POLL_TIMEOUT_SEC", 30)),
        telegram_allowed_updates=_list(
            "TELEGRAM_ALLOWED_UPDATES",
            ["channel_post", "edited_channel_post", "message", "edited_message"],
        ),
        bale_bot_token=_str("BALE_BOT_TOKEN", "").strip(),
        bale_api_base_url=_str("BALE_API_BASE_URL", "https://tapi.bale.ai").strip(),
        bale_file_base_url=_str("BALE_FILE_BASE_URL", "https://tapi.bale.ai/file").strip(),
        http_trust_env=_bool("HTTP_TRUST_ENV", False),
        channels_config_path=_str("CHANNELS_CONFIG_PATH", "./config/channels.json").strip(),
        scripts_dir=_str("SCRIPTS_DIR", "./scripts/channel_scripts").strip(),
        gaurd_scripts_dir=_str("GAURD_SCRIPTS_DIR", "./scripts/gaurd_scrpts").strip(),
        storage_dir=_str("STORAGE_DIR", "./app_data").strip(),
        state_path=_str("STATE_PATH", "./app_data/state.json").strip(),
        default_max_message_mb=max(1, _int("DEFAULT_MAX_MESSAGE_MB", 50)),
        script_timeout_sec=max(5, _int("SCRIPT_TIMEOUT_SEC", 120)),
        gaurd_script_timeout_sec=max(5, _int("GAURD_SCRIPT_TIMEOUT_SEC", 60)),
        poll_idle_sleep_sec=max(0.1, _float("POLL_IDLE_SLEEP_SEC", 1.0)),
        poll_error_sleep_sec=max(0.5, _float("POLL_ERROR_SLEEP_SEC", 5.0)),
        log_channel_target=_str("LOG_CHANNEL_TARGET", "").strip() or None,
        media_group_wait_sec=max(0.3, _float("MEDIA_GROUP_WAIT_SEC", 1.4)),
        admin_bot_enabled=_bool("ADMIN_BOT_ENABLED", True),
        admin_users_config_path=_str("ADMIN_USERS_CONFIG_PATH", "./config/admin_users.json").strip(),
        admin_sessions_path=_str("ADMIN_SESSIONS_PATH", "./app_data/admin_sessions.json").strip(),
    )

    if settings.admin_bot_enabled:
        needed = {"message", "edited_message"}
        merged: list[str] = []
        seen: set[str] = set()
        for item in settings.telegram_allowed_updates + list(needed):
            key = str(item).strip()
            if not key or key in seen:
                continue
            merged.append(key)
            seen.add(key)
        settings.telegram_allowed_updates = merged

    if not settings.telegram_bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN is required")
    if not settings.bale_bot_token:
        raise ValueError("BALE_BOT_TOKEN is required")

    Path(settings.storage_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.scripts_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.gaurd_scripts_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.channels_config_path).parent.mkdir(parents=True, exist_ok=True)
    Path(settings.state_path).parent.mkdir(parents=True, exist_ok=True)
    Path(settings.admin_users_config_path).parent.mkdir(parents=True, exist_ok=True)
    Path(settings.admin_sessions_path).parent.mkdir(parents=True, exist_ok=True)

    return settings


def _default_script_name(route_obj: dict) -> str:
    script = str(route_obj.get("script") or "").strip()
    if script:
        return safe_script_name(script)

    source_username = normalize_channel_username(route_obj.get("source_channel_username"))
    if source_username:
        return f"{source_username.lstrip('@')}.py"

    source_id = normalize_channel_id(route_obj.get("source_channel_id"))
    if source_id:
        return f"{source_id}.py"

    raise ValueError("Route must define either source_channel_id or source_channel_username")


def _default_gaurd_script_name(route_obj: dict) -> str:
    script = str(route_obj.get("gaurd_script") or "").strip()
    if script:
        return safe_script_name(script)
    return "default_guard.py"


def load_routes(config_path: str) -> RouteRegistry:
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Channels config not found: {config_path}")

    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("Channels config must be a JSON list")

    routes: list[ChannelRoute] = []
    by_id: dict[str, list[ChannelRoute]] = {}
    by_username: dict[str, list[ChannelRoute]] = {}

    for idx, obj in enumerate(raw):
        if not isinstance(obj, dict):
            raise ValueError(f"Route at index {idx} must be an object")

        source_channel_id = normalize_channel_id(obj.get("source_channel_id"))
        source_channel_username = normalize_channel_username(obj.get("source_channel_username"))

        if not source_channel_id and not source_channel_username:
            raise ValueError(f"Route at index {idx} has no source channel id/username")

        destination_channel_username = normalize_channel_username(obj.get("destination_channel_username"))
        destination_channel_id = normalize_channel_id(obj.get("destination_channel_id"))
        if not destination_channel_username and not destination_channel_id:
            raise ValueError(f"Route at index {idx} has no destination channel id/username")

        script = _default_script_name(obj)
        gaurd_script = _default_gaurd_script_name(obj)

        max_message_mb_raw = obj.get("max_message_mb")
        max_message_mb: int | None
        if max_message_mb_raw is None or max_message_mb_raw == "":
            max_message_mb = None
        else:
            max_message_mb = max(1, int(max_message_mb_raw))

        route = ChannelRoute(
            name=str(obj.get("name") or f"route_{idx+1}"),
            enabled=bool(obj.get("enabled", True)),
            source_channel_id=source_channel_id,
            source_channel_username=source_channel_username,
            destination_channel_id=destination_channel_id,
            destination_channel_username=destination_channel_username,
            script=script,
            max_message_mb=max_message_mb,
            gaurd_script=gaurd_script,
        )
        routes.append(route)

        if not route.enabled:
            continue

        if route.source_channel_id:
            by_id.setdefault(route.source_channel_id, []).append(route)

        if route.source_channel_username:
            by_username.setdefault(route.source_channel_username, []).append(route)

    return RouteRegistry(routes=routes, by_channel_id=by_id, by_channel_username=by_username)
