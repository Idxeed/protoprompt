"""protoprompt.secrets — scoped, encrypted credential storage.

Credentials are kept separate from the vector store and the profile: they
are never embedded or injected into prompts. Agent-facing code should use
host-registered :meth:`SecretAccess.execute` operations; plaintext
``grant`` access is retained only as a deprecated trusted-host escape hatch.

    from protoprompt.secrets import EncryptedSqliteSecretStore, FileKeyProvider

    vault = EncryptedSqliteSecretStore(
        "secrets.db", key_provider=FileKeyProvider("~/.protoprompt/master.key")
    )
    vault.put("github_token", "ghp_...", scope="ilya:myapp", ttl=3600)
    assert vault.get("github_token", scope="ilya:myapp") == "ghp_..."
"""

from protoprompt.secrets.errors import SecretKeyError
from protoprompt.secrets.guard import SecretAccess
from protoprompt.secrets.key import (
    EnvKeyProvider,
    FileKeyProvider,
    KeyProvider,
    KeyringKeyProvider,
    generate_key,
)
from protoprompt.secrets.store import (
    DEFAULT_SCOPE,
    EncryptedSqliteSecretStore,
    SecretStore,
)
from protoprompt.integrations._cloud_secret import CloudSecretDataError
from protoprompt.integrations.aws_secrets import AWSSecretsManagerStore
from protoprompt.integrations.gcp_secrets import GCPSecretManagerStore

__all__ = [
    "KeyProvider",
    "KeyringKeyProvider",
    "EnvKeyProvider",
    "FileKeyProvider",
    "generate_key",
    "SecretStore",
    "EncryptedSqliteSecretStore",
    "DEFAULT_SCOPE",
    "SecretAccess",
    "SecretKeyError",
    "CloudSecretDataError",
    "AWSSecretsManagerStore",
    "GCPSecretManagerStore",
]
