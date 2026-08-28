"""Key providers: where the master encryption key (KEK) lives.

The secret store never touches raw key material beyond a
:class:`KeyProvider`. Built-in providers cover the common cases:

- :class:`KeyringKeyProvider` — OS keychain (Windows DPAPI, macOS
  Keychain, Linux Secret Service); generates the key on first use and
  falls back to another provider when no backend is available;
- :class:`EnvKeyProvider` — a master key from an environment variable
  (CI / containers);
- :class:`FileKeyProvider` — a self-managed key file.

Keys are 32 random bytes, url-safe base64 encoded on the wire (the
``Fernet`` format), so they can travel through strings safely.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol, runtime_checkable

from cryptography.fernet import Fernet

from protoprompt.secrets.errors import SecretKeyError


def generate_key() -> bytes:
    """Return a fresh random 32-byte key (url-safe base64)."""
    return Fernet.generate_key()


def _to_str(key: bytes) -> str:
    return key.decode("ascii")


def _from_str(value: str) -> bytes:
    try:
        return value.strip().encode("ascii")
    except UnicodeEncodeError as exc:
        raise SecretKeyError("master key must be URL-safe ASCII") from exc


def _validate_key(key: bytes) -> bytes:
    try:
        Fernet(key)
    except (TypeError, ValueError) as exc:
        raise SecretKeyError("invalid Fernet master key") from exc
    return key


@runtime_checkable
class KeyProvider(Protocol):
    def get(self) -> bytes:
        """Return the current master key."""
        ...

    def rotate(self, new: bytes) -> None:
        """Replace the stored key (re-encryption is the caller's job)."""
        ...


class EnvKeyProvider:
    """Read the master key from an environment variable.

    Args:
        env_var: variable name. Missing value raises
            :class:`~protoprompt.secrets.errors.SecretKeyError`.
    """

    def __init__(self, env_var: str = "PROTOPROMPT_MASTER_KEY") -> None:
        self._env_var = env_var

    def get(self) -> bytes:
        value = os.environ.get(self._env_var)
        if not value:
            raise SecretKeyError(
                f"environment variable {self._env_var!r} is not set"
            )
        return _validate_key(_from_str(value))

    def rotate(self, new: bytes) -> None:
        os.environ[self._env_var] = _to_str(_validate_key(new))


class FileKeyProvider:
    """Read/own a master key from a local file.

    Args:
        path: key file location (``~`` expanded). Created on first use
            when ``create=True``.
        create: whether to generate and persist a key when the file is
            missing.
    """

    def __init__(
        self,
        path: str = "~/.protoprompt/master.key",
        *,
        create: bool = True,
    ) -> None:
        self._path = Path(path).expanduser()
        self._create = create

    def get(self) -> bytes:
        if self._path.exists():
            return _validate_key(_from_str(self._path.read_text(encoding="utf-8")))

        if not self._create:
            raise SecretKeyError(f"key file {self._path} does not exist")

        key = generate_key()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(_to_str(key), encoding="utf-8")
        try:
            os.chmod(self._path, 0o600)
        except OSError:
            pass  # Windows: ACLs, not POSIX modes
        return key

    def rotate(self, new: bytes) -> None:
        new = _validate_key(new)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_name(self._path.name + ".tmp")
        temporary.write_text(_to_str(new), encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        os.replace(temporary, self._path)


class KeyringKeyProvider:
    """Store the master key in the OS keychain via ``keyring``.

    On the first call the key is generated and persisted. When ``keyring``
    is missing, has no backend, or is read-only, the configured
    ``fallback`` provider is used instead (default:
    :class:`EnvKeyProvider`).
    """

    def __init__(
        self,
        service: str = "protoprompt",
        username: str = "master-key",
        *,
        fallback: KeyProvider | None = None,
    ) -> None:
        self._service = service
        self._username = username
        self._fallback: KeyProvider = fallback or EnvKeyProvider()
        self._backend: str | None = None

    def get(self) -> bytes:
        try:
            import keyring  # noqa: PLC0415
        except ImportError:
            self._backend = "fallback"
            return self._fallback.get()

        try:
            stored = keyring.get_password(self._service, self._username)
        except Exception:
            self._backend = "fallback"
            return self._fallback.get()

        if stored:
            self._backend = "keyring"
            return _validate_key(_from_str(stored))

        key = generate_key()
        try:
            keyring.set_password(self._service, self._username, _to_str(key))
            self._backend = "keyring"
            return key
        except Exception:
            # Backend missing or read-only: the generated key would be
            # lost, so defer to the fallback and let it own the key.
            self._backend = "fallback"
            return self._fallback.get()

    def rotate(self, new: bytes) -> None:
        new = _validate_key(new)
        if self._backend is None:
            self.get()
        if self._backend == "fallback":
            self._fallback.rotate(new)
            return
        try:
            import keyring  # noqa: PLC0415
        except ImportError:
            raise SecretKeyError("keyring disappeared after it supplied the key")
        try:
            keyring.set_password(self._service, self._username, _to_str(new))
        except Exception as exc:
            raise SecretKeyError("failed to rotate key in the active keyring") from exc
