from __future__ import annotations

from kiwi.message_monitor import MessageMonitor


def test_message_monitor_tracks_stage_status_and_events() -> None:
    monitor = MessageMonitor(max_messages=100, max_events=1000)
    payload = {"message": {"text": "hello world"}}
    monitor.register_message(
        dedupe_key="k1",
        route_name="r1",
        source_channel_id="-1001",
        source_channel_username="@src",
        message_id=11,
        media_group_id=None,
        status="queued",
        payload=payload,
    )
    monitor.note_stage(
        dedupe_key="k1",
        stage="download",
        status="processing",
        progress_pct=42.0,
        details="download ok",
        download_bytes=1200,
        attempt_count=1,
    )
    monitor.note_status(
        dedupe_key="k1",
        status="sent",
        details="done",
        progress_pct=100.0,
    )

    snap = monitor.list_messages(limit=50)
    assert snap["summary"]["total"] == 1
    assert snap["summary"]["status_counts"]["sent"] == 1
    row = snap["messages"][0]
    assert row["dedupe_key"] == "k1"
    assert row["status"] == "sent"
    assert row["download_bytes"] == 1200
    assert row["progress_pct"] >= 100.0
    assert row["text_preview"] == "hello world"
    assert len(row["stage_history"]) >= 2

    ev = monitor.list_events(after_seq=0, limit=100)
    assert ev["latest_seq"] >= 3
    assert len(ev["events"]) >= 3
    assert any(item.get("status") == "sent" for item in ev["events"])


def test_message_monitor_active_filter_works() -> None:
    monitor = MessageMonitor(max_messages=100, max_events=100)
    monitor.register_message(
        dedupe_key="k-active",
        route_name="r1",
        source_channel_id="-1",
        source_channel_username=None,
        message_id=1,
        media_group_id=None,
        status="queued",
        payload={},
    )
    monitor.register_message(
        dedupe_key="k-done",
        route_name="r1",
        source_channel_id="-1",
        source_channel_username=None,
        message_id=2,
        media_group_id=None,
        status="queued",
        payload={},
    )
    monitor.note_status(dedupe_key="k-done", status="sent", progress_pct=100.0)

    active = monitor.list_messages(limit=10, active_only=True)
    assert active["summary"]["total"] == 1
    assert active["messages"][0]["dedupe_key"] == "k-active"
