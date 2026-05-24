from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

TERMINAL_STATUSES = {"sent", "blocked", "skipped"}
# Only statuses that are currently executable should block per-route ordering.
# Keeping failed/ambiguous here can deadlock a route behind one bad record.
ACTIVE_STATUSES = {"queued", "processing"}


@dataclass(slots=True)
class LedgerRecord:
    dedupe_key: str
    route_name: str
    source_channel_id: str
    message_id: int
    media_group_id: str
    status: str
    attempt_count: int
    payload: dict[str, Any]


class SyncLedger:
    def __init__(self, db_path: str) -> None:
        self.raw_dsn = str(db_path or "").strip()
        self.backend = "sqlite"
        self.db_path: Path | None = None
        self.mysql_config: dict[str, Any] | None = None

        if self.raw_dsn.startswith("mysql://") or self.raw_dsn.startswith("mysql+pymysql://"):
            self.backend = "mysql"
            self.mysql_config = _parse_mysql_dsn(self.raw_dsn)
        else:
            self.backend = "sqlite"
            self.db_path = Path(self.raw_dsn or "./app_data/sync_ledger.sqlite3")
            self.db_path.parent.mkdir(parents=True, exist_ok=True)

        self._lock = threading.Lock()
        self._init_schema()

    @staticmethod
    def dedupe_key(route_name: str, source_channel_id: str, message_id: int, media_group_id: str | None) -> str:
        mg = (media_group_id or "-").strip() or "-"
        return f"{route_name}|{source_channel_id}|{int(message_id)}|{mg}"

    def register_message(
        self,
        *,
        route_name: str,
        source_channel_id: str,
        message_id: int,
        media_group_id: str | None,
        payload: dict[str, Any],
    ) -> tuple[bool, str, str]:
        key = self.dedupe_key(route_name, source_channel_id, int(message_id), media_group_id)
        now = _utc_now_iso()
        payload_json = json.dumps(payload, ensure_ascii=False)

        with self._lock:
            conn = self._connect()
            try:
                row = self._fetchone(conn, "SELECT status FROM sync_message_ledger WHERE dedupe_key = ?", (key,))
                if row is None:
                    self._execute(
                        conn,
                        """
                        INSERT INTO sync_message_ledger (
                            dedupe_key, route_name, source_channel_id, message_id, media_group_id,
                            status, attempt_count, first_seen_at, payload_json
                        ) VALUES (?, ?, ?, ?, ?, 'queued', 0, ?, ?)
                        """,
                        (key, route_name, source_channel_id, int(message_id), (media_group_id or "-"), now, payload_json),
                    )
                    conn.commit()
                    return True, key, "queued"

                status = str(row[0] or "").strip().lower()
                if status in TERMINAL_STATUSES:
                    return False, key, status

                self._execute(
                    conn,
                    "UPDATE sync_message_ledger SET payload_json = ? WHERE dedupe_key = ?",
                    (payload_json, key),
                )
                conn.commit()
                return False, key, status
            finally:
                conn.close()

    def mark_processing(self, dedupe_key: str, *, trace_id: str | None = None) -> None:
        now = _utc_now_iso()
        with self._lock:
            conn = self._connect()
            try:
                self._execute(
                    conn,
                    """
                    UPDATE sync_message_ledger
                    SET status = 'processing',
                        attempt_count = attempt_count + 1,
                        last_attempt_at = ?,
                        trace_id = COALESCE(?, trace_id)
                    WHERE dedupe_key = ?
                    """,
                    (now, trace_id, dedupe_key),
                )
                conn.commit()
            finally:
                conn.close()

    def mark_status(
        self,
        dedupe_key: str,
        *,
        status: str,
        last_error: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        normalized = str(status or "").strip().lower()
        if normalized == "skipped":
            normalized = "blocked"
        sent_at = _utc_now_iso() if normalized == "sent" else None
        with self._lock:
            conn = self._connect()
            try:
                self._execute(
                    conn,
                    """
                    UPDATE sync_message_ledger
                    SET status = ?,
                        last_error = ?,
                        trace_id = COALESCE(?, trace_id),
                        sent_at = COALESCE(?, sent_at)
                    WHERE dedupe_key = ?
                    """,
                    (normalized, last_error, trace_id, sent_at, dedupe_key),
                )
                conn.commit()
            finally:
                conn.close()

    def get_record(self, dedupe_key: str) -> LedgerRecord | None:
        with self._lock:
            conn = self._connect()
            try:
                row = self._fetchone(
                    conn,
                    """
                    SELECT dedupe_key, route_name, source_channel_id, message_id, media_group_id,
                           status, attempt_count, payload_json
                    FROM sync_message_ledger
                    WHERE dedupe_key = ?
                    """,
                    (dedupe_key,),
                )
            finally:
                conn.close()
        if row is None:
            return None
        if str(row[5] or "").strip().lower() == "sent":
            return None
        payload = json.loads(str(row[7] or "{}"))
        return LedgerRecord(
            dedupe_key=str(row[0]),
            route_name=str(row[1]),
            source_channel_id=str(row[2]),
            message_id=int(row[3]),
            media_group_id=str(row[4] or "-"),
            status=str(row[5]),
            attempt_count=int(row[6] or 0),
            payload=payload if isinstance(payload, dict) else {},
        )

    def add_review(self, *, dedupe_key: str, route_name: str, reason: str, last_error: str | None = None) -> int:
        now = _utc_now_iso()
        with self._lock:
            conn = self._connect()
            try:
                unresolved = self._fetchone(
                    conn,
                    """
                    SELECT id FROM sync_review_queue
                    WHERE dedupe_key = ? AND resolved_at IS NULL
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (dedupe_key,),
                )
                if unresolved is not None:
                    return int(unresolved[0])

                payload_row = self._fetchone(
                    conn,
                    "SELECT payload_json FROM sync_message_ledger WHERE dedupe_key = ?",
                    (dedupe_key,),
                )
                payload_json = str(payload_row[0] or "{}") if payload_row else "{}"
                cur = self._execute(
                    conn,
                    """
                    INSERT INTO sync_review_queue (
                        dedupe_key, route_name, reason, last_error, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (dedupe_key, route_name, reason, last_error, payload_json, now),
                )
                conn.commit()
                if self.backend == "mysql":
                    return int(cur.lastrowid or 0)
                return int(cur.lastrowid or 0)
            finally:
                conn.close()

    def list_review(self, *, limit: int = 50, only_open: bool = True) -> list[dict[str, Any]]:
        query = (
            "SELECT id, dedupe_key, route_name, reason, last_error, created_at, resolved_at, resolution "
            "FROM sync_review_queue"
        )
        params: tuple[Any, ...] = ()
        if only_open:
            query += " WHERE resolved_at IS NULL"
        query += " ORDER BY id DESC LIMIT ?"
        params = (max(1, int(limit)),)

        with self._lock:
            conn = self._connect()
            try:
                rows = self._fetchall(conn, query, params)
            finally:
                conn.close()

        out: list[dict[str, Any]] = []
        for row in rows:
            out.append(
                {
                    "id": int(row[0]),
                    "dedupe_key": str(row[1]),
                    "route_name": str(row[2]),
                    "reason": str(row[3] or ""),
                    "last_error": str(row[4] or "") or None,
                    "created_at": str(row[5] or ""),
                    "resolved_at": str(row[6] or "") or None,
                    "resolution": str(row[7] or "") or None,
                }
            )
        return out

    def resolve_review(self, review_id: int, *, resolution: str) -> dict[str, Any] | None:
        now = _utc_now_iso()
        with self._lock:
            conn = self._connect()
            try:
                row = self._fetchone(
                    conn,
                    "SELECT id, dedupe_key, route_name FROM sync_review_queue WHERE id = ?",
                    (int(review_id),),
                )
                if row is None:
                    return None

                self._execute(
                    conn,
                    "UPDATE sync_review_queue SET resolved_at = ?, resolution = ? WHERE id = ?",
                    (now, resolution, int(review_id)),
                )

                dedupe_key = str(row[1])
                if resolution == "retry":
                    self._execute(
                        conn,
                        "UPDATE sync_message_ledger SET status = 'queued', last_error = NULL WHERE dedupe_key = ?",
                        (dedupe_key,),
                    )
                elif resolution == "skip":
                    self._execute(
                        conn,
                        "UPDATE sync_message_ledger SET status = 'blocked', last_error = COALESCE(last_error, 'manually_skipped') WHERE dedupe_key = ?",
                        (dedupe_key,),
                    )

                conn.commit()
                return {
                    "id": int(row[0]),
                    "dedupe_key": dedupe_key,
                    "route_name": str(row[2]),
                    "resolution": resolution,
                }
            finally:
                conn.close()

    def get_status_counts(self) -> dict[str, int]:
        with self._lock:
            conn = self._connect()
            try:
                rows = self._fetchall(
                    conn,
                    "SELECT status, COUNT(*) FROM sync_message_ledger GROUP BY status",
                    (),
                )
            finally:
                conn.close()
        out: dict[str, int] = {}
        for status, count in rows:
            out[str(status)] = int(count or 0)
        return out

    def open_review_count(self) -> int:
        with self._lock:
            conn = self._connect()
            try:
                row = self._fetchone(conn, "SELECT COUNT(*) FROM sync_review_queue WHERE resolved_at IS NULL", ())
            finally:
                conn.close()
        return int((row[0] if row else 0) or 0)

    def get_route_status_counts(self, route_name: str) -> dict[str, int]:
        with self._lock:
            conn = self._connect()
            try:
                rows = self._fetchall(
                    conn,
                    "SELECT status, COUNT(*) FROM sync_message_ledger WHERE route_name = ? GROUP BY status",
                    (route_name,),
                )
            finally:
                conn.close()
        out: dict[str, int] = {}
        for status, count in rows:
            out[str(status)] = int(count or 0)
        return out

    def active_count_for_route(self, route_name: str) -> int:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        params: list[Any] = [route_name]
        params.extend(sorted(ACTIVE_STATUSES))
        with self._lock:
            conn = self._connect()
            try:
                row = self._fetchone(
                    conn,
                    f"SELECT COUNT(*) FROM sync_message_ledger WHERE route_name = ? AND status IN ({placeholders})",
                    tuple(params),
                )
            finally:
                conn.close()
        return int((row[0] if row else 0) or 0)

    def min_active_message_id_for_route(self, route_name: str, *, above_checkpoint: int = 0) -> int | None:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        params: list[Any] = [route_name]
        params.extend(sorted(ACTIVE_STATUSES))
        params.append(max(0, int(above_checkpoint)))
        with self._lock:
            conn = self._connect()
            try:
                row = self._fetchone(
                    conn,
                    f"""
                    SELECT MIN(message_id)
                    FROM sync_message_ledger
                    WHERE route_name = ?
                      AND status IN ({placeholders})
                      AND message_id > ?
                    """,
                    tuple(params),
                )
            finally:
                conn.close()
        if row is None or row[0] is None:
            return None
        return int(row[0])

    def first_active_key_for_route_message_id(self, route_name: str, message_id: int) -> str | None:
        placeholders = ",".join("?" for _ in ACTIVE_STATUSES)
        params: list[Any] = [route_name]
        params.extend(sorted(ACTIVE_STATUSES))
        params.append(int(message_id))
        with self._lock:
            conn = self._connect()
            try:
                row = self._fetchone(
                    conn,
                    f"""
                    SELECT dedupe_key
                    FROM sync_message_ledger
                    WHERE route_name = ?
                      AND status IN ({placeholders})
                      AND message_id = ?
                    ORDER BY first_seen_at ASC
                    LIMIT 1
                    """,
                    tuple(params),
                )
            finally:
                conn.close()
        if row is None or row[0] is None:
            return None
        return str(row[0])

    def set_route_checkpoint(self, route_name: str, last_source_message_id: int) -> None:
        now = _utc_now_iso()
        with self._lock:
            conn = self._connect()
            try:
                if self.backend == "mysql":
                    self._execute(
                        conn,
                        """
                        INSERT INTO sync_route_checkpoint(route_name, last_source_message_id, updated_at)
                        VALUES (?, ?, ?)
                        ON DUPLICATE KEY UPDATE
                            last_source_message_id = GREATEST(last_source_message_id, VALUES(last_source_message_id)),
                            updated_at = VALUES(updated_at)
                        """,
                        (route_name, int(last_source_message_id), now),
                    )
                else:
                    self._execute(
                        conn,
                        """
                        INSERT INTO sync_route_checkpoint(route_name, last_source_message_id, updated_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(route_name)
                        DO UPDATE SET
                            last_source_message_id = CASE
                                WHEN excluded.last_source_message_id > sync_route_checkpoint.last_source_message_id
                                THEN excluded.last_source_message_id
                                ELSE sync_route_checkpoint.last_source_message_id
                            END,
                            updated_at = excluded.updated_at
                        """,
                        (route_name, int(last_source_message_id), now),
                    )
                conn.commit()
            finally:
                conn.close()

    def get_route_checkpoint(self, route_name: str) -> int | None:
        with self._lock:
            conn = self._connect()
            try:
                row = self._fetchone(
                    conn,
                    "SELECT last_source_message_id FROM sync_route_checkpoint WHERE route_name = ?",
                    (route_name,),
                )
            finally:
                conn.close()
        if row is None:
            return None
        return int(row[0])

    def delete_route_checkpoint(self, route_name: str) -> int:
        name = str(route_name or "").strip()
        if not name:
            return 0
        with self._lock:
            conn = self._connect()
            try:
                cur = self._execute(
                    conn,
                    "DELETE FROM sync_route_checkpoint WHERE route_name = ?",
                    (name,),
                )
                conn.commit()
                return int(getattr(cur, "rowcount", 0) or 0)
            finally:
                conn.close()

    def clear_route_sync_state(self, route_name: str) -> dict[str, int]:
        name = str(route_name or "").strip()
        if not name:
            return {
                "deleted_checkpoint_rows": 0,
                "deleted_ledger_rows": 0,
                "deleted_review_rows": 0,
            }
        with self._lock:
            conn = self._connect()
            try:
                c1 = self._execute(
                    conn,
                    "DELETE FROM sync_message_ledger WHERE route_name = ?",
                    (name,),
                )
                c2 = self._execute(
                    conn,
                    "DELETE FROM sync_route_checkpoint WHERE route_name = ?",
                    (name,),
                )
                c3 = self._execute(
                    conn,
                    "DELETE FROM sync_review_queue WHERE route_name = ?",
                    (name,),
                )
                conn.commit()
                return {
                    "deleted_checkpoint_rows": int(getattr(c2, "rowcount", 0) or 0),
                    "deleted_ledger_rows": int(getattr(c1, "rowcount", 0) or 0),
                    "deleted_review_rows": int(getattr(c3, "rowcount", 0) or 0),
                }
            finally:
                conn.close()

    def list_retryable_keys(self, *, limit: int = 500) -> list[str]:
        with self._lock:
            conn = self._connect()
            try:
                rows = self._fetchall(
                    conn,
                    """
                    SELECT dedupe_key FROM sync_message_ledger
                    WHERE status IN ('queued', 'failed', 'retry_wait', 'ambiguous')
                    ORDER BY route_name ASC, message_id ASC, first_seen_at ASC
                    LIMIT ?
                    """,
                    (max(1, int(limit)),),
                )
            finally:
                conn.close()
        return [str(item[0]) for item in rows]

    def list_retryable_keys_for_route(self, route_name: str, *, limit: int = 500) -> list[str]:
        with self._lock:
            conn = self._connect()
            try:
                rows = self._fetchall(
                    conn,
                    """
                    SELECT dedupe_key FROM sync_message_ledger
                    WHERE route_name = ?
                      AND status IN ('queued', 'failed', 'retry_wait', 'ambiguous')
                    ORDER BY message_id ASC, first_seen_at ASC
                    LIMIT ?
                    """,
                    (str(route_name or "").strip(), max(1, int(limit))),
                )
            finally:
                conn.close()
        return [str(item[0]) for item in rows]

    def reactivate_route_deactive_blocks(self, route_name: str) -> int:
        name = str(route_name or "").strip()
        if not name:
            return 0
        with self._lock:
            conn = self._connect()
            try:
                cur = self._execute(
                    conn,
                    """
                    UPDATE sync_message_ledger
                    SET status = 'failed',
                        last_error = 'route_resumed_after_deactive'
                    WHERE route_name = ?
                      AND status = 'blocked'
                      AND (
                        last_error = 'route_deactive'
                        OR last_error LIKE 'route_deactive:%%'
                      )
                    """,
                    (name,),
                )
                conn.commit()
                return int(getattr(cur, "rowcount", 0) or 0)
            finally:
                conn.close()

    def requeue_stale_processing(
        self,
        *,
        older_than_sec: int,
        retry_error: str = "stale_processing_requeued",
    ) -> int:
        threshold = (datetime.now(tz=timezone.utc) - timedelta(seconds=max(1, int(older_than_sec)))).isoformat()
        with self._lock:
            conn = self._connect()
            try:
                cur = self._execute(
                    conn,
                    """
                    UPDATE sync_message_ledger
                    SET status = 'failed',
                        last_error = ?
                    WHERE status = 'processing'
                      AND (last_attempt_at IS NULL OR last_attempt_at <= ?)
                    """,
                    (retry_error, threshold),
                )
                conn.commit()
                return int(getattr(cur, "rowcount", 0) or 0)
            finally:
                conn.close()

    def _init_schema(self) -> None:
        with self._lock:
            conn = self._connect()
            try:
                if self.backend == "mysql":
                    self._execute(
                        conn,
                        """
                        CREATE TABLE IF NOT EXISTS sync_message_ledger (
                            dedupe_key VARCHAR(255) PRIMARY KEY,
                            route_name VARCHAR(191) NOT NULL,
                            source_channel_id VARCHAR(191) NOT NULL,
                            message_id BIGINT NOT NULL,
                            media_group_id VARCHAR(191) NOT NULL,
                            status VARCHAR(32) NOT NULL,
                            attempt_count INT NOT NULL DEFAULT 0,
                            first_seen_at VARCHAR(64) NOT NULL,
                            last_attempt_at VARCHAR(64),
                            sent_at VARCHAR(64),
                            last_error TEXT,
                            trace_id VARCHAR(255),
                            payload_json LONGTEXT NOT NULL
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                        """,
                        (),
                    )
                    self._try_execute(
                        conn,
                        "CREATE INDEX idx_sync_ledger_route_status ON sync_message_ledger(route_name, status)",
                        (),
                    )
                    self._try_execute(
                        conn,
                        "CREATE UNIQUE INDEX uq_sync_ledger_message_identity "
                        "ON sync_message_ledger(route_name, source_channel_id, message_id, media_group_id)",
                        (),
                    )
                    self._execute(
                        conn,
                        """
                        CREATE TABLE IF NOT EXISTS sync_route_checkpoint (
                            route_name VARCHAR(191) PRIMARY KEY,
                            last_source_message_id BIGINT NOT NULL,
                            updated_at VARCHAR(64) NOT NULL
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                        """,
                        (),
                    )
                    self._execute(
                        conn,
                        """
                        CREATE TABLE IF NOT EXISTS sync_review_queue (
                            id BIGINT PRIMARY KEY AUTO_INCREMENT,
                            dedupe_key VARCHAR(255) NOT NULL,
                            route_name VARCHAR(191) NOT NULL,
                            reason VARCHAR(191) NOT NULL,
                            last_error TEXT,
                            payload_json LONGTEXT,
                            created_at VARCHAR(64) NOT NULL,
                            resolved_at VARCHAR(64),
                            resolution VARCHAR(32)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
                        """,
                        (),
                    )
                    self._try_execute(
                        conn,
                        "CREATE INDEX idx_sync_review_open ON sync_review_queue(resolved_at, id DESC)",
                        (),
                    )
                else:
                    self._execute(
                        conn,
                        """
                        CREATE TABLE IF NOT EXISTS sync_message_ledger (
                            dedupe_key TEXT PRIMARY KEY,
                            route_name TEXT NOT NULL,
                            source_channel_id TEXT NOT NULL,
                            message_id INTEGER NOT NULL,
                            media_group_id TEXT NOT NULL,
                            status TEXT NOT NULL,
                            attempt_count INTEGER NOT NULL DEFAULT 0,
                            first_seen_at TEXT NOT NULL,
                            last_attempt_at TEXT,
                            sent_at TEXT,
                            last_error TEXT,
                            trace_id TEXT,
                            payload_json TEXT NOT NULL
                        )
                        """,
                        (),
                    )
                    self._execute(
                        conn,
                        "CREATE INDEX IF NOT EXISTS idx_sync_ledger_route_status ON sync_message_ledger(route_name, status)",
                        (),
                    )
                    self._execute(
                        conn,
                        """
                        CREATE UNIQUE INDEX IF NOT EXISTS uq_sync_ledger_message_identity
                        ON sync_message_ledger(route_name, source_channel_id, message_id, media_group_id)
                        """,
                        (),
                    )
                    self._execute(
                        conn,
                        """
                        CREATE TABLE IF NOT EXISTS sync_route_checkpoint (
                            route_name TEXT PRIMARY KEY,
                            last_source_message_id INTEGER NOT NULL,
                            updated_at TEXT NOT NULL
                        )
                        """,
                        (),
                    )
                    self._execute(
                        conn,
                        """
                        CREATE TABLE IF NOT EXISTS sync_review_queue (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            dedupe_key TEXT NOT NULL,
                            route_name TEXT NOT NULL,
                            reason TEXT NOT NULL,
                            last_error TEXT,
                            payload_json TEXT,
                            created_at TEXT NOT NULL,
                            resolved_at TEXT,
                            resolution TEXT
                        )
                        """,
                        (),
                    )
                    self._execute(
                        conn,
                        "CREATE INDEX IF NOT EXISTS idx_sync_review_open ON sync_review_queue(resolved_at, id DESC)",
                        (),
                    )
                # Legacy compatibility: we no longer emit `skipped` in automatic flow.
                # Normalize historical rows so dashboards and counters stay consistent.
                self._execute(
                    conn,
                    "UPDATE sync_message_ledger SET status = 'blocked' WHERE status = 'skipped'",
                    (),
                )
                conn.commit()
            finally:
                conn.close()

    def _sql(self, query: str) -> str:
        if self.backend != "mysql":
            return query
        return query.replace("?", "%s")

    def _execute(self, conn: Any, query: str, params: tuple[Any, ...] | list[Any]) -> Any:
        sql = self._sql(query)
        cur = conn.cursor()
        cur.execute(sql, tuple(params))
        return cur

    def _fetchone(self, conn: Any, query: str, params: tuple[Any, ...] | list[Any]) -> tuple[Any, ...] | None:
        cur = self._execute(conn, query, params)
        return cur.fetchone()

    def _fetchall(self, conn: Any, query: str, params: tuple[Any, ...] | list[Any]) -> list[tuple[Any, ...]]:
        cur = self._execute(conn, query, params)
        rows = cur.fetchall()
        return list(rows or [])

    def _try_execute(self, conn: Any, query: str, params: tuple[Any, ...] | list[Any]) -> None:
        try:
            self._execute(conn, query, params)
        except Exception as exc:
            if self.backend != "mysql":
                raise
            text = str(exc).lower()
            if "duplicate key name" in text or "already exists" in text:
                return
            raise

    def _connect(self) -> Any:
        if self.backend == "mysql":
            if self.mysql_config is None:
                raise RuntimeError("missing mysql config")
            try:
                import pymysql  # type: ignore[import-not-found]
            except Exception as exc:
                raise RuntimeError("pymysql dependency is not installed") from exc

            return pymysql.connect(
                host=self.mysql_config["host"],
                port=int(self.mysql_config["port"]),
                user=self.mysql_config["user"],
                password=self.mysql_config["password"],
                database=self.mysql_config["database"],
                charset=str(self.mysql_config.get("charset") or "utf8mb4"),
                autocommit=False,
                cursorclass=pymysql.cursors.Cursor,
            )

        if self.db_path is None:
            raise RuntimeError("missing sqlite db path")
        conn = sqlite3.connect(self.db_path, timeout=20.0)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn


def _parse_mysql_dsn(dsn: str) -> dict[str, Any]:
    normalized = dsn
    if dsn.startswith("mysql+pymysql://"):
        normalized = "mysql://" + dsn[len("mysql+pymysql://") :]

    parsed = urlparse(normalized)
    if parsed.scheme != "mysql":
        raise ValueError("invalid mysql dsn scheme")

    user = unquote(parsed.username or "")
    password = unquote(parsed.password or "")
    host = parsed.hostname or "127.0.0.1"
    port = int(parsed.port or 3306)
    database = (parsed.path or "/").lstrip("/").strip()
    if not user or not database:
        raise ValueError("mysql dsn must include user and database")

    params = parse_qs(parsed.query)
    charset = str((params.get("charset") or ["utf8mb4"])[0] or "utf8mb4")
    return {
        "host": host,
        "port": port,
        "user": user,
        "password": password,
        "database": database,
        "charset": charset,
    }


def _utc_now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()
