from __future__ import annotations

import pytest

from protoprompt.secrets.guard import SecretAccess
from protoprompt.secrets.key import FileKeyProvider
from protoprompt.secrets.store import EncryptedSqliteSecretStore


@pytest.fixture
def store(tmp_path):
    key = FileKeyProvider(str(tmp_path / "master.key"))
    s = EncryptedSqliteSecretStore(":memory:", key_provider=key)
    yield s
    s.close()


@pytest.mark.asyncio
async def test_grant_returns_value(store):
    store.put("github", "ghp_secret", scope="ilya:myapp")
    access = SecretAccess(store, scope="ilya:myapp")
    with pytest.deprecated_call(match="exposes plaintext"):
        assert await access.grant("github") == "ghp_secret"


@pytest.mark.asyncio
async def test_grant_denied_outside_scope(store):
    store.put("github", "ghp_secret", scope="ilya:myapp")
    # A different session/scope cannot read it.
    attacker = SecretAccess(store, scope="mallory:myapp")
    with pytest.deprecated_call():
        assert await attacker.grant("github") is None


@pytest.mark.asyncio
async def test_store_and_revoke(store):
    access = SecretAccess(store, scope="s")
    await access.store("token", "abc123")
    assert store.get("token", scope="s") == "abc123"
    await access.revoke("token")
    assert store.get("token", scope="s") is None


@pytest.mark.asyncio
async def test_value_never_logged(store, caplog):
    store.put("github", "ghp_super_secret_value", scope="s")
    access = SecretAccess(store, scope="s")
    with caplog.at_level("INFO"):
        with pytest.deprecated_call():
            await access.grant("github")
    assert "ghp_super_secret_value" not in caplog.text
    assert "key='github'" in caplog.text


@pytest.mark.asyncio
async def test_keys_lists_scope_only(store):
    store.put("a", "1", scope="s1")
    store.put("b", "2", scope="s2")
    access = SecretAccess(store, scope="s1")
    assert await access.keys() == ["a"]


@pytest.mark.asyncio
async def test_execute_keeps_plaintext_inside_host_operation(store, caplog):
    store.put("github", "ghp_super_secret_value", scope="s")

    def authenticated_identity(token: str, *, login: str) -> dict:
        assert token == "ghp_super_secret_value"
        return {"login": login, "authenticated": True}

    access = SecretAccess(
        store,
        scope="s",
        operations={"github_identity": authenticated_identity},
    )
    with caplog.at_level("INFO"):
        result = await access.execute(
            "github", "github_identity", login="octocat"
        )

    assert result == {"login": "octocat", "authenticated": True}
    assert access.operation_names() == ["github_identity"]
    assert "ghp_super_secret_value" not in caplog.text


@pytest.mark.asyncio
async def test_execute_rejects_unregistered_operation(store):
    store.put("github", "secret", scope="s")
    access = SecretAccess(store, scope="s")
    with pytest.raises(ValueError, match="unknown secret operation"):
        await access.execute("github", "exfiltrate")
