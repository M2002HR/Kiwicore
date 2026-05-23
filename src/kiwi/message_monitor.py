from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timezone
from typing import Any


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


class MessageMonitor:
    def __init__(self, *, max_messages: int = 6000, max_events: int = 40000) -> None:
        self._max_messages = max(100, int(max_messages))
        self._events_max = max(1000, int(max_events))
        self._lock = threading.Lock()
        self._messages: dict[str, dict[str, Any]] = {}
        self._events: deque[dict[str, Any]] = deque(maxlen=self._events_max)
        self._seq = 0

    def register_message(
        self,
        *,
        dedupe_key: str,
        route_name: str,
        source_channel_id: str,
        source_channel_username: str | None,
        message_id: int,
        media_group_id: str | None,
        status: str,
        payload: dict[str, Any] | None = None,
    ) -> None:
        now = _utc_now_iso()
        normalized_status = str(status or "queued").strip().lower() or "queued"
        with self._lock:
            item = self._messages.get(dedupe_key)
            if item is None:
                item = {
                    "dedupe_key": dedupe_key,
                    "route_name": route_name,
                    "source_channel_id": source_channel_id,
                    "source_channel_username": source_channel_username,
                    "message_id": int(message_id),
                    "media_group_id": str(media_group_id or "") or None,
                    "status": normalized_status,
                    "current_stage": "queued",
                    "progress_pct": 5.0,
                    "attempt_count": 0,
                    "first_seen_at": now,
                    "updated_at": now,
                    "completed_at": None,
                    "last_error": None,
                    "trace_id": None,
                    "download_bytes": 0,
                    "upload_bytes": 0,
                    "timings_ms": {},
                    "text_preview": self._text_preview_from_payload(payload or {}),
                    "stage_history": [],
                }
                self._messages[dedupe_key] = item
            else:
                item["updated_at"] = now
                item["status"] = normalized_status
                if payload:
                    item["text_preview"] = self._text_preview_from_payload(payload)
            self._touch_stage(
                item,
                stage="queued",
                status=normalized_status,
                progress_pct=max(float(item.get("progress_pct") or 0.0), 5.0),
                now_iso=now,
                details="Queued for sync processing",
            )
            self._emit_event_locked(
                item,
                event_type="status",
                stage="queued",
                details="Message queued",
            )
            self._prune_messages_locked()

    def note_stage(
        self,
        *,
        dedupe_key: str,
        stage: str,
        status: str | None = None,
        progress_pct: float | None = None,
        details: str | None = None,
        timings_ms: dict[str, float] | None = None,
        download_bytes: int | None = None,
        upload_bytes: int | None = None,
        attempt_count: int | None = None,
        trace_id: str | None = None,
    ) -> None:
        now = _utc_now_iso()
        with self._lock:
            item = self._messages.get(dedupe_key)
            if item is None:
                return
            normalized_status = str(status or item.get("status") or "processing").strip().lower() or "processing"
            item["status"] = normalized_status
            item["updated_at"] = now
            if trace_id:
                item["trace_id"] = str(trace_id)
            if timings_ms:
                merged = dict(item.get("timings_ms") or {})
                for key, value in timings_ms.items():
                    try:
                        merged[str(key)] = float(value)
                    except Exception:
                        continue
                item["timings_ms"] = merged
            if download_bytes is not None:
                item["download_bytes"] = max(0, int(download_bytes))
            if upload_bytes is not None:
                item["upload_bytes"] = max(0, int(upload_bytes))
            if attempt_count is not None:
                item["attempt_count"] = max(0, int(attempt_count))
            progress = float(progress_pct) if progress_pct is not None else float(item.get("progress_pct") or 0.0)
            if progress_pct is None:
                progress = max(progress, 0.0)
            self._touch_stage(
                item,
                stage=stage,
                status=normalized_status,
                progress_pct=progress,
                now_iso=now,
                details=details,
            )
            self._emit_event_locked(
                item,
                event_type="stage",
                stage=stage,
                details=details,
            )

    def note_status(
        self,
        *,
        dedupe_key: str,
        status: str,
        error: str | None = None,
        progress_pct: float | None = None,
        details: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        now = _utc_now_iso()
        normalized = str(status or "").strip().lower() or "failed"
        terminal = normalized in {"sent", "failed", "blocked", "ambiguous"}
        with self._lock:
            item = self._messages.get(dedupe_key)
            if item is None:
                return
            item["status"] = normalized
            item["updated_at"] = now
            if trace_id:
                item["trace_id"] = str(trace_id)
            if error:
                item["last_error"] = str(error)
            if progress_pct is not None:
                item["progress_pct"] = max(0.0, min(100.0, float(progress_pct)))
            elif terminal:
                item["progress_pct"] = 100.0
            if terminal:
                item["completed_at"] = now
            self._touch_stage(
                item,
                stage="status",
                status=normalized,
                progress_pct=float(item.get("progress_pct") or 0.0),
                now_iso=now,
                details=details or error,
            )
            self._emit_event_locked(
                item,
                event_type="status",
                stage="status",
                details=details,
            )

    def list_messages(
        self,
        *,
        limit: int = 200,
        route_name: str | None = None,
        status: str | None = None,
        active_only: bool = False,
        search: str | None = None,
    ) -> dict[str, Any]:
        take = max(1, min(2000, int(limit)))
        route_filter = str(route_name or "").strip().lower()
        status_filter = str(status or "").strip().lower()
        search_filter = str(search or "").strip().lower()
        with self._lock:
            rows = [dict(item) for item in self._messages.values()]
            latest_seq = int(self._seq)
        if route_filter:
            rows = [item for item in rows if str(item.get("route_name") or "").strip().lower() == route_filter]
        if status_filter:
            rows = [item for item in rows if str(item.get("status") or "").strip().lower() == status_filter]
        if active_only:
            rows = [item for item in rows if str(item.get("status") or "") in {"queued", "processing", "failed", "ambiguous"}]
        if search_filter:
            def _match(item: dict[str, Any]) -> bool:
                hay = " | ".join(
                    [
                        str(item.get("dedupe_key") or ""),
                        str(item.get("route_name") or ""),
                        str(item.get("source_channel_id") or ""),
                        str(item.get("source_channel_username") or ""),
                        str(item.get("message_id") or ""),
                        str(item.get("media_group_id") or ""),
                        str(item.get("text_preview") or ""),
                        str(item.get("last_error") or ""),
                    ]
                ).lower()
                return search_filter in hay
            rows = [item for item in rows if _match(item)]
        rows.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        summary_counts: dict[str, int] = {}
        for item in rows:
            key = str(item.get("status") or "").strip().lower() or "unknown"
            summary_counts[key] = int(summary_counts.get(key, 0) or 0) + 1
        total_download = sum(max(0, int(item.get("download_bytes") or 0)) for item in rows)
        total_upload = sum(max(0, int(item.get("upload_bytes") or 0)) for item in rows)
        out_rows = rows[:take]
        return {
            "messages": out_rows,
            "summary": {
                "total": len(rows),
                "status_counts": summary_counts,
                "total_download_bytes": int(total_download),
                "total_upload_bytes": int(total_upload),
            },
            "latest_seq": latest_seq,
        }

    def list_events(self, *, after_seq: int = 0, limit: int = 200) -> dict[str, Any]:
        take = max(1, min(2000, int(limit)))
        after = max(0, int(after_seq))
        with self._lock:
            items = [dict(event) for event in self._events if int(event.get("seq") or 0) > after]
            latest_seq = int(self._seq)
        if len(items) > take:
            items = items[-take:]
        return {"events": items, "latest_seq": latest_seq}

    def clear_route(self, route_name: str) -> dict[str, int]:
        target = str(route_name or "").strip()
        if not target:
            return {"removed_messages": 0, "removed_events": 0}
        removed_messages = 0
        removed_events = 0
        with self._lock:
            keys = [
                dedupe_key
                for dedupe_key, item in self._messages.items()
                if str(item.get("route_name") or "").strip() == target
            ]
            for key in keys:
                self._messages.pop(key, None)
                removed_messages += 1

            kept_events: deque[dict[str, Any]] = deque(maxlen=self._events_max)
            for event in self._events:
                if str(event.get("route_name") or "").strip() == target:
                    removed_events += 1
                    continue
                kept_events.append(event)
            self._events = kept_events
        return {"removed_messages": int(removed_messages), "removed_events": int(removed_events)}

    @staticmethod
    def _text_preview_from_payload(payload: dict[str, Any]) -> str | None:
        message = payload.get("message")
        if not isinstance(message, dict):
            return None
        text = str(message.get("text") or "").strip()
        caption = str(message.get("caption") or "").strip()
        source = text or caption
        if not source:
            return None
        compact = " ".join(source.split())
        if len(compact) <= 220:
            return compact
        return compact[:217] + "..."

    @staticmethod
    def _touch_stage(
        item: dict[str, Any],
        *,
        stage: str,
        status: str,
        progress_pct: float,
        now_iso: str,
        details: str | None = None,
    ) -> None:
        normalized_stage = str(stage or "").strip().lower() or "processing"
        normalized_status = str(status or "").strip().lower() or "processing"
        item["current_stage"] = normalized_stage
        item["status"] = normalized_status
        clamped_progress = max(0.0, min(100.0, float(progress_pct)))
        if clamped_progress >= float(item.get("progress_pct") or 0.0):
            item["progress_pct"] = clamped_progress
        item["updated_at"] = now_iso
        history = list(item.get("stage_history") or [])
        history.append(
            {
                "ts": now_iso,
                "stage": normalized_stage,
                "status": normalized_status,
                "progress_pct": round(float(item.get("progress_pct") or 0.0), 1),
                "details": details or None,
            }
        )
        if len(history) > 120:
            history = history[-120:]
        item["stage_history"] = history

    def _emit_event_locked(
        self,
        item: dict[str, Any],
        *,
        event_type: str,
        stage: str,
        details: str | None = None,
    ) -> None:
        self._seq += 1
        status = str(item.get("status") or "").strip().lower()
        event = {
            "seq": int(self._seq),
            "ts": _utc_now_iso(),
            "event_type": str(event_type),
            "dedupe_key": str(item.get("dedupe_key") or ""),
            "route_name": str(item.get("route_name") or ""),
            "source_channel_id": str(item.get("source_channel_id") or ""),
            "source_channel_username": item.get("source_channel_username"),
            "message_id": int(item.get("message_id") or 0),
            "media_group_id": item.get("media_group_id"),
            "status": status,
            "stage": str(stage or ""),
            "progress_pct": float(item.get("progress_pct") or 0.0),
            "attempt_count": int(item.get("attempt_count") or 0),
            "error": item.get("last_error"),
            "details": details,
            "download_bytes": int(item.get("download_bytes") or 0),
            "upload_bytes": int(item.get("upload_bytes") or 0),
            "is_terminal": status in {"sent", "failed", "blocked", "ambiguous"},
        }
        self._events.append(event)

    def _prune_messages_locked(self) -> None:
        size = len(self._messages)
        if size <= self._max_messages:
            return
        rows = sorted(self._messages.values(), key=lambda item: str(item.get("updated_at") or ""))
        drop_count = max(0, size - self._max_messages)
        for item in rows[:drop_count]:
            key = str(item.get("dedupe_key") or "")
            if key:
                self._messages.pop(key, None)
