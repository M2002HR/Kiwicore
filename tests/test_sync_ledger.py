from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone

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


def test_sync_ledger_list_retryable_excludes_ambiguous_records(tmp_path: Path) -> None:
    ledger = SyncLedger(str(tmp_path / "sync_ledger.sqlite3"))
    payload = {"update_id": 1, "message_id": 1}
    created, key, _ = ledger.register_message(
        route_name="r1",
        source_channel_id="-1001",
        message_id=1,
        media_group_id=None,
        payload=payload,
    )
    assert created is True
    ledger.mark_status(key, status="ambiguous", last_error="legacy_ambiguous")
    keys = ledger.list_retryable_keys(limit=10)
    assert key not in keys


def test_sync_ledger_list_retryable_for_route_orders_by_message_id(tmp_path: Path) -> None:
    ledger = SyncLedger(str(tmp_path / "sync_ledger.sqlite3"))
    payload = {"update_id": 1, "message_id": 1}
    _, k3, _ = ledger.register_message(
        route_name="r1",
        source_channel_id="-1001",
        message_id=3,
        media_group_id=None,
        payload=payload,
    )
    _, k1, _ = ledger.register_message(
        route_name="r1",
        source_channel_id="-1001",
        message_id=1,
        media_group_id=None,
        payload=payload,
    )
    _, k2, _ = ledger.register_message(
        route_name="r1",
        source_channel_id="-1001",
        message_id=2,
        media_group_id=None,
        payload=payload,
    )
    keys = ledger.list_retryable_keys_for_route("r1", limit=10)
    assert keys[:3] == [k1, k2, k3]


def test_sync_ledger_first_active_key_for_route_message_id(tmp_path: Path) -> None:
    ledger = SyncLedger(str(tmp_path / "sync_ledger.sqlite3"))
    payload = {"update_id": 1, "message_id": 10}
    _, k1, _ = ledger.register_message(
        route_name="r1",
        source_channel_id="-1001",
        message_id=10,
        media_group_id=None,
        payload=payload,
    )
    _, k2, _ = ledger.register_message(
        route_name="r1",
        source_channel_id="-1001",
        message_id=11,
        media_group_id=None,
        payload=payload,
    )
    assert ledger.first_active_key_for_route_message_id("r1", 10) == k1
    assert ledger.first_active_key_for_route_message_id("r1", 11) == k2
    assert ledger.first_active_key_for_route_message_id("r1", 12) is None


def test_sync_ledger_latest_sent_at_for_route(tmp_path: Path) -> None:
    ledger = SyncLedger(str(tmp_path / "sync_ledger.sqlite3"))
    payload = {"update_id": 1, "message_id": 10}
    _, key_old, _ = ledger.register_message(
        route_name="r1",
        source_channel_id="-1001",
        message_id=10,
        media_group_id=None,
        payload=payload,
    )
    _, key_new, _ = ledger.register_message(
        route_name="r1",
        source_channel_id="-1001",
        message_id=11,
        media_group_id=None,
        payload=payload,
    )
    ledger.mark_status(key_old, status="sent")
    ledger.mark_status(key_new, status="sent")

    old_ts = datetime(2026, 5, 25, 10, 0, 0, tzinfo=timezone.utc).isoformat()
    new_ts = datetime(2026, 5, 25, 10, 5, 0, tzinfo=timezone.utc).isoformat()
    conn = ledger._connect()  # noqa: SLF001
    try:
        ledger._execute(conn, "UPDATE sync_message_ledger SET sent_at = ? WHERE dedupe_key = ?", (old_ts, key_old))  # noqa: SLF001
        ledger._execute(conn, "UPDATE sync_message_ledger SET sent_at = ? WHERE dedupe_key = ?", (new_ts, key_new))  # noqa: SLF001
        conn.commit()
    finally:
        conn.close()

    got = ledger.latest_sent_at_for_route("r1")
    assert got is not None
    assert int(got) == int(datetime.fromisoformat(new_ts).timestamp())
