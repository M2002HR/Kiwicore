from __future__ import annotations

import json
import logging
from pathlib import Path

from kiwi.config import Settings
from kiwi.sync_ledger import SyncLedger
from kiwi.sync_queue import build_sync_queue_backend

logger = logging.getLogger(__name__)


def _atomic_dump_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _reset_routes_for_sync(settings: Settings) -> int:
    channels_path = Path(settings.channels_config_path)
    if not channels_path.exists():
        return 0

    raw = json.loads(channels_path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        return 0

    changed = 0
    for item in raw:
        if not isinstance(item, dict):
            continue

        # Turn every route into sync mode on startup with bounded backfill window.
        sync_obj = item.get("sync") if isinstance(item.get("sync"), dict) else {}
        sync_obj["enabled"] = True
        sync_obj["status"] = "syncing"
        sync_obj["backfill_count"] = int(settings.sync_reset_backfill_count)
        sync_obj["seeded"] = False
        item["sync"] = sync_obj
        item["enabled"] = False
        changed += 1

    _atomic_dump_json(channels_path, raw)
    return changed


def _reset_state_files(settings: Settings) -> None:
    state_path = Path(settings.state_path)
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps({"offset": None}, ensure_ascii=False), encoding="utf-8")

    sessions_path = Path(settings.admin_sessions_path)
    sessions_path.parent.mkdir(parents=True, exist_ok=True)
    sessions_path.write_text("{}", encoding="utf-8")


async def run_startup_reset(settings: Settings) -> None:
    if not settings.sync_reset_on_start:
        return

    logger.warning("Startup sync reset is enabled; clearing sync state")

    ledger = SyncLedger(settings.sync_ledger_db_path)
    ledger.reset_all()

    queue = await build_sync_queue_backend(
        backend=settings.sync_queue_backend,
        redis_url=settings.redis_url,
    )
    try:
        await queue.clear()
    except Exception:
        logger.exception("Failed to clear sync queue backend; continuing")

    changed = 0
    if settings.sync_reset_routes_on_start:
        changed = _reset_routes_for_sync(settings)

    if settings.sync_reset_state_on_start:
        _reset_state_files(settings)

    logger.warning(
        "Startup sync reset completed",
        extra={
            "details": {
                "routes_reset": changed,
                "queue_backend": settings.sync_queue_backend,
                "ledger_backend": "mysql" if settings.sync_ledger_db_path.startswith("mysql://") or settings.sync_ledger_db_path.startswith("mysql+pymysql://") else "sqlite",
                "sync_reset_backfill_count": settings.sync_reset_backfill_count,
                "sync_start_from_beginning": settings.sync_start_from_beginning,
            }
        },
    )
