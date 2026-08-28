"""Guarded access to a :class:`~protoprompt.secrets.store.SecretStore`.

``SecretAccess`` is pinned to a single ``scope`` and an immutable set of
host-registered operations. Agent-facing code calls ``execute`` and receives
only the operation result; the plaintext credential stays inside trusted host
code. Logs carry operation/key/scope metadata but **never** the value.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import warnings
from collections.abc import Callable, Mapping
from typing import Any

logger = logging.getLogger(__name__)


class SecretAccess:
    """Scope-pinned, value-redacting facade over a secret store.

    Args:
        store: any :class:`~protoprompt.secrets.store.SecretStore`.
        scope: fixed scope; all reads/writes are confined to it.
        default_ttl: TTL (seconds) applied to :meth:`store` unless a
            ``ttl`` is passed explicitly.
        operations: immutable name → trusted callback allow-list used by
            :meth:`execute`. Each callback receives the credential as its
            first argument.
    """

    def __init__(
        self,
        store: Any,
        *,
        scope: str,
        default_ttl: int = 300,
        operations: Mapping[str, Callable[..., Any]] | None = None,
    ) -> None:
        self._store = store
        self._scope = scope
        self._default_ttl = default_ttl
        # Host-owned allow-list. An agent can select a named operation but
        # cannot provide code that receives the plaintext credential.
        self._operations = dict(operations or {})

    @property
    def scope(self) -> str:
        return self._scope

    async def grant(self, key: str) -> str | None:
        """Return plaintext to trusted host code only.

        Deprecated for agent-facing use. Expose :meth:`execute` as the tool
        boundary so model-visible code receives only the operation result.
        """
        warnings.warn(
            "SecretAccess.grant() exposes plaintext; use execute() with "
            "host-registered operations for agent-facing access",
            DeprecationWarning,
            stacklevel=2,
        )
        value = await asyncio.to_thread(self._store.get, key, scope=self._scope)
        if value is None:
            logger.info("secret denied: key=%r scope=%r", key, self._scope)
            return None
        logger.info("secret granted: key=%r scope=%r", key, self._scope)
        return value

    async def execute(self, key: str, operation: str, /, **kwargs: Any) -> Any:
        """Run a host-registered operation with a secret without returning it.

        ``operations`` are fixed at construction time. Only their names and
        non-secret arguments should be exposed to the model as tool inputs.
        """
        handler = self._operations.get(operation)
        if handler is None:
            logger.warning(
                "secret operation denied: operation=%r key=%r scope=%r",
                operation,
                key,
                self._scope,
            )
            raise ValueError(f"unknown secret operation: {operation}")

        value = await asyncio.to_thread(self._store.get, key, scope=self._scope)
        if value is None:
            logger.info(
                "secret operation denied: operation=%r key=%r scope=%r",
                operation,
                key,
                self._scope,
            )
            return None

        logger.info(
            "secret operation started: operation=%r key=%r scope=%r",
            operation,
            key,
            self._scope,
        )
        if inspect.iscoroutinefunction(handler):
            result = await handler(value, **kwargs)
        else:
            result = await asyncio.to_thread(handler, value, **kwargs)
            if inspect.isawaitable(result):
                result = await result
        logger.info(
            "secret operation completed: operation=%r key=%r scope=%r",
            operation,
            key,
            self._scope,
        )
        return result

    def operation_names(self) -> list[str]:
        """Return the immutable host-defined operation surface."""
        return sorted(self._operations)

    async def store(self, key: str, value: str, *, ttl: int | None = None) -> None:
        effective_ttl = self._default_ttl if ttl is None else ttl
        await asyncio.to_thread(
            self._store.put, key, value, scope=self._scope, ttl=effective_ttl
        )

    async def revoke(self, key: str) -> None:
        await asyncio.to_thread(self._store.delete, key, scope=self._scope)

    async def keys(self) -> list[str]:
        return await asyncio.to_thread(self._store.list_keys, scope=self._scope)
