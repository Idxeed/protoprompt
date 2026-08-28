"""Shared opaque naming and payload format for managed secret services."""

from __future__ import annotations

import hashlib
import json
from typing import Callable

_FORMAT = "protoprompt-secret-v1"
_MAX_PAYLOAD_BYTES = 64 * 1024


class CloudSecretDataError(RuntimeError):
    """A managed secret resource does not contain a valid protoprompt payload."""


def validate_identity(key: str, scope: str) -> None:
    if not isinstance(key, str) or not key:
        raise ValueError("secret key must be a non-empty string")
    if not isinstance(scope, str) or not scope:
        raise ValueError("secret scope must be a non-empty string")


def opaque_id(value: str) -> str:
    """Stable identifier that does not expose tenant or key text in audit logs."""

    return hashlib.blake2b(value.encode("utf-8"), digest_size=16).hexdigest()


def pack_secret(
    key: str,
    value: str,
    *,
    scope: str,
    ttl: int | None,
    clock: Callable[[], float],
) -> str:
    validate_identity(key, scope)
    if not isinstance(value, str):
        raise TypeError("secret value must be a string")
    if ttl is not None and (isinstance(ttl, bool) or not isinstance(ttl, int) or ttl <= 0):
        raise ValueError("secret ttl must be a positive integer or None")
    payload = json.dumps(
        {
            "format": _FORMAT,
            "scope": scope,
            "key": key,
            "value": value,
            "expires_at": None if ttl is None else clock() + ttl,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    if len(payload.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
        raise ValueError("encoded secret exceeds the managed-service 64 KiB limit")
    return payload


def unpack_secret(
    payload: str | bytes,
    *,
    expected_scope: str,
    expected_key: str | None,
    clock: Callable[[], float],
) -> tuple[str, str] | None:
    try:
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        data = json.loads(payload)
        scope = data["scope"]
        key = data["key"]
        value = data["value"]
        expires_at = data["expires_at"]
    except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
        raise CloudSecretDataError("managed secret has an invalid payload") from exc
    if data.get("format") != _FORMAT:
        raise CloudSecretDataError("managed secret has an unsupported payload format")
    if scope != expected_scope or (expected_key is not None and key != expected_key):
        raise CloudSecretDataError("managed secret identity does not match its resource name")
    if not isinstance(key, str) or not isinstance(value, str):
        raise CloudSecretDataError("managed secret key and value must be strings")
    if expires_at is not None and not isinstance(expires_at, (int, float)):
        raise CloudSecretDataError("managed secret expiry is invalid")
    if expires_at is not None and clock() >= float(expires_at):
        return None
    return key, value
