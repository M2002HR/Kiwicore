from __future__ import annotations

import argparse
import asyncio
import getpass
import sqlite3
from os import getenv
from pathlib import Path
from urllib.parse import unquote, urlparse

from telethon import TelegramClient


async def _run(api_id: int, api_hash: str, session_path: str, phone: str | None, proxy_url: str | None) -> None:
    session_file = Path(session_path)
    session_file.parent.mkdir(parents=True, exist_ok=True)

    client = TelegramClient(str(session_file), api_id, api_hash, proxy=_parse_proxy_url(proxy_url))
    await client.connect()
    try:
        if await client.is_user_authorized():
            me = await client.get_me()
            print(f"Session already authorized for: {me.username or me.id}")
            return

        phone_value = (phone or "").strip() or input("Phone (+989...): ").strip()
        await client.send_code_request(phone_value)
        code = input("Telegram login code: ").strip()
        try:
            await client.sign_in(phone=phone_value, code=code)
        except Exception:
            password = getpass.getpass("Two-step password: ")
            await client.sign_in(password=password)
        me = await client.get_me()
        print(f"Session authorized: {me.username or me.id}")
    finally:
        await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize Telethon session file")
    parser.add_argument("--api-id", type=int, required=True)
    parser.add_argument("--api-hash", required=True)
    parser.add_argument("--session-path", default="./app_data/telethon.session")
    parser.add_argument("--phone", default="")
    parser.add_argument("--proxy", default="")
    args = parser.parse_args()
    proxy_url = args.proxy.strip() or getenv("TELETHON_PROXY_URL", "").strip() or None

    try:
        asyncio.run(_run(args.api_id, args.api_hash, args.session_path, args.phone or None, proxy_url))
    except sqlite3.OperationalError as exc:
        if "readonly" in str(exc).lower():
            raise SystemExit(
                "Telethon session path is readonly. "
                "Fix permissions for session file/directory (e.g. app_data) and run again."
            ) from exc
        raise


def _parse_proxy_url(proxy_url: str | None) -> object | None:
    if not proxy_url:
        return None

    parsed = urlparse(proxy_url.strip())
    scheme = parsed.scheme.strip().lower()
    host = parsed.hostname
    port = parsed.port
    if not scheme or not host or port is None:
        raise SystemExit("Invalid proxy URL. Expected scheme://host:port")

    proxy_type = _proxy_type_from_scheme(scheme)
    username = unquote(parsed.username) if parsed.username else None
    password = unquote(parsed.password) if parsed.password else None
    rdns = scheme in {"socks5h", "socks4a"}
    return (proxy_type, host, port, rdns, username, password)


def _proxy_type_from_scheme(scheme: str) -> object:
    normalized = {"socks5h": "socks5", "socks4a": "socks4", "https": "http"}.get(scheme, scheme)
    if normalized not in {"socks5", "socks4", "http"}:
        raise SystemExit(
            "Unsupported proxy scheme. Use socks5://, socks5h://, socks4://, socks4a://, http://, or https://"
        )
    try:
        import socks  # type: ignore[import-not-found]

        if normalized == "socks5":
            return socks.SOCKS5
        if normalized == "socks4":
            return socks.SOCKS4
        return socks.HTTP
    except Exception:
        return normalized


if __name__ == "__main__":
    main()
