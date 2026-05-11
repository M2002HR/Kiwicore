from __future__ import annotations

import argparse
import asyncio
import getpass
import sqlite3
from pathlib import Path

from telethon import TelegramClient


async def _run(api_id: int, api_hash: str, session_path: str, phone: str | None) -> None:
    session_file = Path(session_path)
    session_file.parent.mkdir(parents=True, exist_ok=True)

    client = TelegramClient(str(session_file), api_id, api_hash)
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
    args = parser.parse_args()

    try:
        asyncio.run(_run(args.api_id, args.api_hash, args.session_path, args.phone or None))
    except sqlite3.OperationalError as exc:
        if "readonly" in str(exc).lower():
            raise SystemExit(
                "Telethon session path is readonly. "
                "Fix permissions for session file/directory (e.g. app_data) and run again."
            ) from exc
        raise


if __name__ == "__main__":
    main()
