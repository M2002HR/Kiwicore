from __future__ import annotations

import asyncio
import os
import signal

from kiwi.app import build_service
from kiwi.logging_setup import configure_logging


async def _run() -> None:
    service = await build_service()
    await service.run()


def main() -> None:
    configure_logging(os.getenv("LOG_LEVEL", "INFO"), os.getenv("LOG_FORMAT", "json"))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    stop_event = asyncio.Event()

    def _shutdown(*_: object) -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            pass

    task = loop.create_task(_run())

    async def runner() -> None:
        wait_task = asyncio.create_task(stop_event.wait())
        done, pending = await asyncio.wait({task, wait_task}, return_when=asyncio.FIRST_COMPLETED)
        try:
            for finished in done:
                if finished.cancelled():
                    continue
                exc = finished.exception()
                if exc:
                    raise exc
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        finally:
            for p in pending:
                p.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    try:
        loop.run_until_complete(runner())
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


if __name__ == "__main__":
    main()
