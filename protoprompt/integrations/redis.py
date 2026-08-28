"""Async Redis adapters for cache, sessions, and ephemeral profiles."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from protoprompt.profile.store import profile_from_dict, profile_to_dict
from protoprompt.profile.types import UserProfile
from protoprompt.scope import MemoryScope


def _dependencies():
    try:
        import redis.asyncio as redis
        from redis.exceptions import WatchError
    except ImportError as exc:
        raise ImportError(
            "Redis adapters require redis-py asyncio support. "
            "Install with: pip install 'protoprompt[redis]'"
        ) from exc
    return redis, WatchError


def _positive_ttl(value: int) -> int:
    if value < 1:
        raise ValueError("ttl_seconds must be positive")
    return value


def _opaque_key(prefix: str, kind: str, namespace: str, identity: str) -> str:
    digest = hashlib.sha256(
        json.dumps(
            [namespace, identity],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"{prefix}:{kind}:{digest}"


def _text(value: Any) -> str | None:
    if value is None:
        return None
    return value.decode("utf-8") if isinstance(value, bytes) else str(value)


class _RedisOwner:
    def __init__(
        self,
        *,
        url: str | None,
        client: Any | None,
        socket_timeout: float,
    ) -> None:
        redis, _ = _dependencies()
        if url is not None and client is not None:
            raise ValueError("provide url or client, not both")
        if client is None:
            client = redis.from_url(
                url or "redis://localhost:6379/0",
                decode_responses=True,
                socket_timeout=socket_timeout,
                socket_connect_timeout=socket_timeout,
                health_check_interval=30,
            )
            self._owns_client = True
        else:
            self._owns_client = False
        self._client = client

    @property
    def client(self) -> Any:
        return self._client

    async def ping(self) -> bool:
        return bool(await self._client.ping())

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class RedisEmbeddingCache(_RedisOwner):
    """TTL-backed async cache with content-free opaque Redis keys."""

    def __init__(
        self,
        url: str | None = None,
        *,
        client: Any | None = None,
        ttl_seconds: int = 3600,
        prefix: str = "protoprompt",
        scope: MemoryScope | None = None,
        socket_timeout: float = 5.0,
    ) -> None:
        super().__init__(url=url, client=client, socket_timeout=socket_timeout)
        self.ttl_seconds = _positive_ttl(ttl_seconds)
        self.prefix = prefix
        self.namespace = scope.correlation_id() if scope is not None else "global"

    async def get(self, key: str) -> list[list[float]] | None:
        payload = _text(await self._client.get(self._key(key)))
        if payload is None:
            return None
        try:
            raw = json.loads(payload)
            vectors = [[float(value) for value in vector] for vector in raw]
            if not all(math.isfinite(value) for vector in vectors for value in vector):
                raise ValueError
            return vectors
        except (TypeError, ValueError, json.JSONDecodeError):
            await self._client.delete(self._key(key))
            return None

    async def put(self, key: str, vectors: list[list[float]]) -> None:
        normalized = [[float(value) for value in vector] for vector in vectors]
        if not all(math.isfinite(value) for vector in normalized for value in vector):
            raise ValueError("embedding values must be finite")
        await self._client.set(
            self._key(key),
            json.dumps(normalized, separators=(",", ":")),
            ex=self.ttl_seconds,
        )

    def _key(self, identity: str) -> str:
        return _opaque_key(self.prefix, "embedding", self.namespace, identity)


class RedisSession(_RedisOwner):
    """TTL session implementing OpenAI Agents-compatible item semantics."""

    def __init__(
        self,
        session_id: str,
        *,
        scope: MemoryScope,
        url: str | None = None,
        client: Any | None = None,
        ttl_seconds: int = 86_400,
        prefix: str = "protoprompt",
        socket_timeout: float = 5.0,
    ) -> None:
        if not session_id:
            raise ValueError("session_id must not be empty")
        if scope.is_empty:
            raise ValueError("RedisSession requires a non-empty MemoryScope")
        super().__init__(url=url, client=client, socket_timeout=socket_timeout)
        self.session_id = session_id
        self.ttl_seconds = _positive_ttl(ttl_seconds)
        self._key = _opaque_key(
            prefix,
            "session",
            scope.correlation_id(),
            session_id,
        )

    async def get_items(self, limit: int | None = None) -> list[dict[str, Any]]:
        if limit is not None and limit < 0:
            raise ValueError("limit must be non-negative or None")
        if limit == 0:
            return []
        start = -limit if limit is not None else 0
        values = await self._client.lrange(self._key, start, -1)
        if values:
            await self._client.expire(self._key, self.ttl_seconds)
        return [json.loads(_text(value) or "{}") for value in values]

    async def add_items(self, items: list[dict[str, Any]]) -> None:
        if not items:
            return
        payloads = [json.dumps(item, ensure_ascii=False) for item in items]
        async with self._client.pipeline(transaction=True) as pipeline:
            pipeline.rpush(self._key, *payloads)
            pipeline.expire(self._key, self.ttl_seconds)
            await pipeline.execute()

    async def pop_item(self) -> dict[str, Any] | None:
        payload = _text(await self._client.rpop(self._key))
        if payload is None:
            return None
        await self._client.expire(self._key, self.ttl_seconds)
        return json.loads(payload)

    async def clear_session(self) -> None:
        await self._client.delete(self._key)


class RedisProfileStore(_RedisOwner):
    """Tenant-scoped ephemeral profiles with WATCH-based optimistic locking."""

    def __init__(
        self,
        url: str | None = None,
        *,
        client: Any | None = None,
        tenant: str,
        ttl_seconds: int = 86_400,
        prefix: str = "protoprompt",
        socket_timeout: float = 5.0,
        max_retries: int = 16,
    ) -> None:
        if not tenant:
            raise ValueError("tenant must not be empty")
        if max_retries < 1:
            raise ValueError("max_retries must be positive")
        super().__init__(url=url, client=client, socket_timeout=socket_timeout)
        self.tenant = tenant
        self.ttl_seconds = _positive_ttl(ttl_seconds)
        self.prefix = prefix
        self.max_retries = max_retries

    async def get(self, user_id: str) -> UserProfile | None:
        payload = _text(await self._client.get(self._key(user_id)))
        if payload is None:
            return None
        await self._client.expire(self._key(user_id), self.ttl_seconds)
        return profile_from_dict(json.loads(payload))

    async def put(self, profile: UserProfile) -> None:
        await self._client.set(
            self._key(profile.user_id),
            json.dumps(profile_to_dict(profile), ensure_ascii=False),
            ex=self.ttl_seconds,
        )

    async def compare_and_put(
        self,
        profile: UserProfile,
        *,
        expected_version: int | None,
    ) -> bool:
        _, WatchError = _dependencies()
        key = self._key(profile.user_id)
        payload = json.dumps(profile_to_dict(profile), ensure_ascii=False)
        for _ in range(self.max_retries):
            async with self._client.pipeline(transaction=True) as pipeline:
                try:
                    await pipeline.watch(key)
                    current_payload = _text(await pipeline.get(key))
                    if current_payload is None:
                        matches = expected_version is None
                    else:
                        current = profile_from_dict(json.loads(current_payload))
                        matches = (
                            expected_version is not None
                            and current.version == expected_version
                        )
                    if not matches:
                        await pipeline.unwatch()
                        return False
                    pipeline.multi()
                    pipeline.set(key, payload, ex=self.ttl_seconds)
                    await pipeline.execute()
                    return True
                except WatchError:
                    continue
        raise RuntimeError("Redis profile compare-and-put conflicted repeatedly")

    async def delete(self, user_id: str) -> None:
        await self._client.delete(self._key(user_id))

    def _key(self, user_id: str) -> str:
        return _opaque_key(self.prefix, "profile", self.tenant, str(user_id))
