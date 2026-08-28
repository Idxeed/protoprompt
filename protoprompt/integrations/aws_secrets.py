"""AWS Secrets Manager implementation of the scoped SecretStore contract."""

from __future__ import annotations

import re
import time
from typing import Any, Callable

from protoprompt.integrations._cloud_secret import (
    CloudSecretDataError,
    opaque_id,
    pack_secret,
    unpack_secret,
    validate_identity,
)

_PREFIX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/_+=.@-]{0,127}$")


class AWSSecretsManagerStore:
    """Scoped secrets backed by AWS Secrets Manager.

    Resource names contain hashes of scope/key, not their plaintext. Values use
    the service's encryption-at-rest and a small envelope for identity and TTL.
    """

    def __init__(
        self,
        *,
        prefix: str = "protoprompt",
        client: Any | None = None,
        session: Any | None = None,
        default_ttl: int | None = None,
        recovery_window_days: int = 7,
        force_delete_without_recovery: bool = False,
        clock: Callable[[], float] = time.time,
        **client_options: Any,
    ) -> None:
        prefix = prefix.rstrip("/")
        if not _PREFIX.fullmatch(prefix):
            raise ValueError("AWS secret prefix contains unsupported characters")
        if default_ttl is not None and default_ttl <= 0:
            raise ValueError("default_ttl must be positive or None")
        if not 7 <= recovery_window_days <= 30:
            raise ValueError("recovery_window_days must be between 7 and 30")
        if client is not None and (session is not None or client_options):
            raise ValueError("client cannot be combined with session/client options")
        owned = client is None
        if client is None:
            try:
                import boto3
            except ImportError as exc:
                raise ImportError(
                    "AWSSecretsManagerStore requires boto3. Install with: "
                    "pip install 'protoprompt[aws-secrets]'"
                ) from exc
            factory = session or boto3
            client = factory.client("secretsmanager", **client_options)
        self._client = client
        self._owned_client = owned
        self.prefix = prefix
        self.default_ttl = default_ttl
        self.recovery_window_days = recovery_window_days
        self.force_delete_without_recovery = force_delete_without_recovery
        self._clock = clock
        self._known_names: set[str] = set()

    @property
    def client(self) -> Any:
        return self._client

    def get(self, key: str, *, scope: str) -> str | None:
        validate_identity(key, scope)
        payload = self._get_payload(self._name(key, scope))
        if payload is None:
            return None
        decoded = unpack_secret(
            payload,
            expected_scope=scope,
            expected_key=key,
            clock=self._clock,
        )
        return None if decoded is None else decoded[1]

    def put(
        self,
        key: str,
        value: str,
        *,
        scope: str,
        ttl: int | None = None,
    ) -> None:
        effective_ttl = self.default_ttl if ttl is None else ttl
        payload = pack_secret(
            key, value, scope=scope, ttl=effective_ttl, clock=self._clock
        )
        name = self._name(key, scope)
        try:
            self._client.put_secret_value(SecretId=name, SecretString=payload)
        except Exception as exc:
            code = _aws_error_code(exc)
            if code == "ResourceNotFoundException":
                self._create(name, payload)
            elif code == "InvalidRequestException" and _scheduled_for_deletion(exc):
                self._client.restore_secret(SecretId=name)
                self._client.put_secret_value(SecretId=name, SecretString=payload)
            else:
                raise
        self._known_names.add(name)

    def delete(self, key: str, *, scope: str) -> None:
        validate_identity(key, scope)
        name = self._name(key, scope)
        options: dict[str, Any] = {"SecretId": name}
        if self.force_delete_without_recovery:
            options["ForceDeleteWithoutRecovery"] = True
        else:
            options["RecoveryWindowInDays"] = self.recovery_window_days
        try:
            self._client.delete_secret(**options)
        except Exception as exc:
            code = _aws_error_code(exc)
            if code != "ResourceNotFoundException" and not (
                code == "InvalidRequestException" and _scheduled_for_deletion(exc)
            ):
                raise
        self._known_names.discard(name)

    def list_keys(self, *, scope: str) -> list[str]:
        validate_identity("list", scope)
        prefix = self._scope_prefix(scope)
        names = set(self._known_names)
        paginator = self._client.get_paginator("list_secrets")
        for page in paginator.paginate(Filters=[{"Key": "name", "Values": [prefix]}]):
            names.update(
                secret["Name"] for secret in page.get("SecretList", [])
                if str(secret.get("Name", "")).startswith(prefix)
            )
        keys: list[str] = []
        for name in names:
            if not name.startswith(prefix):
                continue
            payload = self._get_payload(name)
            if payload is None:
                continue
            decoded = unpack_secret(
                payload,
                expected_scope=scope,
                expected_key=None,
                clock=self._clock,
            )
            if decoded is not None:
                keys.append(decoded[0])
        return sorted(set(keys))

    def close(self) -> None:
        if self._owned_client:
            close = getattr(self._client, "close", None)
            if close is not None:
                close()

    def _create(self, name: str, payload: str) -> None:
        try:
            self._client.create_secret(
                Name=name,
                SecretString=payload,
                Tags=[{"Key": "protoprompt", "Value": "managed"}],
            )
        except Exception as exc:
            if _aws_error_code(exc) != "ResourceExistsException":
                raise
            self._client.put_secret_value(SecretId=name, SecretString=payload)

    def _get_payload(self, name: str) -> str | None:
        try:
            response = self._client.get_secret_value(SecretId=name)
        except Exception as exc:
            code = _aws_error_code(exc)
            if code == "ResourceNotFoundException" or (
                code == "InvalidRequestException" and _scheduled_for_deletion(exc)
            ):
                return None
            raise
        payload = response.get("SecretString")
        if not isinstance(payload, str):
            raise CloudSecretDataError("AWS secret payload is not SecretString")
        return payload

    def _scope_prefix(self, scope: str) -> str:
        return f"{self.prefix}/{opaque_id(scope)}/"

    def _name(self, key: str, scope: str) -> str:
        validate_identity(key, scope)
        return self._scope_prefix(scope) + opaque_id(key)


def _aws_error_code(exc: Exception) -> str:
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return str(response.get("Error", {}).get("Code", ""))
    return ""


def _scheduled_for_deletion(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    message = ""
    if isinstance(response, dict):
        message = str(response.get("Error", {}).get("Message", ""))
    return "scheduled for deletion" in (message or str(exc)).lower()
