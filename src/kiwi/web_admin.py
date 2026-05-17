from __future__ import annotations

import asyncio
import errno
import dataclasses
import json
import logging
import os
import secrets
import shutil
import socket
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from kiwi.admin_store import AdminStore
from kiwi.config import Settings
from kiwi.logging_setup import recent_logs
from kiwi.management_api import ManagementApi
from kiwi.service import KiwiService

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _Session:
    username: str
    issued_at: float
    expires_at: float


class AdminWebServer:
    def __init__(
        self,
        *,
        settings: Settings,
        service: KiwiService,
        management_api: ManagementApi,
        admin_store: AdminStore,
        event_loop: asyncio.AbstractEventLoop,
    ) -> None:
        self.settings = settings
        self.service = service
        self.management_api = management_api
        self.admin_store = admin_store
        self.event_loop = event_loop

        self._sessions: dict[str, _Session] = {}
        self._sessions_lock = threading.Lock()

        self._static_root = Path(__file__).resolve().parent / "webui"
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._httpd is not None:
            return

        handler_cls = self._build_handler_class()
        self._httpd = ThreadingHTTPServer((self.settings.admin_web_host, int(self.settings.admin_web_port)), handler_cls)
        self._httpd.daemon_threads = True
        self._httpd.admin_server = self  # type: ignore[attr-defined]

        self._thread = threading.Thread(target=self._httpd.serve_forever, name="kiwi-admin-web", daemon=True)
        self._thread.start()
        logger.info(
            "Admin web panel started",
            extra={
                "details": {
                    "host": self.settings.admin_web_host,
                    "port": int(self.settings.admin_web_port),
                    "static_root": str(self._static_root),
                }
            },
        )

    def stop(self) -> None:
        httpd = self._httpd
        if httpd is None:
            return
        try:
            httpd.shutdown()
            httpd.server_close()
        finally:
            self._httpd = None
            self._thread = None
            logger.info("Admin web panel stopped")

    def _build_handler_class(self):
        parent = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "KiwiAdmin/1.0"

            def log_message(self, format: str, *args: object) -> None:  # noqa: A003
                logger.info(
                    "admin_web_access",
                    extra={
                        "details": {
                            "client": self.client_address[0] if self.client_address else "-",
                            "method": self.command,
                            "path": self.path,
                            "status_line": format % args,
                        }
                    },
                )

            @staticmethod
            def _is_client_disconnect_error(exc: Exception) -> bool:
                if isinstance(exc, (BrokenPipeError, ConnectionResetError, socket.timeout, TimeoutError)):
                    return True
                if isinstance(exc, OSError):
                    return int(getattr(exc, "errno", -1) or -1) in {
                        errno.EPIPE,
                        errno.ECONNRESET,
                        errno.ECONNABORTED,
                    }
                return False

            def _send_response_bytes(
                self,
                data: bytes,
                *,
                status: int,
                content_type: str,
                headers: dict[str, str] | None = None,
            ) -> bool:
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    if headers:
                        for key, value in headers.items():
                            self.send_header(str(key), str(value))
                    self.end_headers()
                    self.wfile.write(data)
                    return True
                except Exception as exc:  # pragma: no cover - socket lifecycle is integration-level
                    if self._is_client_disconnect_error(exc):
                        logger.info(
                            "Admin web client disconnected before response completed",
                            extra={
                                "details": {
                                    "client": self.client_address[0] if self.client_address else "-",
                                    "method": self.command,
                                    "path": self.path,
                                }
                            },
                        )
                        return False
                    raise

            def _send_json(self, payload: object, status: int = 200, *, headers: dict[str, str] | None = None) -> None:
                data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self._send_response_bytes(
                    data,
                    status=status,
                    content_type="application/json; charset=utf-8",
                    headers=headers,
                )

            def _send_text(self, text: str, status: int = 200, *, content_type: str = "text/plain; charset=utf-8") -> None:
                data = text.encode("utf-8")
                self._send_response_bytes(data, status=status, content_type=content_type)

            def _read_json_body(self) -> dict[str, Any]:
                raw_len = self.headers.get("Content-Length", "0")
                try:
                    length = max(0, int(raw_len))
                except Exception:
                    length = 0
                if length <= 0:
                    return {}
                if length > 2 * 1024 * 1024:
                    raise ValueError("request body too large")
                raw = self.rfile.read(length)
                if not raw:
                    return {}
                parsed = json.loads(raw.decode("utf-8"))
                if not isinstance(parsed, dict):
                    raise ValueError("body must be JSON object")
                return parsed

            def _cookie_token(self) -> str | None:
                header = self.headers.get("Cookie")
                if not header:
                    return None
                jar = SimpleCookie()
                try:
                    jar.load(header)
                except Exception:
                    return None
                morsel = jar.get("kiwi_admin_session")
                if morsel is None:
                    return None
                token = str(morsel.value or "").strip()
                return token or None

            def _require_auth(self) -> str | None:
                token = self._cookie_token()
                if not token:
                    self._send_json({"ok": False, "error": "unauthorized"}, status=HTTPStatus.UNAUTHORIZED)
                    return None
                username = parent._get_session_username(token)
                if not username:
                    self._send_json({"ok": False, "error": "unauthorized"}, status=HTTPStatus.UNAUTHORIZED)
                    return None
                return username

            def _set_auth_cookie(self, token: str) -> str:
                expires = int(time.time() + int(parent.settings.admin_web_session_ttl_sec))
                return (
                    f"kiwi_admin_session={token}; Path=/; HttpOnly; SameSite=Lax; "
                    f"Max-Age={max(60, int(parent.settings.admin_web_session_ttl_sec))}; Expires={_http_date(expires)}"
                )

            def _clear_auth_cookie(self) -> str:
                return "kiwi_admin_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT"

            def do_GET(self) -> None:  # noqa: N802
                self._handle("GET")

            def do_POST(self) -> None:  # noqa: N802
                self._handle("POST")

            def do_PUT(self) -> None:  # noqa: N802
                self._handle("PUT")

            def do_DELETE(self) -> None:  # noqa: N802
                self._handle("DELETE")

            def _handle(self, method: str) -> None:
                parsed = urlparse(self.path)
                path = parsed.path or "/"
                query = parse_qs(parsed.query or "")

                try:
                    if path.startswith("/api/"):
                        self._handle_api(method, path, query)
                        return
                    self._handle_static(path)
                except ValueError as exc:
                    self._send_json(
                        {"ok": False, "error": "bad_request", "detail": str(exc)},
                        status=HTTPStatus.BAD_REQUEST,
                    )
                except FileNotFoundError as exc:
                    self._send_json(
                        {"ok": False, "error": "not_found", "detail": str(exc)},
                        status=HTTPStatus.NOT_FOUND,
                    )
                except PermissionError as exc:
                    self._send_json(
                        {"ok": False, "error": "forbidden", "detail": str(exc)},
                        status=HTTPStatus.FORBIDDEN,
                    )
                except Exception as exc:
                    if self._is_client_disconnect_error(exc):
                        logger.info(
                            "Admin web request aborted due to client disconnect",
                            extra={
                                "details": {
                                    "client": self.client_address[0] if self.client_address else "-",
                                    "method": method,
                                    "path": path,
                                }
                            },
                        )
                        return
                    logger.exception("Admin web request failed")
                    self._send_json(
                        {"ok": False, "error": "internal_error", "detail": str(exc)},
                        status=HTTPStatus.INTERNAL_SERVER_ERROR,
                    )

            def _handle_static(self, path: str) -> None:
                clean = path.strip() or "/"
                if clean in {"/", "/index.html"}:
                    file_path = parent._static_root / "index.html"
                elif clean == "/app.js":
                    file_path = parent._static_root / "app.js"
                elif clean == "/styles.css":
                    file_path = parent._static_root / "styles.css"
                else:
                    # SPA fallback
                    file_path = parent._static_root / "index.html"

                if not file_path.exists():
                    self._send_text("Admin UI not found", status=HTTPStatus.NOT_FOUND)
                    return

                body = file_path.read_bytes()
                if file_path.suffix == ".html":
                    content_type = "text/html; charset=utf-8"
                elif file_path.suffix == ".js":
                    content_type = "application/javascript; charset=utf-8"
                elif file_path.suffix == ".css":
                    content_type = "text/css; charset=utf-8"
                else:
                    content_type = "application/octet-stream"
                self._send_response_bytes(body, status=HTTPStatus.OK, content_type=content_type)

            def _handle_api(self, method: str, path: str, query: dict[str, list[str]]) -> None:
                # Public endpoints
                if path == "/api/health" and method == "GET":
                    self._send_json({"ok": True, "service": "kiwi-admin-web"})
                    return

                if path == "/api/auth/login" and method == "POST":
                    body = self._read_json_body()
                    username = str(body.get("username") or "").strip()
                    password = str(body.get("password") or "")
                    if not parent.admin_store.verify_credentials(username, password):
                        self._send_json({"ok": False, "error": "invalid_credentials"}, status=HTTPStatus.UNAUTHORIZED)
                        return
                    token = parent._create_session(username)
                    self._send_json(
                        {"ok": True, "username": username.lower()},
                        headers={"Set-Cookie": self._set_auth_cookie(token)},
                    )
                    return

                if path == "/api/auth/logout" and method == "POST":
                    token = self._cookie_token()
                    if token:
                        parent._delete_session(token)
                    self._send_json({"ok": True}, headers={"Set-Cookie": self._clear_auth_cookie()})
                    return

                if path == "/api/auth/me" and method == "GET":
                    token = self._cookie_token()
                    username = parent._get_session_username(token or "") if token else None
                    if not username:
                        self._send_json({"ok": False, "authenticated": False}, status=HTTPStatus.UNAUTHORIZED)
                        return
                    self._send_json({"ok": True, "authenticated": True, "username": username})
                    return

                username = self._require_auth()
                if not username:
                    return

                if path == "/api/dashboard" and method == "GET":
                    self._send_json({"ok": True, "data": parent._dashboard_snapshot()})
                    return

                if path == "/api/system/status" and method == "GET":
                    self._send_json({"ok": True, "data": parent._system_status()})
                    return

                if path == "/api/system/settings" and method == "GET":
                    self._send_json({"ok": True, "data": parent._settings_snapshot()})
                    return

                if path == "/api/routes" and method == "GET":
                    self._send_json({"ok": True, "routes": parent.management_api.list_routes()})
                    return

                if path == "/api/routes" and method == "POST":
                    body = self._read_json_body()
                    created = parent.management_api.add_route(body)
                    self._send_json({"ok": True, "route": created})
                    return

                if path == "/api/routes/start-all" and method == "POST":
                    out = parent.management_api.start_all_routes()
                    self._send_json({"ok": True, "result": out})
                    return

                if path == "/api/routes/stop-all" and method == "POST":
                    out = parent.management_api.stop_all_routes()
                    self._send_json({"ok": True, "result": out})
                    return

                if path.startswith("/api/routes/"):
                    route_name = unquote(path[len("/api/routes/") :])
                    if not route_name:
                        self._send_json({"ok": False, "error": "route_name_required"}, status=HTTPStatus.BAD_REQUEST)
                        return
                    if route_name.endswith("/sync/start") and method == "POST":
                        name = route_name[: -len("/sync/start")]
                        out = parent.management_api.start_route_sync(unquote(name))
                        self._send_json({"ok": True, "route": out})
                        return
                    if route_name.endswith("/sync/stop") and method == "POST":
                        name = route_name[: -len("/sync/stop")]
                        out = parent.management_api.stop_route_sync(unquote(name))
                        self._send_json({"ok": True, "route": out})
                        return
                    if route_name.endswith("/enable") and method == "POST":
                        name = route_name[: -len("/enable")]
                        out = parent.management_api.set_route_status(unquote(name), "synced")
                        self._send_json({"ok": True, "route": out})
                        return
                    if route_name.endswith("/disable") and method == "POST":
                        name = route_name[: -len("/disable")]
                        out = parent.management_api.set_route_status(unquote(name), "deactive")
                        self._send_json({"ok": True, "route": out})
                        return
                    if method == "GET":
                        out = parent.management_api.get_route(route_name)
                        self._send_json({"ok": True, "route": out})
                        return
                    if method == "PUT":
                        body = self._read_json_body()
                        out = parent.management_api.update_route(route_name, body)
                        self._send_json({"ok": True, "route": out})
                        return
                    if method == "DELETE":
                        parent.management_api.delete_route(route_name)
                        self._send_json({"ok": True})
                        return

                if path == "/api/scripts/channel" and method == "GET":
                    self._send_json({"ok": True, "files": parent.management_api.list_channel_script_files()})
                    return

                if path == "/api/scripts/guard" and method == "GET":
                    self._send_json({"ok": True, "files": parent.management_api.list_guard_files()})
                    return

                if path.startswith("/api/scripts/channel/"):
                    file_name = unquote(path[len("/api/scripts/channel/") :])
                    if method == "GET":
                        content = parent.management_api.read_channel_script_file(file_name)
                        self._send_json({"ok": True, "name": file_name, "content": content})
                        return
                    if method == "PUT":
                        body = self._read_json_body()
                        content = str(body.get("content") or "")
                        result = parent.management_api.write_channel_script_file(file_name, content)
                        self._send_json({"ok": True, "file": result})
                        return

                if path.startswith("/api/scripts/guard/"):
                    file_name = unquote(path[len("/api/scripts/guard/") :])
                    if method == "GET":
                        content = parent.management_api.read_guard_script_file(file_name)
                        self._send_json({"ok": True, "name": file_name, "content": content})
                        return
                    if method == "PUT":
                        body = self._read_json_body()
                        content = str(body.get("content") or "")
                        result = parent.management_api.write_guard_script_file(file_name, content)
                        self._send_json({"ok": True, "file": result})
                        return

                if path == "/api/keyword-links" and method == "GET":
                    links = parent.management_api.get_keyword_links()
                    self._send_json({"ok": True, "links": links})
                    return

                if path == "/api/keyword-links" and method == "PUT":
                    body = self._read_json_body()
                    links = body.get("links")
                    if not isinstance(links, list):
                        raise ValueError("links must be list")
                    result = parent.management_api.save_keyword_links(links)
                    self._send_json({"ok": True, "result": result})
                    return

                if path == "/api/sync/stats" and method == "GET":
                    try:
                        stats = parent._run_async(parent.management_api.sync_stats(), timeout=12.0)
                    except Exception:
                        logger.exception("Failed to fetch async sync stats; fallback to snapshot")
                        stats = parent.management_api.sync_stats_snapshot()
                    self._send_json({"ok": True, "stats": stats})
                    return

                if path == "/api/traffic/stats" and method == "GET":
                    self._send_json({"ok": True, "traffic": parent.service.traffic_snapshot()})
                    return

                if path == "/api/workers/status" and method == "GET":
                    self._send_json({"ok": True, "workers": parent._workers_status_snapshot()})
                    return

                if path == "/api/sync/reviews" and method == "GET":
                    limit = _q_int(query, "limit", 50, min_value=1, max_value=500)
                    only_open = _q_bool(query, "only_open", True)
                    reviews = parent.management_api.sync_review_list(limit=limit, only_open=only_open)
                    self._send_json({"ok": True, "reviews": reviews})
                    return

                if path.startswith("/api/sync/reviews/") and method == "POST":
                    parts = path.split("/")
                    if len(parts) != 6:
                        self._send_json({"ok": False, "error": "invalid_review_action_path"}, status=HTTPStatus.BAD_REQUEST)
                        return
                    review_id = int(parts[4])
                    action = parts[5]
                    if action == "retry":
                        out = parent.management_api.sync_review_retry(review_id)
                        self._send_json({"ok": True, "result": out})
                        return
                    if action == "skip":
                        out = parent.management_api.sync_review_skip(review_id)
                        self._send_json({"ok": True, "result": out})
                        return

                if path == "/api/logs" and method == "GET":
                    limit = _q_int(query, "limit", 200, min_value=1, max_value=2000)
                    level = _q_str(query, "level")
                    logger_name = _q_str(query, "logger")
                    message_contains = _q_str(query, "message")
                    items = recent_logs(
                        limit=limit,
                        level=level,
                        logger_name=logger_name,
                        message_contains=message_contains,
                    )
                    self._send_json({"ok": True, "logs": items})
                    return

                if path == "/api/storage/summary" and method == "GET":
                    self._send_json({"ok": True, "summary": parent._storage_summary()})
                    return

                if path == "/api/storage/runs" and method == "GET":
                    limit = _q_int(query, "limit", 60, min_value=1, max_value=500)
                    self._send_json({"ok": True, "runs": parent._recent_runs(limit=limit)})
                    return

                if path == "/api/storage/file" and method == "GET":
                    rel_path = _q_str(query, "path")
                    if not rel_path:
                        self._send_json({"ok": False, "error": "path_query_required"}, status=HTTPStatus.BAD_REQUEST)
                        return
                    payload = parent._read_storage_file(rel_path)
                    self._send_json({"ok": True, "file": payload})
                    return

                if path == "/api/admin/users" and method == "GET":
                    self._send_json({"ok": True, "users": parent.admin_store.list_admins()})
                    return

                if path == "/api/admin/users" and method == "POST":
                    body = self._read_json_body()
                    user = str(body.get("username") or "").strip()
                    pwd = str(body.get("password") or "")
                    parent.admin_store.add_admin(user, pwd)
                    self._send_json({"ok": True})
                    return

                if path.startswith("/api/admin/users/") and method == "DELETE":
                    user = unquote(path[len("/api/admin/users/") :])
                    parent.admin_store.remove_admin(user)
                    self._send_json({"ok": True})
                    return

                if path == "/api/actions/reload-routes" and method == "POST":
                    parent.management_api.reload_routes()
                    self._send_json({"ok": True})
                    return

                if path == "/api/actions/stop-service" and method == "POST":
                    parent._run_async(parent.service.stop(), timeout=10.0)
                    self._send_json({"ok": True})
                    return

                self._send_json({"ok": False, "error": "not_found"}, status=HTTPStatus.NOT_FOUND)

        return Handler

    def _create_session(self, username: str) -> str:
        now = time.time()
        token = secrets.token_urlsafe(36)
        with self._sessions_lock:
            self._sessions[token] = _Session(
                username=str(username or "").strip().lower(),
                issued_at=now,
                expires_at=now + max(300, int(self.settings.admin_web_session_ttl_sec)),
            )
            self._purge_expired_sessions(now)
        return token

    def _delete_session(self, token: str) -> None:
        with self._sessions_lock:
            self._sessions.pop(str(token or ""), None)

    def _get_session_username(self, token: str) -> str | None:
        key = str(token or "").strip()
        if not key:
            return None
        now = time.time()
        with self._sessions_lock:
            sess = self._sessions.get(key)
            if sess is None:
                return None
            if float(sess.expires_at) <= now:
                self._sessions.pop(key, None)
                return None
            return sess.username

    def _purge_expired_sessions(self, now: float | None = None) -> None:
        ref = float(now if now is not None else time.time())
        stale = [token for token, sess in self._sessions.items() if float(sess.expires_at) <= ref]
        for token in stale:
            self._sessions.pop(token, None)

    def _run_async(self, awaitable, *, timeout: float = 10.0):
        fut = asyncio.run_coroutine_threadsafe(awaitable, self.event_loop)
        try:
            return fut.result(timeout=max(1.0, float(timeout)))
        except FutureTimeoutError:
            fut.cancel()
            raise

    def _dashboard_snapshot(self) -> dict[str, object]:
        routes = self.management_api.list_routes()
        try:
            sync_snapshot = self._run_async(self.management_api.sync_stats(), timeout=12.0)
        except Exception:
            logger.exception("Failed to fetch async sync stats for dashboard; fallback to snapshot")
            sync_snapshot = self.management_api.sync_stats_snapshot()
        status_counts = dict(sync_snapshot.get("status_counts") or {})
        route_status_counts = {"deactive": 0, "syncing": 0, "synced": 0}
        for route in routes:
            status = str(route.get("status") or "").strip().lower()
            if status in route_status_counts:
                route_status_counts[status] += 1
        return {
            "service": self.service.runtime_snapshot(),
            "routes": {
                "total": len(routes),
                "deactive": int(route_status_counts["deactive"]),
                "syncing": int(route_status_counts["syncing"]),
                "synced": int(route_status_counts["synced"]),
            },
            "sync": sync_snapshot,
            "status_counts": status_counts,
            "scripts": {
                "channel": self.management_api.list_channel_script_files(),
                "guard": self.management_api.list_guard_files(),
            },
            "storage": self._storage_summary(),
            "health": self._system_status(),
        }

    def _storage_summary(self) -> dict[str, object]:
        root = Path(self.settings.storage_dir)
        messages_root = root / "messages"
        channel_dirs = 0
        run_dirs = 0
        input_files = 0
        output_files = 0
        payload_files = 0
        raw_files = 0
        total_size = 0

        if messages_root.exists():
            for channel_dir in messages_root.iterdir():
                if not channel_dir.is_dir():
                    continue
                channel_dirs += 1
                for run_dir in channel_dir.iterdir():
                    if not run_dir.is_dir():
                        continue
                    run_dirs += 1
                    for file in run_dir.rglob("*"):
                        if not file.is_file():
                            continue
                        try:
                            total_size += int(file.stat().st_size)
                        except Exception:
                            pass
                        name = file.name
                        raw = str(file)
                        if name == "payload.json":
                            payload_files += 1
                        elif name == "raw_update.json":
                            raw_files += 1
                        elif "/input/" in raw:
                            input_files += 1
                        elif "/output/" in raw:
                            output_files += 1

        return {
            "storage_root": str(root),
            "messages_root": str(messages_root),
            "channels": channel_dirs,
            "runs": run_dirs,
            "payload_files": payload_files,
            "raw_files": raw_files,
            "input_files": input_files,
            "output_files": output_files,
            "total_size_bytes": total_size,
        }

    def _recent_runs(self, *, limit: int) -> list[dict[str, object]]:
        messages_root = Path(self.settings.storage_dir) / "messages"
        if not messages_root.exists():
            return []

        items: list[tuple[float, Path, str, str]] = []
        for channel_dir in messages_root.iterdir():
            if not channel_dir.is_dir():
                continue
            channel_id = channel_dir.name
            for run_dir in channel_dir.iterdir():
                if not run_dir.is_dir():
                    continue
                try:
                    ts = float(run_dir.stat().st_mtime)
                except Exception:
                    ts = 0.0
                items.append((ts, run_dir, channel_id, run_dir.name))

        items.sort(key=lambda x: x[0], reverse=True)
        out: list[dict[str, object]] = []
        for ts, run_dir, channel_id, run_id in items[: max(1, int(limit))]:
            payload_route = None
            msg_id = None
            payload_file = run_dir / "payload.json"
            if payload_file.exists():
                try:
                    payload = json.loads(payload_file.read_text(encoding="utf-8"))
                    if isinstance(payload, dict):
                        route_obj = payload.get("route")
                        message_obj = payload.get("message")
                        if isinstance(route_obj, dict):
                            payload_route = route_obj.get("name")
                        if isinstance(message_obj, dict):
                            msg_id = message_obj.get("message_id")
                except Exception:
                    pass

            rel = f"messages/{channel_id}/{run_id}"
            out.append(
                {
                    "channel_id": channel_id,
                    "run_id": run_id,
                    "route": payload_route,
                    "message_id": msg_id,
                    "updated_at": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
                    "path": rel,
                    "files": {
                        "raw_update": (run_dir / "raw_update.json").exists(),
                        "payload": payload_file.exists(),
                        "channel_messages": (run_dir / "output" / "channel_messages.json").exists(),
                    },
                }
            )
        return out

    def _read_storage_file(self, rel_path: str) -> dict[str, object]:
        clean = str(rel_path or "").strip().lstrip("/")
        if not clean:
            raise ValueError("path is empty")
        root = Path(self.settings.storage_dir).resolve()
        target = (root / clean).resolve()
        if root not in target.parents and target != root:
            raise ValueError("path escapes storage root")
        if not target.exists() or not target.is_file():
            raise ValueError("file not found")
        if target.suffix.lower() != ".json":
            raise ValueError("only json files are viewable")
        raw = target.read_text(encoding="utf-8")
        preview = raw[:200_000]
        return {
            "path": str(target.relative_to(root)),
            "size_bytes": target.stat().st_size,
            "preview": preview,
            "truncated": len(raw) > len(preview),
        }

    def _settings_snapshot(self) -> dict[str, object]:
        data = dataclasses.asdict(self.settings)
        hidden_markers = {"token", "password", "secret", "hash", "dsn"}
        sanitized: dict[str, object] = {}
        for key, value in data.items():
            key_l = str(key).lower()
            if any(marker in key_l for marker in hidden_markers):
                sanitized[key] = "***"
                continue
            sanitized[key] = value
        return sanitized

    def _system_status(self) -> dict[str, object]:
        checks = {
            "storage": self._check_storage_writable(),
            "redis": self._check_redis(),
            "ledger_db": self._check_ledger_db(),
            "gemini_proxy": self._check_gemini_proxy(),
            "telethon_session": self._check_telethon_session(),
        }
        summary = "ok"
        if any(str(v.get("status")) == "error" for v in checks.values()):
            summary = "error"
        elif any(str(v.get("status")) == "warn" for v in checks.values()):
            summary = "warn"

        return {
            "summary": summary,
            "runtime": self.service.runtime_snapshot(),
            "checks": checks,
        }

    def _workers_status_snapshot(self) -> dict[str, object]:
        routes = self.management_api.list_routes()
        syncing_routes = 0
        for route in routes:
            if str(route.get("status") or "").strip().lower() == "syncing":
                syncing_routes += 1

        try:
            sync_stats = self._run_async(self.management_api.sync_stats(), timeout=12.0)
        except Exception:
            logger.exception("Failed to fetch async sync stats for workers page; fallback to snapshot")
            sync_stats = self.management_api.sync_stats_snapshot()

        status_counts = dict(sync_stats.get("status_counts") or {})
        processing = int(status_counts.get("processing", 0) or 0)
        queued = int(status_counts.get("queued", 0) or 0)
        failed = int(status_counts.get("failed", 0) or 0)
        ambiguous = int(status_counts.get("ambiguous", 0) or 0)
        blocked = int(status_counts.get("blocked", 0) or 0)
        sent = int(status_counts.get("sent", 0) or 0)
        skipped = int(status_counts.get("skipped", 0) or 0)
        configured_workers = int(self.settings.sync_worker_count)

        return {
            "workers": {
                "configured_count": configured_workers,
                "queue_backend": str(self.settings.sync_queue_backend or ""),
                "route_max_inflight": int(self.settings.sync_route_max_inflight),
                "retry_base_sec": float(self.settings.sync_retry_base_sec),
                "lock_ttl_sec": int(self.settings.sync_lock_ttl_sec),
                "drain_task_running": bool(self.service.runtime_snapshot().get("sync_drain_task_running")),
                "estimated_busy_workers": max(0, min(configured_workers, processing)),
            },
            "sync": {
                "queue_depth": int(sync_stats.get("queue_depth", 0) or 0),
                "open_reviews": int(sync_stats.get("open_reviews", 0) or 0),
                "syncing_routes": int(syncing_routes),
                "status_counts": {
                    "queued": queued,
                    "processing": processing,
                    "failed": failed,
                    "ambiguous": ambiguous,
                    "blocked": blocked,
                    "sent": sent,
                    "skipped": skipped,
                },
            },
            "host": self._host_metrics_snapshot(),
            "service": self.service.runtime_snapshot(),
        }

    def _host_metrics_snapshot(self) -> dict[str, object]:
        cpu_count = int(os.cpu_count() or 0)
        load_avg: dict[str, float] = {}
        try:
            l1, l5, l15 = os.getloadavg()
            load_avg = {
                "load1": round(float(l1), 3),
                "load5": round(float(l5), 3),
                "load15": round(float(l15), 3),
            }
        except Exception:
            load_avg = {}

        load_pct_1m = None
        if cpu_count > 0 and "load1" in load_avg:
            load_pct_1m = round((float(load_avg["load1"]) / float(cpu_count)) * 100.0, 2)

        mem = self._read_meminfo_snapshot()
        cgroup_mem = self._read_cgroup_memory_snapshot()
        disk = self._read_disk_snapshot()
        uptime_sec = self._read_uptime_sec()

        return {
            "cpu": {
                "cores": cpu_count,
                "load_avg": load_avg,
                "load_pct_1m_per_core": load_pct_1m,
            },
            "memory": mem,
            "cgroup_memory": cgroup_mem,
            "disk": disk,
            "uptime_sec": uptime_sec,
        }

    def _read_meminfo_snapshot(self) -> dict[str, object]:
        meminfo_path = Path("/proc/meminfo")
        values: dict[str, int] = {}
        try:
            for line in meminfo_path.read_text(encoding="utf-8").splitlines():
                if ":" not in line:
                    continue
                key, raw_value = line.split(":", 1)
                parts = raw_value.strip().split()
                if not parts:
                    continue
                num = int(parts[0])
                unit = parts[1].lower() if len(parts) > 1 else "kb"
                factor = 1024 if unit == "kb" else 1
                values[key.strip()] = num * factor
        except Exception as exc:
            return {"error": str(exc)}

        total = int(values.get("MemTotal", 0) or 0)
        available = int(values.get("MemAvailable", values.get("MemFree", 0)) or 0)
        used = max(0, total - available)
        used_pct = round((used / total) * 100.0, 2) if total > 0 else None

        swap_total = int(values.get("SwapTotal", 0) or 0)
        swap_free = int(values.get("SwapFree", 0) or 0)
        swap_used = max(0, swap_total - swap_free)
        swap_used_pct = round((swap_used / swap_total) * 100.0, 2) if swap_total > 0 else None

        return {
            "total_bytes": total,
            "available_bytes": available,
            "used_bytes": used,
            "used_pct": used_pct,
            "swap_total_bytes": swap_total,
            "swap_used_bytes": swap_used,
            "swap_used_pct": swap_used_pct,
        }

    def _read_cgroup_memory_snapshot(self) -> dict[str, object]:
        # cgroup v2
        current_v2 = Path("/sys/fs/cgroup/memory.current")
        max_v2 = Path("/sys/fs/cgroup/memory.max")
        if current_v2.exists() and max_v2.exists():
            try:
                current = int((current_v2.read_text(encoding="utf-8").strip() or "0"))
                raw_max = max_v2.read_text(encoding="utf-8").strip()
                if raw_max == "max":
                    limit = None
                    used_pct = None
                else:
                    limit = int(raw_max or "0")
                    used_pct = round((current / limit) * 100.0, 2) if limit > 0 else None
                return {
                    "current_bytes": current,
                    "limit_bytes": limit,
                    "used_pct": used_pct,
                    "version": "v2",
                }
            except Exception as exc:
                return {"version": "v2", "error": str(exc)}

        # cgroup v1 fallback
        current_v1 = Path("/sys/fs/cgroup/memory/memory.usage_in_bytes")
        max_v1 = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")
        if current_v1.exists() and max_v1.exists():
            try:
                current = int((current_v1.read_text(encoding="utf-8").strip() or "0"))
                limit = int((max_v1.read_text(encoding="utf-8").strip() or "0"))
                used_pct = round((current / limit) * 100.0, 2) if limit > 0 else None
                return {
                    "current_bytes": current,
                    "limit_bytes": limit,
                    "used_pct": used_pct,
                    "version": "v1",
                }
            except Exception as exc:
                return {"version": "v1", "error": str(exc)}

        return {"status": "unavailable"}

    def _read_disk_snapshot(self) -> dict[str, object]:
        try:
            usage = shutil.disk_usage(Path(self.settings.storage_dir))
            used = int(usage.used)
            total = int(usage.total)
            used_pct = round((used / total) * 100.0, 2) if total > 0 else None
            return {
                "storage_dir": str(self.settings.storage_dir),
                "total_bytes": total,
                "used_bytes": used,
                "free_bytes": int(usage.free),
                "used_pct": used_pct,
            }
        except Exception as exc:
            return {"storage_dir": str(self.settings.storage_dir), "error": str(exc)}

    def _read_uptime_sec(self) -> float | None:
        try:
            raw = Path("/proc/uptime").read_text(encoding="utf-8").strip().split()
            if not raw:
                return None
            return round(float(raw[0]), 2)
        except Exception:
            return None

    def _check_storage_writable(self) -> dict[str, object]:
        root = Path(self.settings.storage_dir)
        probe = root / ".kiwi_web_probe"
        try:
            root.mkdir(parents=True, exist_ok=True)
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            return {"status": "ok", "path": str(root)}
        except Exception as exc:
            return {"status": "error", "path": str(root), "error": str(exc)}

    def _check_redis(self) -> dict[str, object]:
        redis_url = str(self.settings.redis_url or "").strip()
        if not redis_url:
            return {"status": "warn", "detail": "REDIS_URL is empty"}
        parsed = urlparse(redis_url)
        host = parsed.hostname or "127.0.0.1"
        port = int(parsed.port or 6379)
        try:
            with socket.create_connection((host, port), timeout=1.5):
                return {"status": "ok", "host": host, "port": port}
        except Exception as exc:
            return {"status": "error", "host": host, "port": port, "error": str(exc)}

    def _check_ledger_db(self) -> dict[str, object]:
        ledger = self.service.sync_ledger
        backend = str(getattr(ledger, "backend", "unknown"))
        try:
            conn = ledger._connect()  # noqa: SLF001
            try:
                cur = conn.cursor()
                cur.execute("SELECT 1")
                cur.fetchone()
            finally:
                conn.close()
            return {"status": "ok", "backend": backend}
        except Exception as exc:
            return {"status": "error", "backend": backend, "error": str(exc)}

    def _check_gemini_proxy(self) -> dict[str, object]:
        endpoint = (
            os.getenv("CHANNEL_SCRIPT_AI_ENDPOINT", "").strip()
            or os.getenv("GUARD_AI_ENDPOINT", "").strip()
            or ""
        )
        if not endpoint:
            return {"status": "warn", "detail": "AI endpoint not configured"}

        parsed = urlparse(endpoint)
        if not parsed.scheme or not parsed.netloc:
            return {"status": "warn", "endpoint": endpoint, "detail": "invalid endpoint"}

        health_url = f"{parsed.scheme}://{parsed.netloc}/health"
        try:
            import urllib.request

            req = urllib.request.Request(health_url, method="GET")
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                code = int(getattr(resp, "status", 200) or 200)
            if code >= 400:
                return {"status": "warn", "endpoint": health_url, "http_status": code}
            return {"status": "ok", "endpoint": health_url, "http_status": code}
        except Exception as exc:
            return {"status": "error", "endpoint": health_url, "error": str(exc)}

    def _check_telethon_session(self) -> dict[str, object]:
        path = Path(self.settings.telethon_session_path)
        if not self.settings.telethon_enabled:
            return {"status": "warn", "detail": "telethon disabled", "path": str(path)}
        if not path.exists():
            return {"status": "error", "detail": "session file missing", "path": str(path)}
        return {
            "status": "ok",
            "path": str(path),
            "size_bytes": path.stat().st_size,
            "mtime": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(),
        }



def _q_str(query: dict[str, list[str]], key: str) -> str | None:
    values = query.get(key) or []
    if not values:
        return None
    text = str(values[0] or "").strip()
    return text or None



def _q_int(query: dict[str, list[str]], key: str, default: int, *, min_value: int, max_value: int) -> int:
    raw = _q_str(query, key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except Exception:
        value = default
    return max(min_value, min(max_value, value))



def _q_bool(query: dict[str, list[str]], key: str, default: bool) -> bool:
    raw = _q_str(query, key)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}



def _http_date(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")
