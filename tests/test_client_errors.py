from __future__ import annotations

import asyncio

import httpx
import pytest

from kiwi.errors import PlatformApiError
from kiwi.platforms.client import BotApiClient


class RaisingHttpClient:
    async def post(self, *args, **kwargs):
        raise httpx.ConnectTimeout("timeout")

    async def aclose(self):
        return None


def test_client_wraps_httpx_error_as_platform_error() -> None:
    client = BotApiClient(
        token="t",
        api_base_url="https://api.telegram.org",
        file_base_url="https://api.telegram.org/file",
    )
    client.client = RaisingHttpClient()

    with pytest.raises(PlatformApiError) as exc_info:
        asyncio.run(client.get_updates(offset=None, timeout=1, allowed_updates=[]))

    assert "network error" in str(exc_info.value)
