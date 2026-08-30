# Профиль пользователя

Модель забывает всё между сессиями. Память чат-бота
([memory.md](memory.md)) хранит диалог, но и она живёт внутри одного чата.
А факты о человеке — как его зовут, какой у него стек, любит ли он списки
или развёрнутые ответы — должны переживать и смену сессий, и смену чатов.

За это отвечает **профиль пользователя**: долговременная память о человеке,
которая копится инкрементально и подкладывается в промпт при каждой встрече.

## Чем профиль отличается от памяти сессии

| | Память сессии (`session`) | Профиль (`profile`) |
|---|---|---|
| Что хранит | Переписку одного чата | Факты о человеке |
| Живёт | Внутри сессии | Между сессиями |
| Когда обновляется | При сжатии диалога | На каждой встрече (или по расписанию) |
| Как попадает в промпт | Векторный поиск по `session_{chat_id}` | Готовый текст в `system_prompt` |

Профиль — это не «пересказ задачи». Это **долговечные факты и
предпочтения**: имя, роль, стек, стиль общения.

## Модель данных

Профиль (`UserProfile`) состоит из трёх корзин:

```python
from protoprompt.profile import UserProfile

profile = UserProfile(
    user_id="u1",
    traits=Traits(style="concise", expertise="expert"),
    preferences=Preferences(format="bullets", language="ru", topics=["ai", "rag"]),
    facts={"name": "Илья", "stack": "python"},
    summary="Опытный Python-разработчик",
    version=3,            # растёт на каждом изменении
    source="llm",         # кто последним менял профиль
)
```

- **`traits`** — стиль общения (закрытые значения-энумы):
  `style` (concise/balanced/detailed), `expertise`
  (beginner/intermediate/expert), `verbosity`, `formality`.
- **`preferences`** — предпочтения по формату ответа: `format`, `language`,
  `topics` (список тем).
- **`facts`** — открытый словарь именованных фактов
  (`name`, `role`, `stack`, ...). Меняется через явные операции.
- **`summary`** — свободная выжимка «кто этот человек».

Значения traits/preferences жёстко заданы в `schema.json` — модель не может
выдумать свои.

## Сигналы и дельты

Профиль меняется **инкрементально**, а не переписывается с нуля.

- **`Signal`** — один входящий сигнал (сообщение, результат инструмента,
  обратная связь). Именно из сигналов извлекаются факты.
- **`FactOp`** — явная операция над фактом: `add`, `update` или `forget`.
- **`ProfileDelta`** — результат одного прогона источника: набор операций
  над фактами + новые traits/preferences + summary.

Схема потока простая:

```
сигналы (сообщения) → источник → дельта → merge в профиль → сохранить
```

## Источники: откуда берётся дельта

Источник (`ProfileProtocol`) превращает список сигналов в дельту. Есть три
встроенных:

| Источник | Что делает | LLM? |
|---|---|---|
| `RuleProfileSource` | Детерминированные правила: длина сообщений, алфавит, маркеры вежливости | нет |
| `LLMProfileSource` | Просит модель вернуть JSON с фактами и предпочтениями | да |
| `CompositeProfileSource` | Запускает несколько источников и складывает их дельты | зависит |

### LLMProfileSource и «грязный» JSON

Настоящие модели редко выдают идеальный JSON: оборачивают в markdown,
вставляют в текст, пишут русские метки («эксперт» вместо `expert`) или
вообще отказываются. Защита от этого — **кодек**:

1. `parse_profile_json` — вытаскивает первый JSON-объект из любого текста
   (из markdown-блоков, из прозы).
2. `coerce_profile` — нормализует: русские метки → канонические энумы
   («списки» → `bullets`), ключи фактов → стабильные слаги
   («Имя» → `imya`), мусор отбрасывается.
3. `slugify` — транслитерация кириллицы в ASCII-слаг.

Если JSON так и не получился — источник делает **один ретрай**, а потом
откатывается на fallback (по умолчанию правила). Профиль не должен ломаться
из-за каприза модели.

Настройки нормализации живут в `CodecProfile`:

- `DEFAULT_PROFILE` — терпимый, для локальных маленьких моделей
  (транслитерация ключей, перевод русских меток);
- `STRICT_PROFILE` — для моделей, которые уже отдают канонические
  английские ключи и энумы.

## Merge: как дельта вливается в профиль

`merge(profile, delta)` возвращает новый профиль. Семантика:

- операции `add`/`update` ставят факт, `forget` удаляет (отсутствующий
  ключ — не ошибка);
- `traits`/`preferences` — новое значение перезаписывает старое, пустое
  игнорируется;
- `topics` — полная замена списка (с удалением дублей); `None` — без
  изменений, `[]` — очистить;
- `summary` перезаписывается непустым;
- `version` увеличивается и `updated_at`/`source` обновляются только когда
  дельта реально что-то изменила.

Пустая дельта возвращает профиль как есть — без ложного роста версии.

## Хранилище

Профиль хранится целиком по `user_id` (это не векторный поиск, а
key-value). Три варианта:

| Класс | Где | Когда |
|---|---|---|
| `InMemoryProfileStore` | В оперативке | Тесты, короткоживущие процессы |
| `SqliteProfileStore` | В файле SQLite | Прод без внешних сервисов |
| `AsyncInMemoryProfileStore` | В оперативке, `async` | Async-приложения |

`as_async_profile(store)` — поднимет любой синхронный стор на event loop.

### Scope и изоляция профиля

Для profile-памяти непустой `MemoryScope` поддерживают `InMemoryProfileStore`,
`SqliteProfileStore` и их async-варианты. Они хранят физический ключ отдельно,
но возвращают исходный логический `user_id`:

```python
from protoprompt import MemoryScope
from protoprompt.profile import ProfileManager, SqliteProfileStore

manager = ProfileManager(
    SqliteProfileStore("users.db"),
    scope=MemoryScope(tenant="acme", user="u1"),
)
```

`MemoryScope` создаёт только доверенный host-код. Нестандартный store без
`supports_profile_scopes=True` нельзя использовать с непустым scope:
`ProfileManager` завершится с `ValueError` до первого чтения. Это намеренно
безопасный отказ — не виртуализируйте scope конкатенацией ключей и не
подхватывайте старые unscoped-профили автоматически. При миграции копируйте в
новую область только явно разрешённые записи. При collision старого физического
ключа scoped read считает запись отсутствующей, а scoped write/reset/delete/CAS
выбрасывают `ValueError`, а не уничтожают эту запись.

## Менеджер: весь цикл в одном классе

`ProfileManager` — оркестратор: загрузить → извлечь → слить → сохранить.

```python
from protoprompt.profile import ProfileManager, SqliteProfileStore
from protoprompt.profile import Signal, LLMProfileSource

store = SqliteProfileStore("users.db")
manager = ProfileManager(store, LLMProfileSource(llm, language="ru"))

# На каждой встрече с пользователем:
signals = [
    Signal(user_id="u1", kind="message", role="user",
           text="Меня зовут Илья, я пишу на python"),
]
profile = await manager.update("u1", signals)
print(profile.version)   # 1

# В следующий раз профиль уже есть, update добавит новое:
signals = [Signal(user_id="u1", kind="message", role="user",
                  text="Теперь я тимлид")]
profile = await manager.update("u1", signals)
print(profile.facts)     # {"imya": "Илья", "rol": "тимлид"}
```

Методы менеджера:

| Метод | Что делает |
|---|---|
| `await update(user_id, signals)` | Слить сигналы в профиль и сохранить |
| `await get(user_id)` | Достать профиль (`None`, если его нет) |
| `await reset(user_id)` | Начать заново: свежий пустой профиль |
| `await delete(user_id)` | Удалить профиль |

## Как профиль попадает в промпт

Два пути:

1. **Рендер вручную** — `render(profile)` возвращает готовый текст секции
   (пустую строку, если показывать нечего):

```python
from protoprompt.profile import render

text = render(profile, language="ru")
# "Профиль пользователя:\n- imya: Илья\n..."
```

2. **Через `ContextBuilder`** — передайте профиль в `ContextInput.profile`
   и включите `include_profile=True`:

```python
from protoprompt import ContextBuilder, ContextInput

builder = ContextBuilder(vector_store, llm)
out = await builder.build(ContextInput(
    query="Как организовать код?",
    include_profile=True,
    profile=profile,
))
print(out.system_prompt)   # профиль уже внутри
```

Модель получает контекст о пользователе в каждой сессии — без повторных
вопросов «а как тебя зовут?».

## Шпаргалка

| Задача | Класс / функция |
|---|---|
| Оркестрация | `ProfileManager` |
| Извлечение из сообщений | `LLMProfileSource`, `RuleProfileSource`, `CompositeProfileSource` |
| Нормализация ответа модели | `codec` (`parse_profile_json`, `coerce_profile`, `slugify`) |
| Слияние | `merge` |
| Хранение | `InMemoryProfileStore`, `SqliteProfileStore` |
| Текст для промпта | `render` |

Подробный разбор с работающим примером — в [уроке 5](tutorials/05-user-profile.md).
