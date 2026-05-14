from __future__ import annotations

import asyncio
import logging
import os
import signal

from kiwi.app import build_service
from kiwi.logging_setup import configure_logging
from kiwi.web_admin import AdminWebServer

logger = logging.getLogger(__name__)

def main() -> None:
    configure_logging(os.getenv("LOG_LEVEL", "INFO"), os.getenv("LOG_FORMAT", "json"))

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    service = loop.run_until_complete(build_service())
    web_server: AdminWebServer | None = None
    if service.settings.admin_web_enabled:
        management_api = getattr(service, "management_api", None)
        admin_store = getattr(service, "admin_store", None)
        if management_api is not None and admin_store is not None:
            web_server = AdminWebServer(
                settings=service.settings,
                service=service,
                management_api=management_api,
                admin_store=admin_store,
                event_loop=loop,
            )
            web_server.start()
        else:
            logger.warning("Admin web panel not started: service is missing management/api store references")

    stop_event = asyncio.Event()

    def _shutdown(*_: object) -> None:
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:
            pass

    task = loop.create_task(service.run())

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
                await service.stop()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        finally:
            for p in pending:
                p.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    try:
        loop.run_until_complete(runner())
    finally:
        if web_server is not None:
            try:
                web_server.stop()
            except Exception:
                logger.exception("Failed to stop admin web panel")
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()


if __name__ == "__main__":
    main()
