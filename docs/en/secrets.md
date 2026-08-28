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

## AWS and Google Cloud stores

Managed backends implement the same `SecretStore` contract:

```bash
pip install "protoprompt[aws-secrets]"
pip install "protoprompt[gcp-secrets]"
```

```python
from protoprompt.integrations import (
    AWSSecretsManagerStore,
    GCPSecretManagerStore,
)

aws = AWSSecretsManagerStore(prefix="protoprompt/prod", region_name="eu-central-1")
gcp = GCPSecretManagerStore("my-project", prefix="protoprompt-prod")
```

AWS uses boto3's default credential chain; GCP uses Application Default
Credentials. Inject an official client to select a custom endpoint, workload
identity, signer, retry policy, or emulator. Injected clients remain host-owned.

Resource names contain only hashes of scope and key. Plain scope, key, value,
and expiry live inside the provider-encrypted payload. A mismatched or malformed
payload fails closed with `CloudSecretDataError`. The common 64 KiB provider
limit is checked before a network request.

TTL is enforced by protoprompt's envelope: expired values and keys are omitted,
but an expired provider version remains available to authorized cloud operators
until their retention policy removes it. This is access expiry, not cryptographic
erasure.

AWS deletion uses a seven-day recovery window by default. A later `put` restores
the scheduled resource before writing a new version. Set
`force_delete_without_recovery=True` only when irreversible deletion is intended.
`ListSecrets` is eventually consistent across processes; writes made through the
current store instance are overlaid immediately. GCP list operations are strongly
consistent, while `delete` permanently removes the secret resource.

For least privilege, grant only get/put/create/list/delete/restore operations for
the chosen prefix (AWS) or the corresponding Secret Manager roles on the selected
project/secrets (GCP). Model-facing code should still use `SecretAccess.execute`.

### Migration and rollback

Copy one scope at a time from the encrypted SQLite store, verify key names and
readback without logging values, then switch the host's store factory. Keep the
SQLite vault and its key read-only for the rollback window. Rolling back is a
factory/configuration change; cloud versions are not copied back automatically.

Provider SDK major updates require the secret contract and an explicitly enabled
live test. IAM behavior changes are documented before widening version ranges.
Run `examples/cloud_secret_store.py` only in a test account: it creates and then
deletes one resource without printing its value.
