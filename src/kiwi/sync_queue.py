from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class QueueItem:
    dedupe_key: str
    route_name: str
    due_at: float
    payload_hash: str


class SyncQueueBackend:
    async def enqueue(self, *, dedupe_key: str, route_name: str, due_at: float, payload_hash: str) -> None:
        raise NotImplementedError

    async def pop_due(self, *, limit: int, now_ts: float | None = None) -> list[QueueItem]:
        raise NotImplementedError

    async def schedule_retry(self, *, dedupe_key: str, route_name: str, due_at: float, payload_hash: str) -> None:
        await self.enqueue(dedupe_key=dedupe_key, route_name=route_name, due_at=due_at, payload_hash=payload_hash)

    async def acquire_route_lock(self, *, route_name: str, owner: str, ttl_sec: int) -> bool:
        raise NotImplementedError

    async def release_route_lock(self, *, route_name: str, owner: str) -> None:
        raise NotImplementedError

    async def depth(self) -> int:
        raise NotImplementedError


class InMemorySyncQueue(SyncQueueBackend):
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._entries: dict[str, QueueItem] = {}
        self._route_locks: dict[str, tuple[str, float]] = {}

    async def enqueue(self, *, dedupe_key: str, route_name: str, due_at: float, payload_hash: str) -> None:
        async with self._lock:
            existing = self._entries.get(dedupe_key)
            if existing is None:
                self._entries[dedupe_key] = QueueItem(
                    dedupe_key=dedupe_key,
                    route_name=route_name,
                    due_at=float(due_at),
                    payload_hash=payload_hash,
                )
                return
            # Keep earliest due time.
            if float(due_at) < float(existing.due_at):
                existing.due_at = float(due_at)
            existing.route_name = route_name
            existing.payload_hash = payload_hash

    async def pop_due(self, *, limit: int, now_ts: float | None = None) -> list[QueueItem]:
        now = float(now_ts if now_ts is not None else time.time())
        take = max(1, int(limit))
        async with self._lock:
            due = [item for item in self._entries.values() if float(item.due_at) <= now]
            due.sort(key=lambda item: item.due_at)
            picked = due[:take]
            for item in picked:
                self._entries.pop(item.dedupe_key, None)
            return [
                QueueItem(
                    dedupe_key=item.dedupe_key,
                    route_name=item.route_name,
                    due_at=item.due_at,
                    payload_hash=item.payload_hash,
                )
                for item in picked
            ]

    async def acquire_route_lock(self, *, route_name: str, owner: str, ttl_sec: int) -> bool:
        now = time.time()
        expiry = now + max(1, int(ttl_sec))
        async with self._lock:
            lock = self._route_locks.get(route_name)
            if lock is not None:
                current_owner, locked_until = lock
                if float(locked_until) > now and current_owner != owner:
                    return False
            self._route_locks[route_name] = (owner, expiry)
            return True

    async def release_route_lock(self, *, route_name: str, owner: str) -> None:
        async with self._lock:
            lock = self._route_locks.get(route_name)
            if lock is None:
                return
            if lock[0] == owner:
                self._route_locks.pop(route_name, None)

    async def depth(self) -> int:
        async with self._lock:
            return len(self._entries)


class RedisSyncQueue(SyncQueueBackend):
    def __init__(self, redis_url: str, *, prefix: str = "kiwi:sync") -> None:
        self.redis_url = redis_url.strip()
        self.prefix = prefix
        self._client: Any | None = None
        self._init_lock = asyncio.Lock()

    async def _ensure_client(self):
        if self._client is not None:
            return self._client
        async with self._init_lock:
            if self._client is not None:
                return self._client
            if not self.redis_url:
                raise RuntimeError("REDIS_URL is empty")
            try:
                from redis.asyncio import Redis  # type: ignore[import-not-found]
            except Exception as exc:
                raise RuntimeError("redis dependency is not installed") from exc

            client = Redis.from_url(self.redis_url, decode_responses=True)
            await client.ping()
            self._client = client
            return client

    def _due_key(self) -> str:
        return f"{self.prefix}:due"

    def _route_key(self) -> str:
        return f"{self.prefix}:route"

    def _payload_hash_key(self) -> str:
        return f"{self.prefix}:payload"

    def _lock_key(self, route_name: str) -> str:
        return f"{self.prefix}:lock:{route_name}"

    async def enqueue(self, *, dedupe_key: str, route_name: str, due_at: float, payload_hash: str) -> None:
        redis = await self._ensure_client()
        zkey = self._due_key()
        score = float(due_at)
        current = await redis.zscore(zkey, dedupe_key)
        if current is not None and float(current) <= score:
            pipe = redis.pipeline()
            pipe.hset(self._route_key(), dedupe_key, route_name)
            pipe.hset(self._payload_hash_key(), dedupe_key, payload_hash)
            await pipe.execute()
            return

        pipe = redis.pipeline()
        pipe.zadd(zkey, {dedupe_key: score})
        pipe.hset(self._route_key(), dedupe_key, route_name)
        pipe.hset(self._payload_hash_key(), dedupe_key, payload_hash)
        await pipe.execute()

    async def pop_due(self, *, limit: int, now_ts: float | None = None) -> list[QueueItem]:
        redis = await self._ensure_client()
        now = float(now_ts if now_ts is not None else time.time())
        take = max(1, int(limit))
        out: list[QueueItem] = []
        zkey = self._due_key()

        while len(out) < take:
            popped = await redis.zpopmin(zkey, count=1)
            if not popped:
                break
            member, score = popped[0]
            score_float = float(score)
            if score_float > now:
                await redis.zadd(zkey, {member: score_float})
                break

            route_name = await redis.hget(self._route_key(), member)
            payload_hash = await redis.hget(self._payload_hash_key(), member)
            dedupe_key = str(member)
            if not route_name:
                route_name = dedupe_key.split("|", 1)[0] if "|" in dedupe_key else "unknown"
            out.append(
                QueueItem(
                    dedupe_key=dedupe_key,
                    route_name=str(route_name),
                    due_at=score_float,
                    payload_hash=str(payload_hash or ""),
                )
            )

        return out

    async def acquire_route_lock(self, *, route_name: str, owner: str, ttl_sec: int) -> bool:
        redis = await self._ensure_client()
        result = await redis.set(self._lock_key(route_name), owner, ex=max(1, int(ttl_sec)), nx=True)
        return bool(result)

    async def release_route_lock(self, *, route_name: str, owner: str) -> None:
        redis = await self._ensure_client()
        key = self._lock_key(route_name)
        current = await redis.get(key)
        if str(current or "") == owner:
            await redis.delete(key)

    async def depth(self) -> int:
        redis = await self._ensure_client()
        return int(await redis.zcard(self._due_key()))


async def build_sync_queue_backend(*, backend: str, redis_url: str) -> SyncQueueBackend:
    mode = str(backend or "memory").strip().lower()
    if mode != "redis":
        return InMemorySyncQueue()

    queue = RedisSyncQueue(redis_url)
    try:
        await queue._ensure_client()
        return queue
    except Exception:
        logger.exception(
            "Redis queue init failed; falling back to in-memory queue",
            extra={"details": {"backend": mode}},
        )
        return InMemorySyncQueue()


def payload_hash_from_json(payload: dict[str, Any]) -> str:
    try:
        dumped = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except Exception:
        dumped = json.dumps({}, separators=(",", ":"))
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()
