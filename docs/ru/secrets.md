# Секреты

Секреты (токены, ключи, credentials) — отдельная подсистема
`protoprompt.secrets`. Они **никогда** не попадают в векторный стор, не
эмбеддятся и не вставляются в промпт автоматически: доступ только через
явный вызов, привязанный к scope.

## Почему отдельно

- Профиль и память живут в сторе и участвуют в эмбеддингах — секрет там
  утёк бы при любом поиске по смыслу.
- Секрет должен быть зашифрован at rest и иметь срок жизни (TTL).
- Агент сессии `X` не должен видеть секреты сессии `Y`.

## Ключ шифрования

Мастер-ключ (KEK) не хранится рядом с данными. Его даёт `KeyProvider`:

| Провайдер             | Где ключ                                              |
|-----------------------|-------------------------------------------------------|
| `KeyringKeyProvider`  | OS-keystore (Windows DPAPI / macOS Keychain / Linux Secret Service). Генерирует ключ при первом запуске, при отсутствии бэкенда — fallback. |
| `EnvKeyProvider`      | Переменная окружения `PROTOPROMPT_MASTER_KEY` (CI/контейнеры). |
| `FileKeyProvider`     | Файл (по умолчанию `~/.protoprompt/master.key`, права `0600`). |

Пользователь не придумывает пароль — ключ случайный, безопасность даёт ОС.

## Хранилище

`EncryptedSqliteSecretStore` шифрует каждый секрет **отдельно** (Fernet:
аутентичное шифрование со встроенной меткой времени). Отсюда:

- **TTL** — нативная: `put(..., ttl=3600)` и через час `get` вернёт `None`.
- **Scope-изоляция** — ключ пары `(scope, key)`, совпадение точное.
- **Ротация** — `rotate_key()` перешифровывает всё под новый ключ.

```python
from protoprompt.secrets import EncryptedSqliteSecretStore, FileKeyProvider

vault = EncryptedSqliteSecretStore(
    "secrets.db",
    key_provider=FileKeyProvider("~/.protoprompt/master.key"),
)
vault.put("github_token", "ghp_...", scope="ilya:myapp", ttl=3600)
vault.get("github_token", scope="ilya:myapp")     # "ghp_..."
vault.get("github_token", scope="mallory:myapp")  # None — другой scope
```

В файле БД значение лежит в зашифрованном виде, а не plaintext.

## Доступ для агента

Агент работает не с вольтом напрямую, а через `SecretAccess`, закреплённый
за одним scope. Host-приложение заранее регистрирует разрешённые операции;
агент выбирает только имя операции и аргументы, а credential остаётся внутри
trusted callback:

```python
from protoprompt.secrets import SecretAccess

def github_identity(token: str, *, login: str) -> dict:
    # Здесь выполняется реальный запрос с Authorization; токен не возвращаем.
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

`execute` пишет в лог только операцию, имя ключа и scope — не значение.
Поменять scope «на ходу» агент не может. Старый `grant()` оставлен только как
deprecated escape hatch для доверенного host-кода; его нельзя выставлять как
LLM tool или включать его результат в сообщения модели.

## Границы

- Секреты — **только credentials**. Чувствительные факты профиля (адрес,
  возраст) — это отдельный разговор; не мешайте их с вольтом.
- В профиле хранится лишь **факт наличия** секрета (`secret_ref`), значение —
  только в вольте.
