# Secrets

Secrets (tokens, keys, credentials) live in a dedicated subsystem,
`protoprompt.secrets`. They are **never** placed in the vector store, never
embedded, and never injected into prompts automatically: access is only
through an explicit, scope-pinned call.

## Why separate

- Profile and memory live in the store and take part in embeddings — a
  secret there would leak on any semantic search.
- A secret must be encrypted at rest and have a lifetime (TTL).
- An agent in session `X` must not see session `Y`'s secrets.

## Encryption key

The master key (KEK) is not stored next to the data. A `KeyProvider` owns it:

| Provider              | Where the key lives                                                   |
|-----------------------|-----------------------------------------------------------------------|
| `KeyringKeyProvider`  | OS keychain (Windows DPAPI / macOS Keychain / Linux Secret Service). Generates the key on first use; falls back when no backend exists. |
| `EnvKeyProvider`      | The `PROTOPROMPT_MASTER_KEY` environment variable (CI/containers).    |
| `FileKeyProvider`     | A file (default `~/.protoprompt/master.key`, mode `0600`).            |

The user never picks a password — the key is random, the OS provides security.

## Storage

`EncryptedSqliteSecretStore` encrypts each secret **individually** (Fernet:
authenticated encryption with an embedded timestamp). This gives:

- **TTL** — native: `put(..., ttl=3600)` and an hour later `get` returns `None`.
- **Scope isolation** — the `(scope, key)` pair is the identity, matched exactly.
- **Rotation** — `rotate_key()` re-encrypts everything under a new key.

```python
from protoprompt.secrets import EncryptedSqliteSecretStore, FileKeyProvider

vault = EncryptedSqliteSecretStore(
    "secrets.db",
    key_provider=FileKeyProvider("~/.protoprompt/master.key"),
)
vault.put("github_token", "ghp_...", scope="ilya:myapp", ttl=3600)
vault.get("github_token", scope="ilya:myapp")     # "ghp_..."
vault.get("github_token", scope="mallory:myapp")  # None — different scope
```

The database file stores the encrypted value, not plaintext.

## Access for an agent

An agent does not touch the vault directly. The host registers trusted
operations on a scope-pinned `SecretAccess`; the agent selects only an
operation name and arguments while the credential stays inside the callback:

```python
from protoprompt.secrets import SecretAccess

def github_identity(token: str, *, login: str) -> dict:
    # A real implementation performs the authenticated request here.
    return {"login": login, "authenticated": token.startswith("ghp_")}

access = SecretAccess(
    vault,
    scope="ilya:myapp",
    operations={"github_identity": github_identity},
)
result = await access.execute(
    "github_token", "github_identity", login="octocat"
)
```

`execute` logs only the operation, key name, and scope — never the value. An
agent cannot widen its own scope. The old `grant()` is retained only as a
deprecated escape hatch for trusted host code; never expose it as an LLM tool
or place its result in model messages.

## Boundaries

- Secrets are **credentials only**. Sensitive profile facts (address, age)
  are a different concern; do not mix them with the vault.
- The profile stores only the **presence** of a secret (`secret_ref`); the
  value lives only in the vault.
