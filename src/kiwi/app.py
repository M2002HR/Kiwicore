from __future__ import annotations

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


async def build_service(env_file: str = ".env") -> KiwiService:
    settings = load_settings(env_file)
    routes = load_routes(settings.channels_config_path)

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
        timeout_sec=30.0,
        trust_env=settings.http_trust_env,
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
    )

    admin_store = AdminStore(settings.admin_users_config_path, settings.admin_sessions_path)
    management_api = ManagementApi(
        channels_config_path=settings.channels_config_path,
        scripts_dir=settings.scripts_dir,
        gaurd_scripts_dir=settings.gaurd_scripts_dir,
        on_routes_reloaded=service.set_routes,
    )
    service.admin_handler = AdminBotHandler(admin_store=admin_store, management_api=management_api)
    return service
