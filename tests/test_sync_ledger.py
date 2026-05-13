from __future__ import annotations

from pathlib import Path

from kiwi.sync_ledger import SyncLedger


def test_sync_ledger_requeues_stale_processing_record(tmp_path: Path) -> None:
    ledger = SyncLedger(str(tmp_path / "sync_ledger.sqlite3"))
    payload = {"update_id": 1, "message_id": 1}
    created, key, status = ledger.register_message(
        route_name="r1",
        source_channel_id="-1001",
        message_id=1,
        media_group_id=None,
        payload=payload,
    )
    assert created is True
    assert status == "queued"

    ledger.mark_processing(key, trace_id="t1")
    conn = ledger._connect()  # noqa: SLF001
    try:
        ledger._execute(  # noqa: SLF001
            conn,
            "UPDATE sync_message_ledger SET last_attempt_at = ? WHERE dedupe_key = ?",
            ("2000-01-01T00:00:00+00:00", key),
        )
        conn.commit()
    finally:
        conn.close()

    recovered = ledger.requeue_stale_processing(older_than_sec=60)
    assert recovered == 1
    assert key in ledger.list_retryable_keys(limit=10)
    record = ledger.get_record(key)
    assert record is not None
    assert record.status == "failed"
