# Дизайн: `pp-agent` — CLI-агент как Claude Code на базе protoprompt

> Версия плана: v1. Статус решений — раздел 8. Профиль пользователя не используется
> (движок переделывается в соседнем треке) — работаем **только с памятью**.
> Дизайн оставляет шов под профиль: `render()` добавляет секцию без изменения
> остального пайплайна.

## 0. Принцип

Отдельное CLI-приложение в этом репозитории, которое **использует protoprompt как
библиотеку** (отдельный пакет, а не подпакет). Всё «умное» уже есть в
`protoprompt.agent`:

- значимость-скоринг (`MemoryScorer`) вместо recency-вытеснения;
- двухзонная память: горячий набор в RAM + холодная зона в `StoreProtocol`;
- `Manifest` — дешёвый индекс холодильника;
- `recall()` с двумя каналами (символы + семантика) и карантином;
- `export_state()`/`import_state()` — переживание рестарта;
- `trace`-колбэк — наблюдаемость для `/trace`.

CLI добавляет только **обвязку**: REPL, слэш-команды, инструменты
(bash/read/write/glob/grep), права на инструменты, конфиг, выбор LLM-бэкенда,
персистентность на диск. Мозг — библиотека.

## 1. Целевая архитектура

```
                    ┌─────────────────────────────┐
                    │   pp-agent (REPL)           │
                    │  argparse + readline + asyncio │
                    └──────────────┬──────────────┘
                                   │ turn()
                                   v
        ┌────────────────────────────────────────────┐
        │  AgentCore                                  │
        │  user → assemble() → context → LLM →        │
        │        actions? → tools → результат в память │
        └──────┬──────────────┬──────────────┬────────┘
               │              │              │
               v              v              v
        ┌─────────────┐ ┌─────────────┐ ┌──────────────────┐
        │ WorkingMemory│ │ ToolRunner  │ │  LLM-фабрика     │
        │ hot set +    │ │ bash/read/  │ │  ollama | openai │
        │ scorer +     │ │ write/glob/ │ │  | httpx (OpenAI-│
        │ eviction     │ │ grep        │ │  совместимый)    │
        └──────┬──────┘ └─────────────┘ └──────────────────┘
               │
               v
        ┌──────────────────────────────┐
        │ SqliteStore (холодная зона)  │  namespace = хэш(корень проекта)
        │ + sessions/*.json (hot+manifest) │ в .protoprompt/
        └──────────────────────────────┘
```

Ключевая связка «как Claude Code»:

1. **Долгий разговор не тащится целиком.** Каждый ход пишется в
   `WorkingMemory`; `assemble()` собирает из горячего набора самые значимые
   куски в системный промпт. Сырые последние N ходов (tail) идут как
   `messages` — мгновенная когезия, а длинный хвост умирает по значимости.
2. **Инструменты.** Модель возвращает действия как размеченные блоки в тексте
   (см. D2), `AgentCore` исполняет через `ToolRunner`, результат — новый
   `MemoryItem` нужного `kind`. Цикл идёт, пока есть действия (с лимитом
   итераций).
3. **Переживание рестарта.** Холодная зона живёт в `SqliteStore` в каталоге
   проекта, горячий набор — `export_state()/import_state()`.

## 2. Модель памяти (события → MemoryItem)

| Источник                          | `kind`          | pin | summary                        |
|-----------------------------------|-----------------|-----|--------------------------------|
| Ход пользователя                  | `tool_output`   | нет | `user: …` (первые 60 симв.)    |
| Ответ ассистента                  | `tool_output`   | нет | `assistant: …`                 |
| Чтение файла                      | `file`          | нет | `relpath: тезис`               |
| Правка / создание файла           | `edit`          | да  | `relpath: что сделано`         |
| Запуск команды / тестов           | `test_result`   | нет | `$ cmd` + исход                |
| Вывод инструмента (сырой)         | `tool_output`   | нет | короткий тезис                 |
| Само-заметка агента (дистилляция) | `note`          | да  | сам текст                      |
| Файл-дерево / обзор               | `log`           | нет | `дерево проекта: …`            |
| `recall` из холода                | `recalled`      | нет | `recall: <orig summary>`       |

Дополнительно:

- **Цель сессии** — `mem.set_goal(...)`: из первого хода пользователя (или
  `/goal <text>`). Якорь семантического слагаемого скоринга.
- **Дедуп заметок** (`note()`): агент не раздувает бюджет повторами.
- **Лимит пинов** — `max_pinned_tokens = budget // 3` (как в демо), чтобы
  заметки не съели весь контекст.
- **Права на бюджет**: `max_tokens` конфигурируемо, `/budget` меняет на лету.
- **Карантин recall** — `recall_cooldown_steps=10` против churn.

Псевдокод одного хода:

```python
async def turn(self, user_text: str) -> None:
    await self.mem.add("tool_output", user_text, summary=f"user: {user_text[:60]}")
    for _ in range(self.max_iterations):                     # защита от цикла
        ctx = await self.mem.assemble()
        system = f"{self.system_prompt}\n\n{ctx.render()}"   # рабочий контекст из памяти
        reply = await self.llm.chat(
            [{"role": "system", "content": system}, *self.tail])
        actions = parse_actions(reply)                       # <action …>…</action>
        if not actions:
            await self.mem.add("tool_output", reply, summary=f"assistant: {reply[:60]}")
            print(reply)
            return
        for action in actions:
            ok, out = await self.tools.run(action, perms=self.perm)
            kind = KIND_BY_TOOL[action.name]                 # edit/file/test_result/…
            await self.mem.add(kind, out, summary=action.summary(),
                               pin=action.name in ("write", "edit"))
        self.tail.append({"role": "assistant", "content": reply})  # сырой хвост, cap N
```

`tail` — скользящее окно последних N сырых ходов (дефолт 8): мгновенная
когезия диалога, а не память. Длинное прошлое — только через `WorkingMemory`.

## 3. Контракты (пакет `apps/agent-cli`)

```
apps/agent-cli/
  pyproject.toml            # deps: protoprompt; extras: ollama/openai/http
  src/protoprompt_cli/
    __init__.py
    __main__.py             # python -m protoprompt_cli
    config.py               # .protoprompt/config.toml (tomllib) + env-переменные
    factory.py              # make_llm(backend, cfg) -> LLMClientProtocol
    repl.py                 # readline-цикл, диспетчер слэш-команд
    core.py                 # AgentCore: turn(), память, tail, лимит итераций
    tools.py                # ToolRunner: bash/read/write/glob/grep + permissions
    actions.py              # parse_actions(reply), Action dataclass
    render.py               # таблица памяти, цвета (паттерн из demo/coder_agent)
    persistence.py          # пути .protoprompt/, export/import, namespace
  tests/
  DESIGN.md                 # этот документ
```

Консольный скрипт: `pp-agent = protoprompt_cli.__main__:main`.

Слишком крупные куски разбиваются на модули (например, `repl.py` может
разрастись → `commands/`), это не дизайн-решение, а техника.

## 4. UX CLI

### 4.1 Интерактивный режим

Запуск: `pp-agent [путь]` — путь по умолчанию = cwd, корень проекта
определяется как ближайший git-root (фолбэк — cwd).

Слэш-команды:

| Команда               | Что делает                                                        |
|-----------------------|-------------------------------------------------------------------|
| `/help`               | список команд                                                     |
| `/memory`             | таблица горячего набора: id, kind, pin, tok, термины скоринга     |
| `/context`            | что реально войдёт в контекст + бюджет-бар                       |
| `/cold`               | записи `Manifest` (холодильник)                                   |
| `/recall <query>`     | ручной recall: символы + семантика, вернуть в hot set             |
| `/note <text>`        | пин-заметка в память                                              |
| `/add <path...>`      | прочитать файлы в память как пин-элементы `file`                  |
| `/pin <id>` `/unpin <id>` | управление пинами                                             |
| `/forget <id>`        | ручное выселение в холод                                          |
| `/goal [text]`        | показать / установить цель                                        |
| `/budget <n>`         | сменить токен-бюджет памяти                                       |
| `/compact`            | сжать горячий набор в пин-обзор (холод цел)                       |
| `/model <name>`       | сменить модель (по бэкенду)                                       |
| `/plan [on|off]`      | режим планирования: инструменты отключены, ответ — план          |
| `/trace [on|off]`     | вербозный трейс событий памяти (add/ref/evict/recall)             |
| `/status`             | сессия, корень, бэкенд/модель, цель, память, токены               |
| `/cost`               | учёт вызовов LLM и токенов за сессию                              |
| `/perms`              | текущие права инструментов                                        |
| `/allow <tool>` `/deny <tool>` | персистентные права в `perms.json`                         |
| `/sessions`           | список сессий проекта                                             |
| `/resume <name>`      | переключиться на сессию                                           |
| `/new [name]`         | начать новую сессию                                               |
| `/git <args...>`      | прогон git в корне проекта                                        |
| `/history [n]`        | последние введённые строки                                       |
| `/init`               | создать проектный `config.toml`                                  |
| `!<command>`          | выполнить shell-команду через permission layer                   |
| `/save` `/resume-state` | сессия / legacy `state.json`                                   |
| `/clear`              | сбросить горячий набор (холод сохраняется, с подтверждением)      |
| `/exit` (Ctrl+D)      | выход с авто-сохранением                                          |

Стриминг: в REPL ответы идут токенами (когда бэкенд поддерживает
`chat_stream`); права `bash`/`write`/`edit` запрашиваются интерактивно
с вариантом `a` = «разрешать всегда» (пишется в `perms.json`).

### 4.2 Инструменты

`ToolRunner` — контракт с правами. В v1 четыре инструмента + grep/glob:

| Инструмент | Аргументы                          | `kind` в память |
|------------|------------------------------------|-----------------|
| `bash`     | `cmd`                              | `tool_output`/`test_result` |
| `read`     | `path`                             | `file`          |
| `write`    | `path`, `content`                  | `edit` (pin)    |
| `edit`     | `path`, `old`, `new` (точная замена)| `edit` (pin)    |
| `glob`     | `pattern`                          | `log`           |
| `grep`     | `pattern`, `path?`                 | `log`           |

Права на инструменты (по образцу Claude Code):
`ask` (спросить в REPL) / `allow` / `deny`, по умолчанию `ask` для
`bash` и `write`/`edit`, `allow` для чтения. Права персистятся в
`.protoprompt/perms.json`. Секретов нет (см. ROADMAP: SecretVault — отдельный
трек, сюда не тянем).

### 4.3 Не-интерактивный режим

`pp-agent -p "почини тест в retry.py"` — один прогон, вывод в stdout, exit
по завершении цикла. Тот же `AgentCore`, без REPL. Готово для CI и
пайплайнов.

Опции прогона:

| Флаг                     | Назначение                                            |
|--------------------------|-------------------------------------------------------|
| `-p/--print <text>`      | один промпт, вывод в stdout                           |
| `--output-format json`   | структурированный вывод: reply, plan, usage           |
| `--plan`                 | старт в план-режиме (инструменты отключены)           |
| `--session <name>`       | имя сессии (по умолчанию `default`)                   |
| `--stream`               | стримить токены ответа в stdout                       |
| `--backend <name>`       | ollama \| openai \| httpx                             |
| `--budget <n>`           | токен-бюджет памяти                                   |
| `--trace`                | трейс событий памяти                                  |

Сессии: состояние хранится в `.protoprompt/sessions/<name>.json`
(в дополнение к холодной зоне в `agent.db`); `-p` и REPL автоматически
продолжают сессию проекта.

## 5. Конфигурация и бэкенды

Конфиг: `.protoprompt/config.toml` (tomllib, stdlib в py3.11+), секреты —
только через env (`PP_OPENAI_API_KEY` и т.п.), никогда в файле.

```toml
[llm]
backend = "ollama"              # ollama | openai | httpx
chat_model = ""                 # "" = дефолт бэкенда
embed_model = "nomic-embed-text"

[llm.ollama]
host = "http://localhost:11434"

[llm.openai]
model = "gpt-4o-mini"
embed_model = "text-embedding-3-small"

[llm.httpx]
base_url = "http://localhost:8000/v1"   # любой OpenAI-совместимый шлюз
api_key_env = "PP_OPENAI_API_KEY"

[memory]
max_tokens = 2048
recall_cooldown_steps = 10
```

`factory.make_llm(backend, cfg)` возвращает `LLMClientProtocol` (все три уже в
библиотеке: `OllamaClient`, `OpenAIClient`, `HttpxLLMClient`), оборачивается в
`CachedLLMClient` с `InMemoryEmbeddingCache` — эмбеддинги не бьют по API
повторно. **Все бэкенды равноправны** — protocol-agnostic tool-calling (D2)
делает их взаимозаменяемыми; единственная разница — строки настроек.

Дефолт: `ollama` (офлайн, обкатан в demo); при недоступности — явная ошибка с
подсказкой `--config` / `PP_LLM_BACKEND`.

## 6. Персистентность

Каталог состояния проекта: `.protoprompt/` (в gitignore репозиториев).

| Файл             | Содержимое                                      | Обновление        |
|------------------|-------------------------------------------------|-------------------|
| `agent.db`       | `SqliteStore` — холодная зона (chunks+metadata) | на каждое выселение |
| `sessions/<name>.json` | `export_state()` активной сессии          | авто-сохранение раз в K ходов, на выходе |
| `state.json`     | legacy-снимок (`/resume-state`)                 | только вручную    |
| `perms.json`     | права на инструменты                            | при `/allow`/`/deny`/`a` |
| `config.toml`    | пользовательская конфигурация                   | вручную           |

- `namespace = sha256(корень проекта)[:8]` — два проекта не смешивают
  холодильники в одной базе.
- Авто-resume: при старте в том же корне hot set и manifest восстанавливаются
  из активной `sessions/<name>.json`, холодная зона — из `agent.db`.
- Старый `state.json` поддерживается только явной командой `/resume-state`.
- Отдельная база на сессию НЕ заводится (в отличие от демо) — CLI — это
  «память проекта», а не разового прогона.

## 7. Фазы работ

| Фаза | Что делаем | Приёмка |
|------|-----------|---------|
| **CLI-0** | каркас пакета: `pyproject.toml`, entry point, `config.py`, `factory.py`, `repl.py` с MockLLM | `pp-agent` стартует, `/help`, `/exit`, эхо-ответ без LLM; `python -m protoprompt_cli` работает |
| **CLI-1** | подключить `WorkingMemory`: события → память, `assemble()` → контекст; `/memory`, `/cold`, `/goal`, `/budget`, `/pin`, `/unpin`, `/forget`, `/recall`, `/trace` | таблица памяти, eviction под давлением, ручной recall, трейс add/evict/recall (тесты на моках как в `tests/test_agent_memory.py`) |
| **CLI-2** | `actions.py` + `ToolRunner` + permissions: bash/read/write/edit/glob/grep, цикл turn с лимитом итераций | агент правит файл и гоняет тест без хардкода сценария; `deny` инструмента не исполняется |
| **CLI-3** | персистентность: `.protoprompt/`, cold zone + `state.json`, авто-save/авто-resume, namespace по проекту | перезапуск в том же каталоге продолжает холодную память; два проекта изолированы |
| **CLI-4** | бэкенды: ollama/openai/httpx в фабрике, `/model`, `-p` (print-режим), тесты с моками для трёх бэкендов | прогон на трёх бэкендах; `-p` возвращает код 0/1 по успеху |
| **CLI-5** | docs RU/EN, CHANGELOG, README, интеграция в CI (см. ниже), бенчмарк памяти | pytest зелёный, coverage ≥ текущего уровня |

CI: отдельный job в `.github/workflows/ci.yml` — `pip install -e apps/agent-cli`,
`pytest apps/agent-cli/tests`. Интеграционные тесты — под маркером `integration`
(не в основном прогоне).

## 8. Решения

| #  | Решение | Статус |
|----|---------|--------|
| D1 | Отдельный пакет `apps/agent-cli`, консольный скрипт `pp-agent`; protoprompt — зависимость | принято |
| D2 | Tool-calling через action-блоки в тексте (`<action name="bash">…</action>`), парсинг в `actions.py` — совместимо с любым `LLMClientProtocol`; нативный function-calling — будущая опция | принято |
| D3 | Работаем только с `WorkingMemory`; профиль подключается позже как доп. секция `render()` | принято |
| D4 | Холодная зона — `SqliteStore`, горячий набор — `export/import_state` (JSON) | принято |
| D5 | `namespace` = хэш корня git-проекта; два проекта изолированы | принято |
| D6 | Бэкенды ollama/openai/httpx равноправны через фабрику, дефолт ollama | принято |
| D7 | Только stdlib в REPL (argparse + readline + asyncio + tomllib); rich — опциональный extra, не в ядро | принято |
| D8 | Конфиг `.protoprompt/config.toml`; секреты только через env | принято |
| D9 | Диалог: system-промпт = `assemble().render()`, messages = скользящий tail (N=8) сырых ходов | принято |
| D10 | Права на инструменты `ask/allow/deny`, дефолт — `ask` для пишущих; `perms.json` | принято |

## 9. Риски

- **Action-блоки в тексте хрупки** (модель может не закрыть тег). Гасим:
  парсер толерантен к обрывкам, при нуле действий текст идёт как ответ.
- **Холодная зона растёт** — eviction пишет, recall читает, но старые lineage
  никто не чистит. v1 ок (SQLite), GC холодной зоны — фоллоу-ап.
- **`write`/`edit` поверх реальных файлов** — гасится permission-моделью D10 и
  подтверждением; бэкапы-в-память (как Claude Code) — фоллоу-ап.
- **Разные эмбеддеры у бэкендов** — векторные пространства несогласованы.
  Признаём: память привязана к одному бэкенду; миграция векторов — не v1.
- **Слияние с SecretVault из ROADMAP** — не делаем в этом треке, шов —
  `ToolRunner.permissions` позже можно связать с guarded-tool.

## 10. Definition of done

- [x] `pp-agent` REPL с `/memory`, `/context`, `/cold`, `/recall`, `/pin|unpin|forget`, `/goal`, `/budget`, `/trace`
- [x] события всех видов памяти пишутся в `WorkingMemory`, `assemble()` кормит system-промпт
- [x] инструменты bash/read/write/edit/glob/grep с permission-моделью и лимитом итераций
- [x] персистентность `.protoprompt/`: холод + сессии + авто-resume по проекту
- [x] три бэкенда равноправны через фабрику; `-p` print-режим, `--output-format json`
- [x] сессии `/sessions` `/resume` `/new`, `--session`, план-режим, `/compact`, `/cost`, стриминг
- [x] профиль НЕ используется (проверка: рендер секции профиля отключён)
- [x] тесты всех фаз на моках (паттерн `_mocks.MockLLM`), CI-job, docs RU/EN, CHANGELOG
