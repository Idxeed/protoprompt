from __future__ import annotations

import time
from contextlib import closing
import pytest

from protoprompt.secrets.key import FileKeyProvider, generate_key
from protoprompt.secrets.errors import SecretKeyError
from protoprompt.secrets.store import EncryptedSqliteSecretStore


@pytest.fixture
def store(tmp_path):
    key = FileKeyProvider(str(tmp_path / "master.key"))
    s = EncryptedSqliteSecretStore(":memory:", key_provider=key)
    yield s
    s.close()


def test_put_get_roundtrip(store):
    store.put("github", "ghp_token", scope="ilya:myapp")
    assert store.get("github", scope="ilya:myapp") == "ghp_token"


def test_put_overwrites(store):
    store.put("github", "old", scope="s")
    store.put("github", "new", scope="s")
    assert store.get("github", scope="s") == "new"


def test_scope_isolation(store):
    store.put("github", "token", scope="ilya:myapp")
    assert store.get("github", scope="someone:myapp") is None
    assert store.get("github", scope="ilya:other") is None


def test_missing_key_returns_none(store):
    assert store.get("nope", scope="s") is None


def test_delete(store):
    store.put("k", "v", scope="s")
    store.delete("k", scope="s")
    assert store.get("k", scope="s") is None


def test_list_keys_sorted(store):
    store.put("b", "2", scope="s")
    store.put("a", "1", scope="s")
    assert store.list_keys(scope="s") == ["a", "b"]
    assert store.list_keys(scope="other") == []


def test_ttl_expiry_rejects_and_cleans(store):
    store.put("k", "v", scope="s", ttl=-1)  # already expired at creation
    assert store.get("k", scope="s") is None
    assert store.list_keys(scope="s") == []  # lazily cleaned


def test_default_ttl_applied(tmp_path):
    key = FileKeyProvider(str(tmp_path / "master.key"))
    store = EncryptedSqliteSecretStore(":memory:", key_provider=key, ttl=-1)
    store.put("k", "v", scope="s")
    assert store.get("k", scope="s") is None
    store.close()


def test_rotate_key_reencrypts(store):
    store.put("a", "1", scope="s")
    store.put("b", "2", scope="s")
    store.rotate_key(generate_key())
    assert store.get("a", scope="s") == "1"
    assert store.get("b", scope="s") == "2"


def test_rotate_key_preserves_original_ttl_timestamp(store):
    expired = store._fernet.encrypt_at_time(b"value", int(time.time()) - 100)
    store._conn.execute(
        "INSERT INTO secrets (scope, key, token, ttl) VALUES (?, ?, ?, ?)",
        ("s", "expired", expired.decode("ascii"), 10),
    )
    store._conn.commit()

    store.rotate_key(generate_key())

    assert store.get("expired", scope="s") is None


def test_rotation_recovers_when_provider_failed_before_update(tmp_path):
    class FailOnceProvider:
        def __init__(self):
            self.key = generate_key()
            self.fail = True

        def get(self):
            return self.key

        def rotate(self, new):
            if self.fail:
                self.fail = False
                raise RuntimeError("provider down")
            self.key = new

    provider = FailOnceProvider()
    path = str(tmp_path / "recover-before.db")
    store = EncryptedSqliteSecretStore(path, key_provider=provider)
    store.put("a", "secret", scope="s")
    with pytest.raises(SecretKeyError, match="recoverable"):
        store.rotate_key()
    with pytest.raises(SecretKeyError, match="already pending"):
        store.rotate_key()
    store.close()

    recovered = EncryptedSqliteSecretStore(path, key_provider=provider)
    assert recovered.get("a", scope="s") == "secret"
    recovered.close()


def test_rotation_recovers_when_provider_updated_then_raised(tmp_path):
    class UpdateThenFailProvider:
        def __init__(self):
            self.key = generate_key()
            self.fail = True

        def get(self):
            return self.key

        def rotate(self, new):
            self.key = new
            if self.fail:
                self.fail = False
                raise RuntimeError("uncertain response")

    provider = UpdateThenFailProvider()
    path = str(tmp_path / "recover-after.db")
    store = EncryptedSqliteSecretStore(path, key_provider=provider)
    store.put("a", "secret", scope="s")
    with pytest.raises(SecretKeyError, match="recoverable"):
        store.rotate_key()
    store.close()

    recovered = EncryptedSqliteSecretStore(path, key_provider=provider)
    assert recovered.get("a", scope="s") == "secret"
    recovered.close()


def test_unencrypted_at_rest(store, tmp_path):
    import sqlite3

    path = str(tmp_path / "secrets.db")
    key = FileKeyProvider(str(tmp_path / "master.key"))
    store = EncryptedSqliteSecretStore(path, key_provider=key)
    store.put("github", "ghp_supersecret", scope="s")

    with closing(sqlite3.connect(path)) as connection:
        raw = connection.execute(
            "SELECT token FROM secrets"
        ).fetchone()[0]
    assert "ghp_supersecret" not in raw
    store.close()
