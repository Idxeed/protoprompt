"""Демо вольта секретов: scope-изоляция, TTL и шифрование at rest.

Показывает, что:
- секреты шифруются поштучно (Fernet), в БД лежит не plaintext;
- доступ привязан к scope (агент одной сессии не видит чужое);
- значения не попадают в логи.

    python examples/secrets.py

Ключ мастер-шифрования кладётся в `~/.protoprompt/demo_master.key`
(FileKeyProvider); в проде можно заменить на KeyringKeyProvider.
"""

from __future__ import annotations

import asyncio
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from protoprompt.secrets import (  # noqa: E402
    EncryptedSqliteSecretStore,
    FileKeyProvider,
    SecretAccess,
)

DB = Path(__file__).resolve().parent / "secrets_demo.db"


async def main() -> None:
    vault = EncryptedSqliteSecretStore(
        str(DB),
        key_provider=FileKeyProvider("~/.protoprompt/demo_master.key"),
    )

    def authenticate(token: str) -> dict:
        return {"authenticated": token.startswith("ghp_")}

    operations = {"authenticate": authenticate}
    ilya = SecretAccess(vault, scope="ilya:myapp", operations=operations)
    mallory = SecretAccess(vault, scope="mallory:myapp", operations=operations)

    await ilya.store("github_token", "ghp_demo_secret", ttl=3600)

    print("Илья выполняет запрос :", await ilya.execute("github_token", "authenticate"))
    print("Маллори выполняет     :", await mallory.execute("github_token", "authenticate"))

    raw = sqlite3.connect(str(DB)).execute(
        "SELECT token FROM secrets"
    ).fetchone()[0]
    print("\nсырой токен в БД :", raw)
    print("secret в plaintext:", "ghp_demo_secret" in raw)

    vault.close()


if __name__ == "__main__":
    asyncio.run(main())
