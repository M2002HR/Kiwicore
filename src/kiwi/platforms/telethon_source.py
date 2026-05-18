from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from kiwi.errors import MessageTooLargeError
from kiwi.types import ChannelRoute, IncomingChannelMessage, IncomingMedia, MediaKind
from kiwi.utils import normalize_channel_id, normalize_channel_username

logger = logging.getLogger(__name__)


class TelethonSourceClient:
    def __init__(
        self,
        *,
        api_id: int,
        api_hash: str,
        session_path: str,
        poll_batch_size: int = 50,
        trust_env_proxy: bool = False,
        proxy_url: str | None = None,
    ) -> None:
        self.api_id = int(api_id)
        self.api_hash = api_hash.strip()
        self.session_path = session_path
        self.poll_batch_size = max(1, int(poll_batch_size))
        self.trust_env_proxy = bool(trust_env_proxy)
        self.proxy_url = (proxy_url or "").strip() or None

        self._client: Any | None = None
        self._entity_cache: dict[str, Any] = {}
        self._last_message_id: dict[str, int] = {}
        self._resolve_retry_after: dict[str, float] = {}
        self._resolve_last_warn_at: dict[str, float] = {}
        self._lock = asyncio.Lock()

    @staticmethod
    def _is_unresolvable_entity_error(exc: Exception) -> bool:
        text = str(exc or "").strip().lower()
        return ("unable to resolve source entity" in text) or ("could not find the input entity" in text)

    def _cache_entity(self, source_key: str, entity: Any, *, channel_id: str | None = None) -> None:
        key = str(source_key or "").strip()
        if key:
            self._entity_cache[key] = entity
        normalized_channel_id = normalize_channel_id(channel_id)
        if normalized_channel_id:
            self._entity_cache[normalized_channel_id] = entity
        try:
            ent_id = int(getattr(entity, "id", 0) or 0)
        except Exception:
            ent_id = 0
        if ent_id > 0:
            self._entity_cache[f"-100{ent_id}"] = entity

    @staticmethod
    def _is_channel_entity(entity: Any) -> bool:
        if entity is None:
            return False
        cls_name = str(getattr(entity.__class__, "__name__", "") or "").lower()
        if "channel" in cls_name:
            return True
        if bool(getattr(entity, "broadcast", False)):
            return True
        if bool(getattr(entity, "megagroup", False)):
            return True
        if bool(getattr(entity, "gigagroup", False)):
            return True
        return False

    def _cache_if_channel_entity(self, source_key: str, entity: Any, *, channel_id: str | None = None) -> bool:
        if not self._is_channel_entity(entity):
            return False
        self._cache_entity(source_key, entity, channel_id=channel_id)
        return True

    async def _resolve_entity_from_dialogs(self, channel_id: int) -> Any | None:
        if self._client is None:
            return None
        iter_dialogs = getattr(self._client, "iter_dialogs", None)
        if not callable(iter_dialogs):
            return None

        async def _scan_dialogs(*, archived: bool | None = None) -> Any | None:
            kwargs: dict[str, Any] = {"limit": 1000}
            if archived is not None:
                kwargs["archived"] = archived
            async for dlg in iter_dialogs(**kwargs):
                entity = getattr(dlg, "entity", None)
                if entity is None:
                    continue
                try:
                    ent_id = int(getattr(entity, "id", 0) or 0)
                except Exception:
                    continue
                if ent_id == int(channel_id):
                    if not self._is_channel_entity(entity):
                        continue
                    return entity
            return None

        try:
            resolved = await _scan_dialogs()
            if resolved is not None:
                return resolved
            try:
                resolved = await _scan_dialogs(archived=True)
                if resolved is not None:
                    return resolved
            except TypeError:
                # Older clients/fakes may not support the archived argument.
                pass
        except Exception:
            return None
        return None

    def route_source_key(self, route: ChannelRoute) -> str | None:
        return self._route_source_key(route)

    def prime_cursor(self, source_key: str, message_id: int) -> None:
        key = str(source_key or "").strip()
        if not key:
            return
        seeded = max(0, int(message_id))
        current = self._last_message_id.get(key)
        if current is None:
            self._last_message_id[key] = seeded
            return
        # Never rewind an already-advanced cursor; otherwise the same backfill window
        # is replayed every loop and media-group buffers never settle.
        self._last_message_id[key] = max(int(current), seeded)

    async def aclose(self) -> None:
        if self._client is None:
            return
        await self._client.disconnect()
        self._client = None

    async def poll_messages(self, routes: list[ChannelRoute]) -> list[IncomingChannelMessage]:
        await self._ensure_connected()
        assert self._client is not None

        sources: dict[str, tuple[str | None, str | None, list[str]]] = {}
        for route in routes:
            if route.is_deactive():
                continue
            source_key = self._route_source_key(route)
            if source_key is None:
                continue
            existing = sources.get(source_key)
            if existing is None:
                sources[source_key] = (route.source_channel_username, route.source_channel_id, [route.name])
            else:
                names = list(existing[2])
                if route.name not in names:
                    names.append(route.name)
                channel_id = existing[1] or route.source_channel_id
                sources[source_key] = (existing[0], channel_id, names)

        out: list[IncomingChannelMessage] = []
        for source_key, (username, channel_id, route_names) in sources.items():
            now = asyncio.get_running_loop().time()
            retry_after = float(self._resolve_retry_after.get(source_key) or 0.0)
            if retry_after > now:
                continue
            try:
                entity = await self._resolve_entity(source_key, username=username, channel_id=channel_id)
                if source_key not in self._last_message_id:
                    # First poll must not replay historical channel content.
                    # Initialize cursor at the latest message id and start from there.
                    self._last_message_id[source_key] = await self._latest_message_id(entity)
                    continue

                min_id = int(self._last_message_id.get(source_key) or 0)
                max_seen = min_id

                async for msg in self._client.iter_messages(
                    entity,
                    min_id=min_id,
                    limit=self.poll_batch_size,
                    reverse=True,
                ):
                    message_id = int(getattr(msg, "id", 0) or 0)
                    if message_id <= 0:
                        continue
                    if message_id > max_seen:
                        max_seen = message_id
                    parsed = await self._to_incoming(msg, source_key=source_key, source_username=username)
                    if parsed is not None:
                        out.append(parsed)

                if max_seen > min_id:
                    self._last_message_id[source_key] = max_seen
                self._resolve_retry_after.pop(source_key, None)
                self._resolve_last_warn_at.pop(source_key, None)
            except Exception as exc:
                if self._is_unresolvable_entity_error(exc):
                    # Back off repeated resolution attempts for invalid/missing channels.
                    self._resolve_retry_after[source_key] = now + 300.0
                    last_warn_at = float(self._resolve_last_warn_at.get(source_key) or 0.0)
                    if (now - last_warn_at) >= 300.0:
                        self._resolve_last_warn_at[source_key] = now
                        logger.warning(
                            "Telethon source resolve failed; route temporarily paused",
                            extra={
                                "details": {
                                    "routes": route_names,
                                    "source_key": source_key,
                                    "source_username": username,
                                    "source_channel_id": channel_id,
                                    "retry_in_sec": 300,
                                    "resolve_strategy": "username_then_channel_id_then_numeric_source_key",
                                }
                            },
                        )
                else:
                    logger.exception(
                        "Telethon source poll failed",
                        extra={
                            "details": {
                                "routes": route_names,
                                "source_key": source_key,
                                "source_username": username,
                                "source_channel_id": channel_id,
                                "resolve_strategy": "username_then_channel_id_then_numeric_source_key",
                            }
                        },
                    )
                continue

        out.sort(key=lambda item: (item.update_id, item.message_id))
        return out

    async def seed_recent_messages(self, route: ChannelRoute, limit: int) -> list[IncomingChannelMessage]:
        await self._ensure_connected()
        assert self._client is not None

        source_key = self._route_source_key(route)
        if source_key is None:
            return []

        entity = await self._resolve_entity(
            source_key,
            username=route.source_channel_username,
            channel_id=route.source_channel_id,
        )

        take = max(0, int(limit))
        if take <= 0:
            self._last_message_id[source_key] = max(
                int(self._last_message_id.get(source_key) or 0),
                await self._latest_message_id(entity),
            )
            return []

        batch = await self._client.get_messages(entity, limit=take)
        messages = list(batch or [])
        if not messages:
            self._last_message_id[source_key] = max(
                int(self._last_message_id.get(source_key) or 0),
                await self._latest_message_id(entity),
            )
            return []

        parsed: list[IncomingChannelMessage] = []
        max_seen = int(self._last_message_id.get(source_key) or 0)
        messages.sort(key=lambda msg: int(getattr(msg, "id", 0) or 0))
        for msg in messages:
            message_id = int(getattr(msg, "id", 0) or 0)
            if message_id > max_seen:
                max_seen = message_id
            incoming = await self._to_incoming(
                msg,
                source_key=source_key,
                source_username=route.source_channel_username,
            )
            if incoming is not None:
                parsed.append(incoming)

        self._last_message_id[source_key] = max_seen
        return parsed

    async def latest_message_id_for_route(self, route: ChannelRoute) -> int:
        await self._ensure_connected()
        source_key = self._route_source_key(route)
        if source_key is None:
            return 0
        entity = await self._resolve_entity(
            source_key,
            username=route.source_channel_username,
            channel_id=route.source_channel_id,
        )
        return await self._latest_message_id(entity)

    async def download_media(self, source_ref: dict, output_path: Path, max_bytes: int) -> int:
        await self._ensure_connected()
        assert self._client is not None

        source_key = str(source_ref.get("source_key") or "").strip()
        message_id = int(source_ref.get("message_id") or 0)
        if not source_key or message_id <= 0:
            raise ValueError("invalid telethon source_ref")

        entity = await self._resolve_entity(source_key, username=None, channel_id=None)
        msg = await self._client.get_messages(entity, ids=message_id)
        if msg is None:
            raise FileNotFoundError(f"Telethon message not found: {source_key}#{message_id}")

        size = int(getattr(getattr(msg, "file", None), "size", 0) or 0)
        if size > 0 and size > max_bytes:
            raise MessageTooLargeError(f"Message exceeded size limit ({size} > {max_bytes} bytes)")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        before = {p.resolve() for p in output_path.parent.glob("*") if p.is_file()}
        downloaded_to = await msg.download_media(file=str(output_path))
        resolved_path = self._resolve_downloaded_path(
            output_path=output_path,
            downloaded_to=downloaded_to,
            files_before=before,
        )
        if resolved_path is None:
            raise FileNotFoundError(str(output_path))
        if resolved_path.resolve() != output_path.resolve():
            output_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(resolved_path), str(output_path))

        downloaded = int(output_path.stat().st_size)
        if downloaded > max_bytes:
            raise MessageTooLargeError(f"Message exceeded size limit ({downloaded} > {max_bytes} bytes)")
        return downloaded

    def _resolve_downloaded_path(
        self,
        *,
        output_path: Path,
        downloaded_to: object,
        files_before: set[Path],
    ) -> Path | None:
        if output_path.exists():
            return output_path

        if isinstance(downloaded_to, (str, Path)):
            candidate = Path(downloaded_to)
            if candidate.exists():
                return candidate

        candidates: list[Path] = []
        for p in output_path.parent.glob(f"{output_path.name}*"):
            if p.is_file():
                candidates.append(p)

        if not candidates:
            for p in output_path.parent.glob("*"):
                if not p.is_file():
                    continue
                try:
                    resolved = p.resolve()
                except Exception:
                    continue
                if resolved in files_before:
                    continue
                candidates.append(p)

        if not candidates:
            return None
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        return candidates[0]

    async def _latest_message_id(self, entity: Any) -> int:
        latest = await self._client.get_messages(entity, limit=1)
        if isinstance(latest, list):
            if not latest:
                return 0
            item = latest[0]
            return int(getattr(item, "id", 0) or 0)
        return int(getattr(latest, "id", 0) or 0)

    async def _ensure_connected(self) -> None:
        if self._client is not None:
            return
        async with self._lock:
            if self._client is not None:
                return
            try:
                from telethon import TelegramClient  # type: ignore[import-not-found]
            except Exception as exc:  # pragma: no cover - import path exercised in integration
                raise RuntimeError("Telethon dependency is not installed. Install `telethon`.") from exc

            session_file = Path(self.session_path)
            session_file.parent.mkdir(parents=True, exist_ok=True)
            proxy = _parse_proxy_url(self.proxy_url)
            connect_errors: list[Exception] = []

            async def _connect_once(proxy_config: object | None) -> Any:
                client = TelegramClient(str(session_file), self.api_id, self.api_hash, proxy=proxy_config)
                try:
                    # Broken proxy paths can hang in reconnect loops; keep bootstrap bounded.
                    await asyncio.wait_for(client.connect(), timeout=20.0)
                    if not await asyncio.wait_for(client.is_user_authorized(), timeout=10.0):
                        raise RuntimeError(
                            "Telethon session is not authorized. Run one-time login first and create the session file."
                        )
                    return client
                except Exception:
                    with contextlib.suppress(Exception):
                        await client.disconnect()
                    raise

            if proxy is not None:
                try:
                    self._client = await _connect_once(proxy)
                    return
                except Exception as exc:
                    connect_errors.append(exc)
                    logger.warning(
                        "Telethon proxy connection failed; retrying without proxy",
                        extra={
                            "details": {
                                "proxy_url": self.proxy_url,
                                "fallback": "direct_connection",
                                "error": str(exc),
                            }
                        },
                    )

            try:
                self._client = await _connect_once(None)
                return
            except Exception as direct_exc:
                if connect_errors:
                    raise RuntimeError(
                        "Failed to connect Telethon client via configured proxy and direct connection"
                    ) from direct_exc
                raise

    async def _resolve_entity(self, source_key: str, *, username: str | None, channel_id: str | None) -> Any:
        if source_key in self._entity_cache:
            return self._entity_cache[source_key]
        assert self._client is not None
        resolve_errors: list[Exception] = []

        normalized_channel_id = normalize_channel_id(channel_id)
        if normalized_channel_id and normalized_channel_id in self._entity_cache:
            return self._entity_cache[normalized_channel_id]

        candidate = normalize_channel_username(username) or source_key
        try:
            entity = await self._client.get_entity(candidate)
            if self._cache_if_channel_entity(source_key, entity, channel_id=channel_id):
                logger.debug(
                    "Telethon source resolved via username/source_key",
                    extra={"details": {"source_key": source_key, "resolve_strategy": "username_or_source_key"}},
                )
                return entity
            resolve_errors.append(ValueError("resolved peer is not a channel"))
        except Exception as exc:
            resolve_errors.append(exc)
            pass

        if normalized_channel_id:
            normalized = normalized_channel_id
            # Telegram channel ids are stored as -100<channel_id>; resolve via PeerChannel first.
            if normalized.startswith("-100") and normalized[4:].isdigit():
                try:
                    from telethon.tl.types import PeerChannel  # type: ignore[import-not-found]
                except Exception:
                    class PeerChannel:  # type: ignore[no-redef]
                        def __init__(self, channel_id: int) -> None:
                            self.channel_id = int(channel_id)

                        def __repr__(self) -> str:
                            return f"PeerChannel(channel_id={self.channel_id})"

                        __str__ = __repr__

                cid_int = int(normalized[4:])
                dialog_entity = await self._resolve_entity_from_dialogs(cid_int)
                if dialog_entity is not None:
                    self._cache_entity(source_key, dialog_entity, channel_id=normalized)
                    logger.debug(
                        "Telethon source resolved via dialog scan",
                        extra={
                            "details": {
                                "source_key": source_key,
                                "route_channel_id": channel_id,
                                "resolve_strategy": "dialog_scan_by_channel_id",
                            }
                        },
                    )
                    return dialog_entity
                try:
                    entity = await self._client.get_entity(PeerChannel(cid_int))
                except Exception as exc_peer:
                    resolve_errors.append(exc_peer)
                    try:
                        entity = await self._client.get_entity(cid_int)
                    except Exception as exc_numeric:
                        resolve_errors.append(exc_numeric)
                        entity = None
                if entity is None:
                    # Continue to source_key fallback before failing.
                    pass
                else:
                    if self._cache_if_channel_entity(source_key, entity, channel_id=normalized):
                        logger.debug(
                            "Telethon source resolved via route channel_id",
                            extra={
                                "details": {
                                    "source_key": source_key,
                                    "route_channel_id": channel_id,
                                    "resolve_strategy": "route_channel_id_peerchannel",
                                }
                            },
                        )
                        return entity
                    resolve_errors.append(ValueError("resolved peer is not a channel"))

            if normalized.lstrip("-").isdigit():
                cid_int = int(normalized)
                dialog_entity = await self._resolve_entity_from_dialogs(cid_int)
                if dialog_entity is not None:
                    self._cache_entity(source_key, dialog_entity, channel_id=normalized)
                    logger.debug(
                        "Telethon source resolved via dialog scan",
                        extra={
                            "details": {
                                "source_key": source_key,
                                "route_channel_id": channel_id,
                                "resolve_strategy": "dialog_scan_by_numeric_route_id",
                            }
                        },
                    )
                    return dialog_entity
                try:
                    entity = await self._client.get_entity(cid_int)
                    if self._cache_if_channel_entity(source_key, entity, channel_id=normalized):
                        logger.debug(
                            "Telethon source resolved via numeric route id",
                            extra={
                                "details": {
                                    "source_key": source_key,
                                    "route_channel_id": channel_id,
                                    "resolve_strategy": "route_channel_id_numeric",
                                }
                            },
                        )
                        return entity
                    resolve_errors.append(ValueError("resolved peer is not a channel"))
                except Exception as exc:
                    resolve_errors.append(exc)

        # As a fallback, try source key as numeric channel id.
        stripped = source_key
        if stripped.startswith("-100"):
            stripped = stripped[4:]
        if stripped.lstrip("-").isdigit():
            cid_int = int(stripped)
            dialog_entity = await self._resolve_entity_from_dialogs(cid_int)
            if dialog_entity is not None:
                self._cache_entity(source_key, dialog_entity, channel_id=f"-100{cid_int}")
                logger.debug(
                    "Telethon source resolved via dialog scan",
                    extra={"details": {"source_key": source_key, "resolve_strategy": "dialog_scan_by_source_key"}},
                )
                return dialog_entity
            try:
                entity = await self._client.get_entity(cid_int)
                if self._cache_if_channel_entity(source_key, entity, channel_id=f"-100{cid_int}"):
                    logger.debug(
                        "Telethon source resolved via numeric source_key",
                        extra={"details": {"source_key": source_key, "resolve_strategy": "numeric_source_key"}},
                    )
                    return entity
                resolve_errors.append(ValueError("resolved peer is not a channel"))
            except Exception as exc:
                resolve_errors.append(exc)

        last_error = resolve_errors[-1] if resolve_errors else None
        suffix = f" channel_id={normalized_channel_id}" if normalized_channel_id else ""
        if last_error is None:
            raise ValueError(f"Unable to resolve source entity for source_key={source_key}{suffix}")
        raise ValueError(f"Unable to resolve source entity for source_key={source_key}{suffix}") from last_error

    async def _to_incoming(
        self,
        msg: Any,
        *,
        source_key: str,
        source_username: str | None,
    ) -> IncomingChannelMessage | None:
        message_id = int(getattr(msg, "id", 0) or 0)
        if message_id <= 0:
            return None

        peer_channel_id = int(getattr(getattr(msg, "peer_id", None), "channel_id", 0) or 0)
        if peer_channel_id <= 0:
            return None
        source_channel_id = normalize_channel_id(f"-100{peer_channel_id}")
        if source_channel_id is None:
            return None

        text_raw = str(getattr(msg, "message", "") or "").strip()
        medias: list[IncomingMedia] = []

        file_obj = getattr(msg, "file", None)
        file_size = int(getattr(file_obj, "size", 0) or 0) or None
        file_name = str(getattr(file_obj, "name", "") or "").strip() or None
        mime_type = str(getattr(file_obj, "mime_type", "") or "").strip() or None
        duration = getattr(file_obj, "duration", None)
        duration_int = int(duration) if duration is not None else None

        media_kind: MediaKind | None = None
        if getattr(msg, "photo", None) is not None:
            media_kind = MediaKind.PHOTO
        elif getattr(msg, "video", None) is not None:
            media_kind = MediaKind.VIDEO
        elif getattr(msg, "voice", None) is not None:
            media_kind = MediaKind.VOICE
        elif getattr(msg, "audio", None) is not None:
            media_kind = MediaKind.AUDIO
        elif getattr(msg, "gif", None) is not None or getattr(msg, "animation", None) is not None:
            media_kind = MediaKind.ANIMATION
        elif getattr(msg, "sticker", None) is not None:
            media_kind = MediaKind.STICKER
        elif getattr(msg, "video_note", None) is not None:
            media_kind = MediaKind.VIDEO_NOTE
        elif getattr(msg, "document", None) is not None:
            media_kind = MediaKind.DOCUMENT

        if media_kind is not None:
            medias.append(
                IncomingMedia(
                    kind=media_kind,
                    file_id=f"mt:{source_key}:{message_id}",
                    file_size=file_size,
                    file_name=file_name,
                    mime_type=mime_type,
                    duration=duration_int,
                    source="telethon",
                    source_ref={"source_key": source_key, "message_id": message_id},
                )
            )

        grouped_id = getattr(msg, "grouped_id", None)
        media_group_id = str(grouped_id).strip() if grouped_id is not None else None
        if media_group_id == "":
            media_group_id = None

        has_media = len(medias) > 0
        text = None if has_media else (text_raw or None)
        caption = text_raw or None if has_media else None

        ts = getattr(msg, "date", None)
        date_unix = int(ts.timestamp()) if ts is not None else None

        return IncomingChannelMessage(
            update_id=message_id,
            source_channel_id=source_channel_id,
            source_channel_username=normalize_channel_username(source_username),
            message_id=message_id,
            date=date_unix,
            text=text,
            caption=caption,
            medias=medias,
            raw={"telethon": True, "source_key": source_key, "message_id": message_id},
            media_group_id=media_group_id,
        )

    @staticmethod
    def _route_source_key(route: ChannelRoute) -> str | None:
        raw_username = str(route.source_channel_username or "").strip()
        lowered = raw_username.lower()
        if lowered.startswith("https://t.me/") or lowered.startswith("http://t.me/") or lowered.startswith("t.me/"):
            return raw_username
        if route.source_channel_username:
            return normalize_channel_username(route.source_channel_username)
        if route.source_channel_id:
            return normalize_channel_id(route.source_channel_id)
        return None


def _parse_proxy_url(proxy_url: str | None) -> object | None:
    if not proxy_url:
        return None

    raw = proxy_url.strip()
    parsed = urlparse(raw)
    scheme = parsed.scheme.strip().lower()
    host = parsed.hostname
    port = parsed.port
    if not scheme or not host or port is None:
        raise ValueError("Invalid TELETHON_PROXY_URL. Expected scheme://host:port")

    proxy_type = _proxy_type_from_scheme(scheme)
    username = unquote(parsed.username) if parsed.username else None
    password = unquote(parsed.password) if parsed.password else None
    rdns = scheme in {"socks5h", "socks4a"}
    return (proxy_type, host, port, rdns, username, password)


def _proxy_type_from_scheme(scheme: str) -> object:
    normalized = {"socks5h": "socks5", "socks4a": "socks4", "https": "http"}.get(scheme, scheme)
    if normalized not in {"socks5", "socks4", "http"}:
        raise ValueError(
            "Unsupported TELETHON_PROXY_URL scheme. Use socks5://, socks5h://, socks4://, socks4a://, http://, or https://"
        )
    try:
        import socks  # type: ignore[import-not-found]

        if normalized == "socks5":
            return socks.SOCKS5
        if normalized == "socks4":
            return socks.SOCKS4
        return socks.HTTP
    except Exception:
        return normalized
