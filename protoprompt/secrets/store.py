"""Encrypted secret storage with scope isolation and TTL.

Secrets are *never* stored in the vector store and never embedded: this
module keeps them in a dedicated SQLite table, encrypted per-entry with
``Fernet`` (authenticated AES + embedded timestamp, which gives native
TTL). Access is scoped by an opaque ``scope`` string (conventionally
``f"{user_id}:{project}"``) with exact-match semantics — one session's
agent cannot read another session's secrets.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from typing import Protocol, runtime_checkable

from cryptography.fernet import Fernet, InvalidToken

from protoprompt.secrets.errors import SecretKeyError
from protoprompt.secrets.key import KeyProvider, generate_key

logger = logging.getLogger(__name__)

DEFAULT_SCOPE = "default"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS secrets (
    scope TEXT NOT NULL,
    key TEXT NOT NULL,
    token TEXT NOT NULL,
    ttl INTEGER,
    PRIMARY KEY (scope, key)
);
CREATE TABLE IF NOT EXISTS secret_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_PENDING_ROTATION = "pending_rotation"


@runtime_checkable
class SecretStore(Protocol):
    def get(self, key: str, *, scope: str) -> str | None:
        ...

    def put(
        self,
        key: str,
        value: str,
        *,
        scope: str,
        ttl: int | None = None,
    ) -> None:
        ...

    def delete(self, key: str, *, scope: str) -> None:
        ...

    def list_keys(self, *, scope: str) -> list[str]:
        ...


class EncryptedSqliteSecretStore:
    """SQLite-backed, per-entry Fernet-encrypted secret store.

    Args:
        path: SQLite file path, or ``":memory:"``.
        key_provider: supplies the master key (defaults to
            :class:`~protoprompt.secrets.key.KeyringKeyProvider`).
        ttl: default TTL in seconds applied to every ``put`` unless
            overridden (``None`` = never expires).
    """

    def __init__(
        self,
        path: str = ":memory:",
        *,
        key_provider: KeyProvider | None = None,
        ttl: int | None = None,
    ) -> None:
        if key_provider is None:
            from protoprompt.secrets.key import KeyringKeyProvider

            key_provider = KeyringKeyProvider()

        self._key_provider = key_provider
        self._default_ttl = ttl
        provider_key = key_provider.get()
        self._fernet = Fernet(provider_key)

        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            self._conn.commit()
        self._recover_pending_rotation(provider_key)

    def get(self, key: str, *, scope: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT token, ttl FROM secrets WHERE scope = ? AND key = ?",
                (scope, key),
            ).fetchone()
        if row is None:
            return None

        token, ttl = row
        try:
            if ttl is None:
                value = self._fernet.decrypt(token.encode())
            else:
                value = self._fernet.decrypt(token.encode(), ttl=ttl)
        except InvalidToken:
            # Expired or tampered; drop the entry so it is not retried.
            logger.debug("secret %r in scope %r expired or tampered", key, scope)
            with self._lock:
                self._conn.execute(
                    "DELETE FROM secrets WHERE scope = ? AND key = ?", (scope, key)
                )
                self._conn.commit()
            return None
        return value.decode("utf-8")

    def put(
        self,
        key: str,
        value: str,
        *,
        scope: str,
        ttl: int | None = None,
    ) -> None:
        effective_ttl = self._default_ttl if ttl is None else ttl
        token = self._fernet.encrypt(value.encode("utf-8")).decode("ascii")
        with self._lock:
            self._conn.execute(
                "INSERT INTO secrets (scope, key, token, ttl) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(scope, key) DO UPDATE SET token = excluded.token, "
                "ttl = excluded.ttl",
                (scope, key, token, effective_ttl),
            )
            self._conn.commit()

    def delete(self, key: str, *, scope: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM secrets WHERE scope = ? AND key = ?", (scope, key)
            )
            self._conn.commit()

    def list_keys(self, *, scope: str) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT key FROM secrets WHERE scope = ? ORDER BY key", (scope,)
            ).fetchall()
        return [r[0] for r in rows]

    def rotate_key(self, new_key: bytes | None = None) -> None:
        """Re-encrypt every secret under a new master key, recoverably.

        The database first records the new key encrypted by the old key and
        commits the re-encrypted rows. Only then is the external key provider
        updated. If the process dies between those steps, initialization can
        recover regardless of whether the provider still has the old key or
        already has the new one.
        """
        with self._lock:
            pending = self._conn.execute(
                "SELECT 1 FROM secret_meta WHERE key = ?", (_PENDING_ROTATION,)
            ).fetchone()
        if pending is not None:
            raise SecretKeyError(
                "a key rotation is already pending; reopen the vault to recover it"
            )

        new_key = new_key or generate_key()
        new_fernet = Fernet(new_key)
        wrapped_new_key = self._fernet.encrypt(new_key).decode("ascii")

        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                rows = self._conn.execute(
                    "SELECT scope, key, token FROM secrets"
                ).fetchall()
                for scope, key, token in rows:
                    token_bytes = token.encode()
                    value = self._fernet.decrypt(token_bytes)
                    created_at = self._fernet.extract_timestamp(token_bytes)
                    self._conn.execute(
                        "UPDATE secrets SET token = ? WHERE scope = ? AND key = ?",
                        (
                            new_fernet.encrypt_at_time(value, created_at).decode("ascii"),
                            scope,
                            key,
                        ),
                    )
                self._conn.execute(
                    "INSERT INTO secret_meta (key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (_PENDING_ROTATION, wrapped_new_key),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

        self._fernet = new_fernet
        try:
            self._key_provider.rotate(new_key)
        except Exception as exc:
            # The committed pending record lets the next process recover with
            # the provider's old key. This instance already uses the new key.
            raise SecretKeyError(
                "key provider rotation failed; vault remains recoverable"
            ) from exc
        self._clear_pending_rotation()

    def _recover_pending_rotation(self, provider_key: bytes) -> None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM secret_meta WHERE key = ?", (_PENDING_ROTATION,)
            ).fetchone()
            tokens = [
                token for (token,) in self._conn.execute("SELECT token FROM secrets")
            ]
        if row is None:
            return

        provider_fernet = Fernet(provider_key)
        try:
            pending_key = provider_fernet.decrypt(row[0].encode())
        except InvalidToken:
            # The provider already has the new key. Verify that it decrypts
            # the committed data before declaring the rotation complete.
            if not self._tokens_decrypt(provider_fernet, tokens):
                raise SecretKeyError("cannot recover interrupted key rotation")
            self._fernet = provider_fernet
            self._clear_pending_rotation()
            return

        pending_fernet = Fernet(pending_key)
        if not self._tokens_decrypt(pending_fernet, tokens):
            raise SecretKeyError("pending rotation key does not decrypt the vault")
        self._fernet = pending_fernet
        try:
            self._key_provider.rotate(pending_key)
        except Exception as exc:
            raise SecretKeyError(
                "interrupted key rotation recovered, but provider update still fails"
            ) from exc
        self._clear_pending_rotation()

    @staticmethod
    def _tokens_decrypt(fernet: Fernet, tokens: list[str]) -> bool:
        try:
            for token in tokens:
                fernet.decrypt(token.encode())
        except InvalidToken:
            return False
        return True

    def _clear_pending_rotation(self) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM secret_meta WHERE key = ?", (_PENDING_ROTATION,)
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "EncryptedSqliteSecretStore":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def __del__(self) -> None:
        conn = getattr(self, "_conn", None)
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass
