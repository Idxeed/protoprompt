# Роадмапа: движок профиля + секреты

> Зафиксировано. Версия плана: v2. Статус решений — см. раздел 5.

## 0. Принцип (три сущности в конфликте)

Три независимых «голоса», каждый со своим жизненным циклом, и **ни один не может
молча протащить своё в другой**:

| Сущность         | Что хранит                  | Жизненный цикл           | Как попадает в модель       |
|------------------|-----------------------------|--------------------------|-----------------------------|
| **ContextBuilder** | сборка на вызов           | stateless                | напрямую в промпт           |
| **ProfileEngine**  | долговечные факты о юзере | stateful, merge + версии | рендер в `system_prompt`    |
| **SecretVault**    | credentials               | stateful, TTL + rotation | **никогда** — только guarded-tool |

Правила неразмыкаемости:

- секрет никогда не эмбеддится и не живёт в `StoreProtocol`;
- профиль хранит только **факт наличия** секрета (`secret_ref`), значение — в вольте;
- контекст не знает про вольт, вольт не знает про контекст.

## 1. Целевая архитектура

```
сигналы (message / tool_result / feedback / ...)
        │
        v
+-------------------+     +-------------------+
|  ProfileEngine    |     |  SecretVault      |
| Signal → extract  |     | SecretStore       |
| → FactOp merge    |     |  ├ KeyProvider    |
| → ProfileStore    |     |  └ Cipher (Fernet)|
+--------+----------+     +---------+---------+
         |                          |
         v                          v
   ProfileRenderer           GuardedTool (агент просит
         │                   ключ под 1 действие)
         v
+-------------------+
|  ContextBuilder   |  ←— system_prompt
+-------------------+

общие примитивы: MemoryLifecycle (scorer/eviction/summary) —
  переиспользуются ProfileEngine и agent.WorkingMemory
```

## 2. Подсистема A — профиль (кросс-сессионный движок)

### 2.1 Модель данных — `profile/types.py`

```python
@dataclass
class Traits:
    style: str = ""          # concise | balanced | detailed
    expertise: str = ""      # beginner | intermediate | expert
    verbosity: str = ""      # concise | balanced | detailed
    formality: str = ""      # casual | neutral | formal

@dataclass
class Preferences:
    format: str = ""         # bullets | narrative | code_heavy | mixed
    language: str = ""       # ru | en | ...
    topics: list[str] = field(default_factory=list)

@dataclass
class UserProfile:
    user_id: str
    traits: Traits = field(default_factory=Traits)
    preferences: Preferences = field(default_factory=Preferences)
    facts: dict[str, str] = field(default_factory=dict)  # открытый набор: name, role, tech_stack
    summary: str = ""
    updated_at: str = ""     # ISO-8601
    version: int = 0         # монотонный, +1 на каждый merge
    source: str = ""
```

```python
@dataclass
class Signal:
    user_id: str
    kind: str                # message | tool_result | feedback | ...
    text: str
    role: str = ""
    ts: str = ""
    meta: dict = field(default_factory=dict)

@dataclass
class FactOp:
    op: str                  # add | update | forget
    key: str
    value: str = ""

@dataclass
class ProfileDelta:
    fact_ops: list[FactOp] = field(default_factory=list)
    traits: dict[str, str] = field(default_factory=dict)
    preferences: dict[str, str] = field(default_factory=dict)
    summary: str = ""
    source: str = ""
```

Ключевое отличие от старого `ProfileBuilder`: вместо «модель переписала summary» —
**явные операции памяти** `add/update/forget` над именованными фактами. Это даёт
детерминированный merge, UI provenance и задел под decay.

### 2.2 Контракты

```python
# profile/source.py
class ProfileProtocol(Protocol):
    async def extract(self, user_id: str, signals: list[Signal]) -> ProfileDelta: ...
# LLMProfileSource(llm, language="ru", max_signals=20)  — retry при битом JSON
# RuleProfileSource()                                   — детерминированный, без LLM
# CompositeProfileSource(sources, conflict="first_nonempty")

# profile/codec.py
def parse_profile_json(text: str) -> dict[str, Any]      # fenced JSON, первый {...}, типы
def coerce_profile(raw: dict) -> ProfileDelta            # enum RU→EN, отброс мусора

# profile/merge.py
def merge(profile: UserProfile, delta: ProfileDelta, *, now: str) -> UserProfile
#   fact_ops применяются к facts (forget → удалить); traits/preferences non-empty wins;
#   summary non-empty перезаписывает; version += 1; updated_at = now; source = delta.source

# profile/store.py + profile/async_store.py
class ProfileStore(Protocol):
    def get(self, user_id) -> UserProfile | None: ...
    def put(self, profile) -> None: ...
    def delete(self, user_id) -> None: ...
# InMemoryProfileStore / SqliteProfileStore(user_id PK, json, updated_at, version)
# AsyncProfileStore + as_async_profile()

# profile/manager.py
class ProfileManager:
    async def update(self, user_id, signals) -> UserProfile  # get→extract→coerce→merge→put
    async def get(self, user_id) -> UserProfile | None
    async def reset(self, user_id) -> UserProfile            # fresh с нуля
    async def delete(self, user_id) -> None

# profile/render.py
def render(profile, *, language="ru") -> str                 # единый заголовок секции
```

## 3. Подсистема B — секреты (credentials только)

### 3.1 Контракты

```python
# secrets/key.py
class KeyProvider(Protocol):
    def get(self) -> bytes: ...
    def rotate(self, new: bytes) -> None: ...

class KeyringKeyProvider: ...   # default: OS-keystore (Windows DPAPI / macOS Keychain / Linux Secret Service)
class EnvKeyProvider: ...       # CI / docker
class FileKeyProvider: ...      # self-managed, chmod 600 / ACL
# будущее: TPMProvider, SecureEnclaveProvider, VaultProvider

# secrets/store.py
class SecretStore(Protocol):
    def get(self, key: str, *, scope: str) -> str | None: ...
    def put(self, key: str, value: str, *, scope: str, ttl: int | None = None) -> None: ...
    def delete(self, key: str, *, scope: str) -> None: ...
    def list_keys(self, *, scope: str) -> list[str]: ...

class EncryptedSqliteSecretStore(SecretStore):
    # Fernet, каждый секрет — отдельный токен (своя TTL, своя ротация)
```

### 3.2 Ключевые решения

- **Cipher:** `cryptography.Fernet` — authenticated, встроенный timestamp → **нативная TTL**,
  без возни с IV.
- **KEK:** `keyring` по умолчанию — при первом запуске генерим случайный 32-байтный ключ
  в OS-keystore, **юзер не знает пароль**, безопасность даёт ОС.
- **Scope:** строка `f"{user_id}:{project}"`, точное совпадение, без wildcard в v1.
  `DEFAULT_SCOPE = "default"`. Агент сессии `X` не видит секреты сессии `Y` — заложено с
  первого дня.

### 3.3 Guarded-tool (как агент получает секрет)

```python
# secrets/guard.py — НЕ часть SecretStore, а шов к агенту
class SecretAccess:
    def __init__(self, store, *, scope, operations): ...
    async def execute(self, key: str, operation: str, **kwargs): ...
    # credential получает только заранее зарегистрированный trusted callback;
    # агенту возвращается результат операции, значение не попадает в tool result
```

Доступ только через явный вызов, никогда через RAG/embedding/промпт.

## 4. Фазы работ

### A. Профиль-движок

| Фаза | Что делаем | Edge cases | Приёмка |
|------|-----------|-----------|---------|
| **A0** | `types.py` (Traits/Preferences/UserProfile/Signal/FactOp/ProfileDelta); `schema.py` → `schema.json` + loader; `codec.py` | fenced JSON, JSON в тексте, `null`/неверные типы, RU/EN enum, пустой ответ | `test_codec.py`: таблица вход→дельта |
| **A1** | `source.py`: `ProfileProtocol`, `LLMProfileSource` (+retry→fallback), `RuleProfileSource`, `CompositeProfileSource` | битый JSON → 1 retry → RuleSource; пустая дельта | `test_source.py` |
| **A2** | `merge.py`: FactOp-применение, non-empty wins, `version+=1`, `updated_at` | forget несуществующего факта; topics без дублей; пустая дельта не меняет `version` | `test_merge.py` |
| **A3** | `store.py` + `async_store.py`: `InMemoryProfileStore`, `SqliteProfileStore`, `as_async_profile` | апсёрт, удаление, async не блокирует loop | `test_profile_store.py` |
| **A4** | `manager.py`: `update/get/reset/delete` | первый update создаёт; второй сливает; сбой LLM → профиль не теряется; reset обнуляет version | `test_manager.py` |
| **A5** | `render.py` + `ContextInput.profile: UserProfile \| None`; вынести хардкод `"Профиль пользователя:\n"` из `injector.py:78` и `injector_budgeted.py:104`; i18n секций (профиль + `"История диалога"`) | приоритет `profile` > `profile_text`; EN-локаль | `test_integration.py` |
| **A6** | депрекация: `ProfileBuilder` → алиас с `DeprecationWarning`; обновить `__init__.py`, README, docs, demo | старые вызовы не падают | smoke-тест |

### B. Вольт секретов

| Фаза | Что делаем | Edge cases | Приёмка |
|------|-----------|-----------|---------|
| **B7** | `secrets/key.py`: `KeyProvider`, `KeyringKeyProvider`, `EnvKeyProvider`, `FileKeyProvider` | нет keyring (headless) → fallback env/file; ключ создаётся один раз | `test_key_provider.py` |
| **B8** | `secrets/store.py`: `EncryptedSqliteSecretStore` (Fernet, поштучно, scope, TTL) | TTL-протухание; scope-изоляция (`X` не читает `Y`); ротация ключа через `MultiFernet` | `test_secret_store.py` |
| **B9** | `secrets/guard.py`: `SecretAccess.execute()` с host-registered operations; **никогда не логировать/возвращать credential агенту** | injection-сценарий: модель просит чужой scope или незарегистрированную операцию → отказ | `test_secret_guard.py` |

### C. Общее

| Фаза | Что делаем |
|------|-----------|
| **C10** | (опц.) рефакторинг `MemoryLifecycle`: вынести scorer/eviction/summary в общий слой, переиспользовать в `agent.WorkingMemory` и будущем decay профиля. Не блокирует A/B. |
| **C11** | docs RU/EN, CHANGELOG, `examples/profile.py` + `examples/secrets.py`, полный прогон pytest/coverage |

## 5. Решения

| # | Решение | Статус |
|---|---------|--------|
| D1 | Типизированные `Traits`/`Preferences` + открытый `facts` | утверждено |
| D2 | `summary` перезаписывается non-empty, регенерация — опция позже | утверждено |
| D3 | Конфликт traits: newest-wins, без confidence в v1 | утверждено |
| D4 | Decay/aging — out of scope v1 (версионирование готово) | утверждено |
| D5 | `schema.json` + тонкий loader `schema.py` | утверждено |
| D6 | Имя оркестратора `ProfileManager`; `ProfileBuilder` → алиас | утверждено |
| D7 | Scope = `f"{user_id}:{project}"`, точное совпадение, `DEFAULT_SCOPE` | утверждено |
| D8 | Мастер-ключ: `keyring` (default) + `Fernet`; TPM/KMS — будущие адаптеры | утверждено |
| D9 | Факты как явные `FactOp add/update/forget` вместо freeform summary | утверждено |

## 6. Риски

- **Breaking change** на `UserProfile` (dict → typed + facts) — гасится фазой A6.
- **Retry удваивает LLM-затраты** — только при битом JSON.
- **Scope слишком грубый** — если позже нужен wildcard/наследование, меняем `SecretStore` за
  `SecretAccess`, не за вольт.
- **Слияние ProfileEngine и WorkingMemory** — отложить до C10, иначе размазываем скоуп.

## 7. Definition of done

- [x] `ProfileEngine`: signals → FactOp → merge → `ProfileStore` → render → `ContextInput.profile`
- [x] `SecretVault`: `KeyProvider` + Fernet + scope + TTL + guarded-tool, ноль автоматических утечек в логи/промпт/эмбеддинги
- [x] депрекация `ProfileBuilder` без поломки существующих вызовов
- [x] i18n секций, схема согласована с промптом
- [x] тесты всех фаз, docs RU/EN, CHANGELOG, примеры

---

# Роадмапа v3: RAG-движок

## Решения (подтверждены)

- **R1.** Полный скоуп: загрузка (чанкер + индекс) + поиск.
- **R2.** Структурные чанки: \RetrievedChunk\ (doc_id, index, text, score).
- **R3.** Режимы поиска: \doc_ids\ (фильтр) И \None\ (весь стор).
- **R4.** Rerank в скоупе: \RerankerProtocol\ + \NoOpReranker\ (default) + \LLMReranker\.
- **R5.** Namespace: \kind\-метка в metadata (\document\ / \session\), поиск по всему стору фильтрует \kind=document\.
- **R6.** Чанкеры: \FixedSizeChunker\, \ParagraphChunker\, \TokenChunker\.

## Фазы

| Фаза | Что |
|------|-----|
| R0 | types (Document, RetrievedChunk) + тесты |
| R1 | chunker.py (ChunkerProtocol + 3 реализации) + тесты |
| R2 | indexer.py (DocumentIndexer: chunk → embed → index, kind=document) + тесты |
| R3 | reranker.py (NoOp + LLMReranker) + тесты |
| R4 | retriever.py (query + score_threshold + doc_ids/search-all + rerank + provenance) + тесты |
| R5 | интеграция: ContextInput(doc_ids=None, score_threshold), ContextOutput.rag_chunks, builders на Retriever, Pipeline → kind=session + тесты |
| R6 | docs RU/EN, CHANGELOG, examples/rag.py, полный pytest/coverage |
