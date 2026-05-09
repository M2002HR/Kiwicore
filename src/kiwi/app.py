from __future__ import annotations

from kiwi.config import load_routes, load_settings
from kiwi.platforms.client import BotApiClient
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

    return KiwiService(
        settings=settings,
        routes=routes,
        telegram_client=telegram_client,
        bale_client=bale_client,
        storage=StorageManager(settings.storage_dir),
        script_runner=ScriptRunner(settings.scripts_dir, settings.script_timeout_sec),
        state_store=StateStore(settings.state_path),
    )
