from __future__ import annotations

import os

import pytest
from cryptography.fernet import Fernet

from protoprompt.secrets.errors import SecretKeyError
from protoprompt.secrets.key import (
    EnvKeyProvider,
    FileKeyProvider,
    KeyringKeyProvider,
    _from_str,
    _to_str,
    generate_key,
)


def test_generate_key_is_valid_fernet_key():
    Fernet(generate_key())  # must not raise


def test_env_provider_reads_var(monkeypatch):
    key = _to_str(generate_key())
    monkeypatch.setenv("PROTOPROMPT_MASTER_KEY", key)
    assert EnvKeyProvider().get() == _from_str(key)


def test_env_provider_missing_raises(monkeypatch):
    monkeypatch.delenv("PROTOPROMPT_MASTER_KEY", raising=False)
    with pytest.raises(SecretKeyError):
        EnvKeyProvider().get()


def test_env_provider_rejects_invalid_key(monkeypatch):
    monkeypatch.setenv("PROTOPROMPT_MASTER_KEY", "not-a-fernet-key")
    with pytest.raises(SecretKeyError):
        EnvKeyProvider().get()


def test_env_provider_rotate(monkeypatch):
    monkeypatch.setenv("PROTOPROMPT_MASTER_KEY", _to_str(generate_key()))
    p = EnvKeyProvider()
    new = generate_key()
    p.rotate(new)
    assert p.get() == new


def test_file_provider_creates_and_is_stable(tmp_path):
    path = tmp_path / "master.key"
    p = FileKeyProvider(str(path))
    first = p.get()
    assert path.exists()
    assert p.get() == first


def test_file_provider_missing_raises(tmp_path):
    p = FileKeyProvider(str(tmp_path / "nope"), create=False)
    with pytest.raises(SecretKeyError):
        p.get()


def test_file_provider_rotate(tmp_path):
    p = FileKeyProvider(str(tmp_path / "master.key"))
    p.get()
    new = generate_key()
    p.rotate(new)
    assert p.get() == new


def test_keyring_happy_path(monkeypatch):
    import keyring

    storage: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(keyring, "get_password", lambda s, u: storage.get((s, u)))
    monkeypatch.setattr(
        keyring, "set_password", lambda s, u, v: storage.__setitem__((s, u), v)
    )

    p = KeyringKeyProvider(service="svc", username="usr")
    key = p.get()
    assert p.get() == key
    assert storage[("svc", "usr")] == _to_str(key)


def test_keyring_falls_back_on_readonly_backend(monkeypatch, tmp_path):
    import keyring

    def boom(*a, **k):
        raise RuntimeError("read-only backend")

    monkeypatch.setattr(keyring, "get_password", lambda *a, **k: None)
    monkeypatch.setattr(keyring, "set_password", boom)

    fallback = FileKeyProvider(str(tmp_path / "fallback.key"))
    p = KeyringKeyProvider(fallback=fallback)
    assert p.get() == fallback.get()


def test_keyring_rotation_does_not_switch_backend(monkeypatch):
    import keyring

    current = _to_str(generate_key())
    monkeypatch.setattr(keyring, "get_password", lambda *a, **k: current)

    def boom(*a, **k):
        raise RuntimeError("read-only")

    monkeypatch.setattr(keyring, "set_password", boom)
    provider = KeyringKeyProvider()
    provider.get()
    with pytest.raises(SecretKeyError, match="active keyring"):
        provider.rotate(generate_key())
