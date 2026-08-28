"""Google Cloud Secret Manager implementation of scoped SecretStore."""

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

_PROJECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]{0,127}$")
_PREFIX = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


class GCPSecretManagerStore:
    """Scoped secrets backed by Google Cloud Secret Manager and ADC auth."""

    def __init__(
        self,
        project_id: str,
        *,
        prefix: str = "protoprompt",
        client: Any | None = None,
        default_ttl: int | None = None,
        replication: dict[str, Any] | None = None,
        clock: Callable[[], float] = time.time,
        **client_options: Any,
    ) -> None:
        if not _PROJECT.fullmatch(project_id):
            raise ValueError("invalid Google Cloud project_id")
        if not _PREFIX.fullmatch(prefix):
            raise ValueError("GCP secret prefix must start lowercase and use [a-z0-9_-]")
        if default_ttl is not None and default_ttl <= 0:
            raise ValueError("default_ttl must be positive or None")
        if client is not None and client_options:
            raise ValueError("client cannot be combined with client options")
        owned = client is None
        if client is None:
            try:
                from google.cloud import secretmanager
            except ImportError as exc:
                raise ImportError(
                    "GCPSecretManagerStore requires google-cloud-secret-manager. "
                    "Install with: pip install 'protoprompt[gcp-secrets]'"
                ) from exc
            client = secretmanager.SecretManagerServiceClient(**client_options)
        self._client = client
        self._owned_client = owned
        self.project_id = project_id
        self.parent = f"projects/{project_id}"
        self.prefix = prefix
        self.default_ttl = default_ttl
        self.replication = replication or {"automatic": {}}
        self._clock = clock

    @property
    def client(self) -> Any:
        return self._client

    def get(self, key: str, *, scope: str) -> str | None:
        validate_identity(key, scope)
        try:
            response = self._client.access_secret_version(
                request={"name": self._name(key, scope) + "/versions/latest"}
            )
        except Exception as exc:
            if _gcp_error(exc) == "not_found":
                return None
            raise
        payload = _payload_data(response)
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
        ).encode("utf-8")
        name = self._name(key, scope)
        try:
            self._add_version(name, payload)
        except Exception as exc:
            if _gcp_error(exc) != "not_found":
                raise
            try:
                self._client.create_secret(request={
                    "parent": self.parent,
                    "secret_id": name.rsplit("/", 1)[-1],
                    "secret": {
                        "replication": self.replication,
                        "labels": {"protoprompt": "managed"},
                    },
                })
            except Exception as create_exc:
                if _gcp_error(create_exc) != "already_exists":
                    raise
            self._add_version(name, payload)

    def delete(self, key: str, *, scope: str) -> None:
        validate_identity(key, scope)
        try:
            self._client.delete_secret(request={"name": self._name(key, scope)})
        except Exception as exc:
            if _gcp_error(exc) != "not_found":
                raise

    def list_keys(self, *, scope: str) -> list[str]:
        validate_identity("list", scope)
        resource_prefix = self._resource_id_prefix(scope)
        pager = self._client.list_secrets(request={
            "parent": self.parent,
            "filter": f"name:{resource_prefix}",
        })
        keys: list[str] = []
        for secret in pager:
            name = _resource_name(secret)
            if name.rsplit("/", 1)[-1].startswith(resource_prefix):
                try:
                    response = self._client.access_secret_version(
                        request={"name": name + "/versions/latest"}
                    )
                except Exception as exc:
                    if _gcp_error(exc) == "not_found":
                        continue
                    raise
                decoded = unpack_secret(
                    _payload_data(response),
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

    def _add_version(self, name: str, payload: bytes) -> None:
        self._client.add_secret_version(request={
            "parent": name,
            "payload": {"data": payload},
        })

    def _resource_id_prefix(self, scope: str) -> str:
        return f"{self.prefix}-s-{opaque_id(scope)}-k-"

    def _name(self, key: str, scope: str) -> str:
        validate_identity(key, scope)
        resource_id = self._resource_id_prefix(scope) + opaque_id(key)
        return f"{self.parent}/secrets/{resource_id}"


def _gcp_error(exc: Exception) -> str:
    name = type(exc).__name__.lower().replace("_", "")
    if name == "notfound":
        return "not_found"
    if name == "alreadyexists":
        return "already_exists"
    code = getattr(exc, "code", None)
    if callable(code):
        code = code()
    normalized = str(getattr(code, "name", code)).lower().replace("_", "")
    return {"notfound": "not_found", "alreadyexists": "already_exists"}.get(
        normalized, ""
    )


def _payload_data(response: Any) -> bytes:
    if isinstance(response, dict):
        payload = response.get("payload", {}).get("data")
    else:
        payload = getattr(getattr(response, "payload", None), "data", None)
    if not isinstance(payload, bytes):
        raise CloudSecretDataError("GCP secret version has no bytes payload")
    return payload


def _resource_name(secret: Any) -> str:
    name = secret.get("name") if isinstance(secret, dict) else getattr(secret, "name", None)
    if not isinstance(name, str):
        raise CloudSecretDataError("GCP secret resource has no name")
    return name
