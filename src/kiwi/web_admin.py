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
import ssl
import threading
import time
import contextlib
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse, urlencode

try:  # optional at import-time; required for realtime websocket server
    from websockets.legacy.server import WebSocketServerProtocol, serve as ws_serve
except Exception:  # pragma: no cover - dependency may be missing in limited test envs
    WebSocketServerProtocol = Any  # type: ignore[misc,assignment]
    ws_serve = None  # type: ignore[assignment]

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


@dataclass(slots=True)
class _RealtimeTicket:
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
        self._realtime_tickets: dict[str, _RealtimeTicket] = {}
        self._realtime_tickets_lock = threading.Lock()

        self._static_root = Path(__file__).resolve().parent / "webui"
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._ws_thread: threading.Thread | None = None
        self._ws_loop: asyncio.AbstractEventLoop | None = None
        self._ws_shutdown = threading.Event()
        self._ws_ready = threading.Event()
        self._ws_port = int(self.settings.admin_ws_port) if int(self.settings.admin_ws_port or 0) > 0 else int(self.settings.admin_web_port) + 1
        self._ws_ssl_context: ssl.SSLContext | None = None
        self._ws_tls_enabled = False

    def start(self) -> None:
        if self._httpd is not None:
            return

        self._start_ws_server()
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
                    "websocket_port": int(self._ws_port),
                    "websocket_tls": bool(self._ws_tls_enabled),
                    "static_root": str(self._static_root),
                }
            },
        )

    def stop(self) -> None:
        httpd = self._httpd
        try:
            if httpd is not None:
                httpd.shutdown()
                httpd.server_close()
        finally:
            self._httpd = None
            self._thread = None
            self._stop_ws_server()
            logger.info("Admin web panel stopped")

    def _start_ws_server(self) -> None:
        if self._ws_thread is not None:
            return
        self._ws_ssl_context = self._build_ws_ssl_context()
        self._ws_tls_enabled = self._ws_ssl_context is not None
        self._ws_shutdown.clear()
        self._ws_ready.clear()
        self._ws_thread = threading.Thread(target=self._ws_thread_main, name="kiwi-admin-ws", daemon=True)
        self._ws_thread.start()
        self._ws_ready.wait(timeout=2.5)

    def _stop_ws_server(self) -> None:
        self._ws_shutdown.set()
        loop = self._ws_loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(lambda: None)
            except Exception:
                pass
        thread = self._ws_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.5)
        self._ws_thread = None
        self._ws_loop = None
        self._ws_ssl_context = None
        self._ws_tls_enabled = False
        self._ws_ready.clear()

    def _build_ws_ssl_context(self) -> ssl.SSLContext | None:
        cert_path = str(self.settings.admin_ws_tls_cert_path or "").strip()
        key_path = str(self.settings.admin_ws_tls_key_path or "").strip()
        if not cert_path or not key_path:
            return None
        cert_file = Path(cert_path)
        key_file = Path(key_path)
        if not cert_file.exists() or not key_file.exists():
            logger.warning(
                "Websocket TLS cert/key not found; starting without TLS",
                extra={"details": {"cert_path": cert_path, "key_path": key_path}},
            )
            return None
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(str(cert_file), str(key_file))
            return context
        except Exception:
            logger.exception(
                "Failed to load websocket TLS cert/key; starting without TLS",
                extra={"details": {"cert_path": cert_path, "key_path": key_path}},
            )
            return None

    def _ws_thread_main(self) -> None:
        loop = asyncio.new_event_loop()
        self._ws_loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._ws_main())
        except Exception:
            logger.exception("Admin websocket server crashed")
        finally:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                with contextlib.suppress(Exception):
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            with contextlib.suppress(Exception):
                loop.run_until_complete(loop.shutdown_asyncgens())
            loop.close()

    async def _ws_main(self) -> None:
        if ws_serve is None:
            logger.warning("websockets dependency is missing; realtime websocket server disabled")
            self._ws_ready.set()
            while not self._ws_shutdown.is_set():
                await asyncio.sleep(0.5)
            return

        clients: dict[WebSocketServerProtocol, set[str]] = {}
        clients_lock = asyncio.Lock()

        async def send_json(client: WebSocketServerProtocol, payload: dict[str, Any]) -> None:
            try:
                await client.send(json.dumps(payload, ensure_ascii=False))
            except Exception:
                with contextlib.suppress(Exception):
                    await client.close()

        async def broadcast(payload: dict[str, Any], *, scopes: set[str] | None = None) -> None:
            if not clients:
                return
            target_scopes = {str(s).strip() for s in (scopes or set()) if str(s).strip()}
            async with clients_lock:
                if not target_scopes:
                    targets = list(clients.keys())
                else:
                    targets = [
                        ws
                        for ws, ws_scopes in clients.items()
                        if ws_scopes.intersection(target_scopes) or "*" in ws_scopes
                    ]
            if not targets:
                return
            await asyncio.gather(*(send_json(client, payload) for client in targets), return_exceptions=True)

        def parse_scopes(query: dict[str, list[str]]) -> set[str]:
            out: set[str] = set()
            scope_one = _q_str(query, "scope")
            if scope_one:
                out.add(scope_one)
            scopes_raw = _q_str(query, "scopes")
            if scopes_raw:
                for item in str(scopes_raw).split(","):
                    token = str(item).strip()
                    if token:
                        out.add(token)
            if not out:
                out.add("dashboard_cards")
            return out

        async def auth_and_register(websocket: WebSocketServerProtocol) -> tuple[bool, str | None, set[str]]:
            parsed = urlparse(str(getattr(websocket, "path", "") or ""))
            if parsed.path != "/ws":
                with contextlib.suppress(Exception):
                    await websocket.close(code=1008, reason="invalid_path")
                return False, None, set()
            query = parse_qs(parsed.query or "")
            ticket = _q_str(query, "ticket")
            username = self._validate_realtime_ticket(ticket or "")
            if not username:
                with contextlib.suppress(Exception):
                    await websocket.close(code=1008, reason="unauthorized")
                return False, None, set()
            scopes = parse_scopes(query)
            async with clients_lock:
                clients[websocket] = scopes
            return True, username, scopes

        async def unregister(websocket: WebSocketServerProtocol) -> None:
            async with clients_lock:
                clients.pop(websocket, None)

        async def ws_handler(websocket: WebSocketServerProtocol) -> None:
            ok, username, scopes = await auth_and_register(websocket)
            if not ok:
                return
            latest_seq = int(self.service.message_monitor_events(after_seq=0, limit=1).get("latest_seq") or 0)
            await send_json(
                websocket,
                {
                    "type": "hello",
                    "latest_seq": latest_seq,
                    "username": username,
                    "scopes": sorted(scopes),
                    "server_time": datetime.now(tz=timezone.utc).isoformat(),
                },
            )
            try:
                async for raw in websocket:
                    try:
                        data = json.loads(str(raw or "{}"))
                    except Exception:
                        continue
                    if not isinstance(data, dict):
                        continue
                    msg_type = str(data.get("type") or "").strip().lower()
                    if msg_type != "subscribe":
                        continue
                    raw_scopes = data.get("scopes")
                    if not isinstance(raw_scopes, list):
                        continue
                    normalized = {str(item).strip() for item in raw_scopes if str(item).strip()}
                    if not normalized:
                        continue
                    async with clients_lock:
                        if websocket in clients:
                            clients[websocket] = normalized
                    await send_json(websocket, {"type": "subscribed", "scopes": sorted(normalized)})
            except Exception:
                pass
            finally:
                await unregister(websocket)

        async def push_loop() -> None:
            last_seq = 0
            next_hint_at = time.time()
            hint_scopes = {
                "dashboard_cards",
                "routes_table",
                "keywords_table",
                "sync_reviews_table",
                "monitor_messages_table",
                "logs_table",
                "traffic_runs_table",
                "traffic_routes_table",
                "workers_status_table",
                "storage_runs_table",
                "system_checks_table",
                "admins_table",
                "scripts_meta",
            }
            monitor_scopes = {
                "monitor_messages_table",
                "sync_reviews_table",
                "workers_status_table",
                "dashboard_cards",
                "routes_table",
                "traffic_runs_table",
                "traffic_routes_table",
                "storage_runs_table",
            }
            while not self._ws_shutdown.is_set():
                try:
                    payload = self.service.message_monitor_events(after_seq=last_seq, limit=500)
                    events = payload.get("events") if isinstance(payload, dict) else []
                    latest_seq = int(payload.get("latest_seq") or 0) if isinstance(payload, dict) else last_seq
                    if isinstance(events, list) and events:
                        await broadcast(
                            {
                                "type": "monitor_events",
                                "events": events,
                                "latest_seq": latest_seq,
                                "scopes": sorted(monitor_scopes),
                            },
                            scopes=monitor_scopes,
                        )
                    if latest_seq > last_seq:
                        last_seq = latest_seq
                    now = time.time()
                    if now >= next_hint_at:
                        await broadcast(
                            {
                                "type": "refresh_hint",
                                "scopes": sorted(hint_scopes),
                                "ts": datetime.now(tz=timezone.utc).isoformat(),
                            },
                            scopes=hint_scopes,
                        )
                        next_hint_at = now + 2.0
                except Exception:
                    logger.exception("Admin websocket broadcaster loop failed")
                await asyncio.sleep(0.45)

        host = "0.0.0.0"
        async with ws_serve(
            ws_handler,
            host,
            int(self._ws_port),
            ping_interval=25,
            ping_timeout=None,
            max_size=2_000_000,
            ssl=self._ws_ssl_context,
        ):
            self._ws_ready.set()
            logger.info(
                "Admin websocket server started",
                extra={"details": {"host": host, "port": int(self._ws_port), "tls": bool(self._ws_ssl_context)}},
            )
            task = asyncio.create_task(push_loop())
            try:
                while not self._ws_shutdown.is_set():
                    await asyncio.sleep(0.4)
            finally:
                task.cancel()
                with contextlib.suppress(Exception):
                    await task
                async with clients_lock:
                    targets = list(clients.keys())
                    clients.clear()
                for client in targets:
                    with contextlib.suppress(Exception):
                        await client.close(code=1001, reason="server_shutdown")

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

                if path == "/api/realtime/config" and method == "GET":
                    self._send_json({"ok": True, **parent._realtime_config(username=username, request_headers=self.headers)})
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
                    self._send_json({"ok": True, "routes": parent._routes_with_runtime_status()})
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
                        out = parent._run_async(parent.management_api.start_route_sync_async(unquote(name)), timeout=20.0)
                        self._send_json({"ok": True, "route": out})
                        return
                    if route_name.endswith("/sync/force") and method == "POST":
                        name = unquote(route_name[: -len("/sync/force")])
                        force_limit = _q_int(query, "limit", 0, min_value=0, max_value=200000)
                        force_backfill_timeout_sec = _q_int(
                            query,
                            "timeout_sec",
                            int(os.getenv("WEB_FORCE_SYNC_BACKFILL_TIMEOUT_SEC", "180") or 180),
                            min_value=20,
                            max_value=1800,
                        )
                        out = parent._run_async(parent.management_api.force_route_sync(name), timeout=20.0)
                        runtime_reset = parent._run_async(parent.service.force_sync_route_runtime_reset(name), timeout=8.0)
                        backfill = parent._run_async(
                            parent.service.force_sync_route_backfill(
                                name,
                                limit=(None if force_limit <= 0 else int(force_limit)),
                            ),
                            timeout=float(force_backfill_timeout_sec),
                        )
                        self._send_json({"ok": True, **out, "runtime_reset": runtime_reset, "backfill": backfill})
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

                if path == "/api/monitor/messages" and method == "GET":
                    limit = _q_int(query, "limit", 200, min_value=1, max_value=2000)
                    route_name = _q_str(query, "route")
                    status = _q_str(query, "status")
                    search = _q_str(query, "search")
                    active_only = _q_bool(query, "active_only", False)
                    payload = parent.service.message_monitor_snapshot(
                        limit=limit,
                        route_name=route_name,
                        status=status,
                        active_only=active_only,
                        search=search,
                    )
                    self._send_json({"ok": True, **payload})
                    return

                if path == "/api/monitor/events" and method == "GET":
                    after_seq = _q_int(query, "after_seq", 0, min_value=0, max_value=10_000_000)
                    limit = _q_int(query, "limit", 200, min_value=1, max_value=2000)
                    payload = parent.service.message_monitor_events(after_seq=after_seq, limit=limit)
                    self._send_json({"ok": True, **payload})
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

    def _create_realtime_ticket(self, username: str) -> str:
        now = time.time()
        token = secrets.token_urlsafe(28)
        ttl_sec = min(300, max(45, int(self.settings.admin_web_session_ttl_sec // 4)))
        with self._realtime_tickets_lock:
            self._realtime_tickets[token] = _RealtimeTicket(
                username=str(username or "").strip().lower(),
                issued_at=now,
                expires_at=now + ttl_sec,
            )
            self._purge_expired_realtime_tickets(now)
        return token

    def _validate_realtime_ticket(self, token: str) -> str | None:
        key = str(token or "").strip()
        if not key:
            return None
        now = time.time()
        with self._realtime_tickets_lock:
            item = self._realtime_tickets.get(key)
            if item is None:
                return None
            if float(item.expires_at) <= now:
                self._realtime_tickets.pop(key, None)
                return None
            return item.username

    def _purge_expired_realtime_tickets(self, now: float | None = None) -> None:
        ref = float(now if now is not None else time.time())
        stale = [token for token, item in self._realtime_tickets.items() if float(item.expires_at) <= ref]
        for token in stale:
            self._realtime_tickets.pop(token, None)

    def _realtime_config(self, *, username: str, request_headers: Any | None = None) -> dict[str, object]:
        ticket = self._create_realtime_ticket(username)
        proto = "http"
        host_header = ""
        if request_headers is not None:
            try:
                host_header = str(request_headers.get("x-forwarded-host") or request_headers.get("host") or "").strip()
            except Exception:
                host_header = ""
            try:
                raw_proto = str(request_headers.get("x-forwarded-proto") or "").strip().lower()
                if raw_proto:
                    proto = raw_proto.split(",", 1)[0].strip().lower()
            except Exception:
                proto = "http"
        if proto not in {"http", "https"}:
            proto = "http"
        ws_scheme_same_origin = "wss" if proto == "https" else "ws"
        ws_scheme_direct = "wss" if bool(self._ws_tls_enabled) else "ws"

        def _format_host(raw: str) -> str:
            text = str(raw or "").strip()
            if not text:
                return text
            if text.startswith("[") and text.endswith("]"):
                return text
            if ":" in text:
                return f"[{text}]"
            return text

        def _with_ticket(base_url: str) -> str:
            raw = str(base_url or "").strip()
            if not raw:
                return ""
            parsed = urlparse(raw)
            current_q = parse_qs(parsed.query or "")
            current_q["ticket"] = [ticket]
            query = urlencode(current_q, doseq=True)
            rebuilt = parsed._replace(query=query)
            return rebuilt.geturl()

        host_only = ""
        host_without_port = ""
        if host_header:
            parsed = urlparse(f"//{host_header}")
            host_only = str(parsed.netloc or host_header).strip()
            host_without_port = str(parsed.hostname or "").strip()
        if not host_without_port:
            host_without_port = str(self.settings.admin_web_host or "").strip() or "127.0.0.1"
        if not host_only:
            host_only = _format_host(host_without_port)
            default_http_port = int(self.settings.admin_web_port)
            if default_http_port > 0:
                host_only = f"{host_only}:{default_http_port}"

        direct_host = _format_host(host_without_port)
        candidates: list[str] = []
        public_ws_base = str(self.settings.admin_ws_public_url or "").strip()
        if public_ws_base:
            candidates.append(_with_ticket(public_ws_base))
        direct_ws = _with_ticket(f"{ws_scheme_direct}://{direct_host}:{int(self._ws_port)}/ws")
        same_origin_ws = _with_ticket(f"{ws_scheme_same_origin}://{host_only}/ws")
        for item in (direct_ws, same_origin_ws):
            if item and item not in candidates:
                candidates.append(item)

        return {
            "enabled": ws_serve is not None,
            "ws_port": int(self._ws_port),
            "ws_path": "/ws",
            "ws_tls_enabled": bool(self._ws_tls_enabled),
            "ws_url": candidates[0] if candidates else "",
            "ws_candidates": candidates,
            "ticket": ticket,
        }

    def _run_async(self, awaitable, *, timeout: float = 10.0):
        fut = asyncio.run_coroutine_threadsafe(awaitable, self.event_loop)
        try:
            return fut.result(timeout=max(1.0, float(timeout)))
        except FutureTimeoutError:
            fut.cancel()
            raise

    def _dashboard_snapshot(self) -> dict[str, object]:
        routes = self._routes_with_runtime_status()
        try:
            sync_snapshot = self._run_async(self.management_api.sync_stats(), timeout=12.0)
        except Exception:
            logger.exception("Failed to fetch async sync stats for dashboard; fallback to snapshot")
            sync_snapshot = self.management_api.sync_stats_snapshot()
        status_counts = dict(sync_snapshot.get("status_counts") or {})
        route_status_counts = {"deactive": 0, "syncing": 0, "sync_waiting": 0, "synced": 0}
        for route in routes:
            status = str(route.get("runtime_status") or route.get("status") or "").strip().lower()
            if status in route_status_counts:
                route_status_counts[status] += 1
        return {
            "service": self.service.runtime_snapshot(),
            "routes": {
                "total": len(routes),
                "deactive": int(route_status_counts["deactive"]),
                "syncing": int(route_status_counts["syncing"]),
                "sync_waiting": int(route_status_counts["sync_waiting"]),
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
        routes = self._routes_with_runtime_status()
        syncing_routes = 0
        for route in routes:
            status = str(route.get("runtime_status") or route.get("status") or "").strip().lower()
            if status in {"syncing", "sync_waiting"}:
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

    def _routes_with_runtime_status(self) -> list[dict]:
        routes = [dict(item) for item in self.management_api.list_routes()]
        runtime_map: dict[str, dict[str, object]] = {}
        try:
            out = self._run_async(self.service.sync_runtime_status_snapshot(), timeout=4.0)
            if isinstance(out, dict):
                runtime_map = out
        except Exception:
            logger.exception("Failed to fetch route runtime status snapshot")
            runtime_map = {}

        for route in routes:
            name = str(route.get("name") or "").strip()
            if not name:
                continue
            runtime = runtime_map.get(name)
            if not isinstance(runtime, dict):
                continue
            runtime_status = str(runtime.get("runtime_status") or "").strip().lower()
            if runtime_status:
                route["runtime_status"] = runtime_status
            wait_remaining = runtime.get("wait_remaining_sec")
            try:
                wait_sec = max(0.0, float(wait_remaining if wait_remaining is not None else 0.0))
            except Exception:
                wait_sec = 0.0
            if wait_sec > 0.0:
                route["wait_remaining_sec"] = round(wait_sec, 2)
            next_due = runtime.get("next_due_at_ts")
            try:
                next_due_ts = float(next_due) if next_due is not None else 0.0
            except Exception:
                next_due_ts = 0.0
            if next_due_ts > 0.0:
                route["next_due_at"] = datetime.fromtimestamp(next_due_ts, tz=timezone.utc).isoformat()
        return routes

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
