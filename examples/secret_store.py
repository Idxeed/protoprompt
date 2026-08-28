"""Демо вольта секретов: scope-изоляция, TTL и шифрование at rest.

Показывает, что:
- секреты шифруются поштучно (Fernet), в БД лежит не plaintext;
- доступ привязан к scope (агент одной сессии не видит чужое);
- значения не попадают в логи.

    python examples/secret_store.py

База и master key создаются во временном каталоге и удаляются после запуска;
в production FileKeyProvider обычно заменяют на KeyringKeyProvider.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
import tempfile

from protoprompt.secrets import (
    EncryptedSqliteSecretStore,
    FileKeyProvider,
    SecretAccess,
)


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="protoprompt-secrets-") as directory:
        root = Path(directory)
        database = root / "secrets.db"
        vault = EncryptedSqliteSecretStore(
            str(database),
            key_provider=FileKeyProvider(str(root / "master.key")),
        )

        def authenticate(token: str) -> dict:
            return {"authenticated": token.startswith("ghp_")}

        operations = {"authenticate": authenticate}
        ilya = SecretAccess(vault, scope="ilya:myapp", operations=operations)
        mallory = SecretAccess(vault, scope="mallory:myapp", operations=operations)

        await ilya.store("github_token", "ghp_demo_secret", ttl=3600)

        print("Илья выполняет запрос :", await ilya.execute("github_token", "authenticate"))
        print("Маллори выполняет     :", await mallory.execute("github_token", "authenticate"))

        inspection = sqlite3.connect(str(database))
        try:
            raw = inspection.execute("SELECT token FROM secrets").fetchone()[0]
        finally:
            inspection.close()
        print("\nсырой токен в БД :", raw)
        print("secret в plaintext:", "ghp_demo_secret" in raw)

        vault.close()


if __name__ == "__main__":
    asyncio.run(main())
