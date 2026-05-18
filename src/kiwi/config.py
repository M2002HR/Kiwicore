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
    telegram_source_mode: str

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
    telethon_enabled: bool
    telethon_api_id: int | None
    telethon_api_hash: str
    telethon_session_path: str
    telethon_poll_batch_size: int
    telethon_proxy_url: str
    sync_queue_backend: str = "redis"
    redis_url: str = ""
    sync_worker_count: int = 4
    sync_route_max_inflight: int = 1
    sync_retry_base_sec: float = 2.0
    sync_lock_ttl_sec: int = 120
    sync_meta_flush_sec: float = 3.0
    sync_review_alert_target: str | None = None
    sync_ledger_db_path: str = "./app_data/sync_ledger.sqlite3"
    sync_ledger_dsn: str = ""
    admin_web_enabled: bool = True
    admin_web_host: str = "127.0.0.1"
    admin_web_port: int = 8787
    admin_web_session_ttl_sec: int = 28800
    admin_ws_port: int = 0
    admin_ws_public_url: str = ""
    admin_ws_tls_cert_path: str = ""
    admin_ws_tls_key_path: str = ""


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

        normalized_id = normalize_channel_id(source_channel_id)
        if normalized_id:
            for route in self.by_channel_id.get(normalized_id, []):
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
        telegram_source_mode=_str("TELEGRAM_SOURCE_MODE", "bot").strip().lower(),
        bale_bot_token=_str("BALE_BOT_TOKEN", "").strip(),
        bale_api_base_url=_str("BALE_API_BASE_URL", "https://tapi.bale.ai").strip(),
        bale_file_base_url=_str("BALE_FILE_BASE_URL", "https://tapi.bale.ai/file").strip(),
        http_trust_env=_bool("HTTP_TRUST_ENV", False),
        channels_config_path=_str("CHANNELS_CONFIG_PATH", "./config/channels.json").strip(),
        scripts_dir=_str("CHANNEL_SCRIPTS_DIR", _str("SCRIPTS_DIR", "./scripts/channel_scripts")).strip(),
        gaurd_scripts_dir=_str("GAURD_SCRIPTS_DIR", "./scripts/gaurd_scrpts").strip(),
        storage_dir=_str("STORAGE_DIR", "./app_data").strip(),
        state_path=_str("STATE_PATH", "./app_data/state.json").strip(),
        default_max_message_mb=max(1, _int("DEFAULT_MAX_MESSAGE_MB", 60)),
        script_timeout_sec=max(5, _int("SCRIPT_TIMEOUT_SEC", 120)),
        gaurd_script_timeout_sec=max(5, _int("GAURD_SCRIPT_TIMEOUT_SEC", 60)),
        poll_idle_sleep_sec=max(0.1, _float("POLL_IDLE_SLEEP_SEC", 1.0)),
        poll_error_sleep_sec=max(0.5, _float("POLL_ERROR_SLEEP_SEC", 5.0)),
        log_channel_target=_str("LOG_CHANNEL_TARGET", "").strip() or None,
        media_group_wait_sec=max(0.3, _float("MEDIA_GROUP_WAIT_SEC", 1.4)),
        admin_bot_enabled=_bool("ADMIN_BOT_ENABLED", True),
        admin_users_config_path=_str("ADMIN_USERS_CONFIG_PATH", "./config/admin_users.json").strip(),
        admin_sessions_path=_str("ADMIN_SESSIONS_PATH", "./app_data/admin_sessions.json").strip(),
        telethon_enabled=_bool("TELETHON_ENABLED", False),
        telethon_api_id=_int("TELETHON_API_ID", 0) or None,
        telethon_api_hash=_str("TELETHON_API_HASH", "").strip(),
        telethon_session_path=_str("TELETHON_SESSION_PATH", "./app_data/telethon.session").strip(),
        telethon_poll_batch_size=max(1, _int("TELETHON_POLL_BATCH_SIZE", 50)),
        telethon_proxy_url=_str("TELETHON_PROXY_URL", "").strip(),
        sync_queue_backend=_str("SYNC_QUEUE_BACKEND", "redis").strip().lower() or "redis",
        redis_url=_str("REDIS_URL", "").strip(),
        sync_worker_count=max(1, _int("SYNC_WORKER_COUNT", 4)),
        sync_route_max_inflight=max(1, _int("SYNC_ROUTE_MAX_INFLIGHT", 1)),
        sync_retry_base_sec=max(0.2, _float("SYNC_RETRY_BASE_SEC", 2.0)),
        sync_lock_ttl_sec=max(5, _int("SYNC_LOCK_TTL_SEC", 120)),
        sync_meta_flush_sec=max(0.5, _float("SYNC_META_FLUSH_SEC", 3.0)),
        sync_review_alert_target=_str("SYNC_REVIEW_ALERT_TARGET", "").strip() or None,
        sync_ledger_db_path=_str("SYNC_LEDGER_DB_PATH", "./app_data/sync_ledger.sqlite3").strip(),
        sync_ledger_dsn=_str("SYNC_LEDGER_DSN", "").strip(),
        admin_web_enabled=_bool("ADMIN_WEB_ENABLED", True),
        admin_web_host=_str("ADMIN_WEB_HOST", "127.0.0.1").strip() or "127.0.0.1",
        admin_web_port=max(1, min(65535, _int("ADMIN_WEB_PORT", 8787))),
        admin_web_session_ttl_sec=max(300, _int("ADMIN_WEB_SESSION_TTL_SEC", 28800)),
        admin_ws_port=max(0, min(65535, _int("ADMIN_WS_PORT", 0))),
        admin_ws_public_url=_str("ADMIN_WS_PUBLIC_URL", "").strip(),
        admin_ws_tls_cert_path=_str("ADMIN_WS_TLS_CERT_PATH", "").strip(),
        admin_ws_tls_key_path=_str("ADMIN_WS_TLS_KEY_PATH", "").strip(),
    )

    if settings.telegram_source_mode not in {"bot", "telethon", "hybrid"}:
        raise ValueError("TELEGRAM_SOURCE_MODE must be one of: bot, telethon, hybrid")

    if settings.admin_bot_enabled:
        needed = {"message", "edited_message", "callback_query"}
        merged: list[str] = []
        seen: set[str] = set()
        for item in settings.telegram_allowed_updates + list(needed):
            key = str(item).strip()
            if not key or key in seen:
                continue
            merged.append(key)
            seen.add(key)
        settings.telegram_allowed_updates = merged

    if settings.telegram_source_mode == "telethon":
        # Keep private admin updates only; channel updates come from Telethon.
        settings.telegram_allowed_updates = [u for u in settings.telegram_allowed_updates if u in {"message", "edited_message", "callback_query"}]

    if settings.telegram_source_mode in {"telethon", "hybrid"}:
        settings.telethon_enabled = True

    if not settings.telegram_bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN is required")
    if not settings.bale_bot_token:
        raise ValueError("BALE_BOT_TOKEN is required")
    if settings.telethon_enabled:
        if settings.telethon_api_id is None or settings.telethon_api_id <= 0:
            raise ValueError("TELETHON_API_ID is required when TELETHON_ENABLED=true")
        if not settings.telethon_api_hash:
            raise ValueError("TELETHON_API_HASH is required when TELETHON_ENABLED=true")

    Path(settings.storage_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.scripts_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.gaurd_scripts_dir).mkdir(parents=True, exist_ok=True)
    Path(settings.channels_config_path).parent.mkdir(parents=True, exist_ok=True)
    Path(settings.state_path).parent.mkdir(parents=True, exist_ok=True)
    Path(settings.admin_users_config_path).parent.mkdir(parents=True, exist_ok=True)
    Path(settings.admin_sessions_path).parent.mkdir(parents=True, exist_ok=True)
    Path(settings.telethon_session_path).parent.mkdir(parents=True, exist_ok=True)
    if settings.sync_ledger_dsn:
        settings.sync_ledger_db_path = settings.sync_ledger_dsn
    if not settings.sync_ledger_db_path.startswith("mysql://") and not settings.sync_ledger_db_path.startswith(
        "mysql+pymysql://"
    ):
        Path(settings.sync_ledger_db_path).parent.mkdir(parents=True, exist_ok=True)

    return settings


def _default_channel_script_name(route_obj: dict) -> str | None:
    if "channel_script" in route_obj or "script" in route_obj:
        raw_script = route_obj.get("channel_script") if "channel_script" in route_obj else route_obj.get("script")
        script = str(raw_script or "").strip()
        if not script:
            return None
        return safe_script_name(script)

    source_username = normalize_channel_username(route_obj.get("source_channel_username"))
    if source_username:
        return f"{source_username.lstrip('@')}.py"
    source_id = normalize_channel_id(route_obj.get("source_channel_id"))
    if source_id:
        return f"{source_id}.py"
    raise ValueError("Route must define source_channel_username")


def _default_gaurd_script_name(route_obj: dict) -> str | None:
    if "gaurd_script" in route_obj:
        script = str(route_obj.get("gaurd_script") or "").strip()
        if not script:
            return None
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

        raw_source_username = str(obj.get("source_channel_username") or "").strip()
        source_channel_id = normalize_channel_id(obj.get("source_channel_id"))
        source_channel_username: str | None = None
        if raw_source_username:
            if not raw_source_username.startswith("@"):
                lowered = raw_source_username.lower()
                if raw_source_username.lstrip("-").isdigit() and not lowered.startswith("https://t.me/") and not lowered.startswith(
                    "http://t.me/"
                ):
                    inline_source_id = normalize_channel_id(raw_source_username)
                    source_channel_id = inline_source_id
                else:
                    source_channel_username = normalize_channel_username(raw_source_username)
            else:
                source_channel_username = normalize_channel_username(raw_source_username)
        if not source_channel_username and not source_channel_id:
            raise ValueError(f"Route at index {idx} has no source channel id/username")

        destination_channel_username = normalize_channel_username(obj.get("destination_channel_username"))
        destination_channel_id = normalize_channel_id(obj.get("destination_channel_id"))
        if not destination_channel_username and not destination_channel_id:
            raise ValueError(f"Route at index {idx} has no destination channel id/username")

        channel_script = _default_channel_script_name(obj)
        gaurd_script = _default_gaurd_script_name(obj)

        max_message_mb_raw = obj.get("max_message_mb")
        max_message_mb: int | None
        if max_message_mb_raw is None or max_message_mb_raw == "":
            max_message_mb = None
        else:
            max_message_mb = max(1, int(max_message_mb_raw))

        raw_status = str(obj.get("status") or "").strip().lower()
        if raw_status not in {"deactive", "syncing", "synced"}:
            legacy_enabled = bool(obj.get("enabled", True))
            sync_obj_legacy = obj.get("sync") if isinstance(obj.get("sync"), dict) else {}
            legacy_sync_enabled = bool(sync_obj_legacy.get("enabled", False))
            legacy_sync_status = str(sync_obj_legacy.get("status", "")).strip().lower()
            legacy_sync_seeded = bool(sync_obj_legacy.get("seeded", True))
            if legacy_sync_enabled and (legacy_sync_status in {"syncing", "active"} or not legacy_sync_seeded):
                raw_status = "syncing"
            elif not legacy_enabled:
                raw_status = "deactive"
            else:
                raw_status = "synced"

        sync_obj = obj.get("sync") if isinstance(obj.get("sync"), dict) else {}
        backfill_count_raw = obj.get("backfill_count", sync_obj.get("backfill_count", 100))
        interval_sec_raw = obj.get("interval_sec", sync_obj.get("interval_sec", 1))
        batch_size_raw = obj.get("batch_size", sync_obj.get("batch_size", 1))
        retry_attempts_raw = obj.get("retry_attempts", sync_obj.get("retry_attempts", 2))

        route = ChannelRoute(
            name=str(obj.get("name") or f"route_{idx+1}"),
            status=raw_status,
            source_channel_id=source_channel_id,
            source_channel_username=source_channel_username,
            destination_channel_id=destination_channel_id,
            destination_channel_username=destination_channel_username,
            channel_script=channel_script,
            max_message_mb=max_message_mb,
            gaurd_script=gaurd_script,
            sync_backfill_count=max(0, int(backfill_count_raw)),
            sync_interval_sec=max(1, int(interval_sec_raw)),
            sync_batch_size=max(1, int(batch_size_raw)),
            sync_retry_attempts=max(0, int(retry_attempts_raw)),
        )
        routes.append(route)

        if route.source_channel_id:
            by_id.setdefault(route.source_channel_id, []).append(route)
        if route.source_channel_username:
            by_username.setdefault(route.source_channel_username, []).append(route)

    return RouteRegistry(routes=routes, by_channel_id=by_id, by_channel_username=by_username)
