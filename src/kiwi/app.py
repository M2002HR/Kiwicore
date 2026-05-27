from __future__ import annotations

import os

from kiwi.admin_bot import AdminBotHandler
from kiwi.admin_store import AdminStore
from kiwi.config import load_routes, load_settings
from kiwi.guard_runner import GuardRunner
from kiwi.management_api import ManagementApi
from kiwi.platforms.client import BotApiClient
from kiwi.platforms.telethon_source import TelethonSourceClient
from kiwi.script_runner import ScriptRunner
from kiwi.service import KiwiService
from kiwi.state import StateStore
from kiwi.storage import StorageManager
from kiwi.sync_ledger import SyncLedger
from kiwi.sync_queue import build_sync_queue_backend


async def build_service(env_file: str = ".env") -> KiwiService:
    settings = load_settings(env_file)
    routes = load_routes(settings.channels_config_path)
    try:
        bale_timeout_sec = float(str(os.getenv("BALE_API_TIMEOUT_SEC", "90")).strip() or "90")
    except Exception:
        bale_timeout_sec = 90.0
    bale_timeout_sec = max(20.0, min(300.0, bale_timeout_sec))
    try:
        bale_upload_max_concurrency = int(str(os.getenv("BALE_UPLOAD_MAX_CONCURRENCY", "1")).strip() or "1")
    except Exception:
        bale_upload_max_concurrency = 1
    raw_bale_trust_env = str(os.getenv("BALE_HTTP_TRUST_ENV", "")).strip().lower()
    if raw_bale_trust_env in {"1", "true", "yes", "on"}:
        bale_trust_env = True
    elif raw_bale_trust_env in {"0", "false", "no", "off"}:
        bale_trust_env = False
    else:
        bale_trust_env = bool(settings.http_trust_env)

    telegram_client = BotApiClient(
        token=settings.telegram_bot_token,
        api_base_url=settings.telegram_api_base_url,
        file_base_url=settings.telegram_file_base_url,
        timeout_sec=float(settings.telegram_poll_timeout_sec + 15),
        trust_env=settings.http_trust_env,
    )
    bale_client = BotApiClient(
        token=settings.bale_bot_token,
        api_base_url=settings.bale_api_base_url,
        file_base_url=settings.bale_file_base_url,
        timeout_sec=bale_timeout_sec,
        upload_max_concurrency=max(0, bale_upload_max_concurrency),
        trust_env=bale_trust_env,
    )
    telethon_source_client = None
    if settings.telethon_enabled:
        telethon_source_client = TelethonSourceClient(
            api_id=int(settings.telethon_api_id or 0),
            api_hash=settings.telethon_api_hash,
            session_path=settings.telethon_session_path,
            poll_batch_size=settings.telethon_poll_batch_size,
            proxy_url=settings.telethon_proxy_url,
        )
    sync_ledger = SyncLedger(settings.sync_ledger_db_path)
    sync_queue = await build_sync_queue_backend(
        backend=settings.sync_queue_backend,
        redis_url=settings.redis_url,
    )

    service = KiwiService(
        settings=settings,
        routes=routes,
        telegram_client=telegram_client,
        bale_client=bale_client,
        source_client=telethon_source_client,
        storage=StorageManager(settings.storage_dir),
        guard_runner=GuardRunner(settings.gaurd_scripts_dir, settings.gaurd_script_timeout_sec),
        script_runner=ScriptRunner(settings.scripts_dir, settings.script_timeout_sec),
        state_store=StateStore(settings.state_path),
        sync_ledger=sync_ledger,
        sync_queue=sync_queue,
    )

    admin_store = AdminStore(settings.admin_users_config_path, settings.admin_sessions_path)
    management_api = ManagementApi(
        channels_config_path=settings.channels_config_path,
        scripts_dir=settings.scripts_dir,
        gaurd_scripts_dir=settings.gaurd_scripts_dir,
        sync_ledger=sync_ledger,
        sync_queue=sync_queue,
        source_client=telethon_source_client,
        on_routes_reloaded=service.set_routes,
    )
    service.set_route_patch_callback(management_api.update_route)
    service.admin_handler = AdminBotHandler(admin_store=admin_store, management_api=management_api)
    service.management_api = management_api  # type: ignore[attr-defined]
    service.admin_store = admin_store  # type: ignore[attr-defined]
    return service
