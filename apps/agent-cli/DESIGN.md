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
        │ SqliteStore (холодная зона)  │  namespace = хэш(canonical root + FS identity)
        │ + sessions/*.json (hot+manifest) │ в user-owned state
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
3. **Переживание рестарта.** Холодная зона и снимки горячего набора живут в
   user-owned application-state, отдельном для canonical root и его filesystem
   identity. Репозиторий не является источником конфигурации или authority.

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
    final = [{"role": "user", "content": user_text}]
    await self._preflight_final_input(final)  # без RAG/session/memory side effects
    await self._establish_initial_goal(user_text)
    user_committed = False
    for _ in range(self.max_iterations):                     # защита от цикла
        ctx = await self.mem.assemble()
        system = f"{self.system_prompt}\n\n{ctx.render()}"   # рабочий контекст из памяти
        plan = await self.context_builder.plan_messages(
            ContextInput(query=user_text, system_prompt=system,
                         include_rag=False, include_session=False),
            history=self._history_before_final(final), final_messages=final,
        )
        if not user_committed:
            await self._record_user_turn(user_text, final[0])  # только после plan
            user_committed = True
        reply = await self.llm.chat(
            plan.render_messages(), max_tokens=plan.receipt.output_reserve_tokens,
        )
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
        final = [
            {"role": "assistant", "content": reply},
            {"role": "user", "content": tool_outputs},
        ]
        self._push_tail_group(final)  # сырой хвост, cap N, пара не дробится
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
    config.py               # trusted user-owned/explicit config.toml + env-переменные
    factory.py              # make_llm(backend, cfg) -> LLMClientProtocol
    repl.py                 # readline-цикл, диспетчер слэш-команд
    core.py                 # AgentCore: turn(), память, tail, лимит итераций
    tools.py                # ToolRunner: bash/read/write/glob/grep + permissions
    actions.py              # parse_actions(reply), Action dataclass
    render.py               # таблица памяти, цвета (паттерн из demo/coder_agent)
    persistence.py          # user-owned state, export/import, identity namespace
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
| `/allow <tool>` `/deny <tool>` | allow — только до конца сессии; deny — persistent user-owned policy |
| `/sessions`           | список сессий проекта                                             |
| `/resume <name>`      | переключиться на сессию                                           |
| `/new [name]`         | начать новую сессию                                               |
| `/git <args...>`      | прогон git через Bash permission/jail path                        |
| `/history [n]`        | последние введённые строки                                       |
| `/init`               | создать user-owned `config.toml`                                 |
| `!<command>`          | выполнить shell-команду через permission layer                   |
| `/save` `/resume-state` | сессия / legacy `state.json`                                   |
| `/clear`              | сбросить горячий набор (холод сохраняется, с подтверждением)      |
| `/exit` (Ctrl+D)      | выход с авто-сохранением                                          |

Стриминг: в REPL ответы идут токенами (когда бэкенд поддерживает
`chat_stream`); права `bash`/`write`/`edit` запрашиваются интерактивно.
Вариант `a` разрешает инструмент лишь до конца текущей сессии. Persistent
policy хранит только explicit `deny` в user-owned state.

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
`bash` и `write`/`edit`, `allow` для чтения. `allow` не восстанавливается
автоматически после рестарта; persistent `deny` хранится в user-owned
`perms.json`. `bash` запускается с working directory проекта, но не является
sandbox: после явного подтверждения команда обладает правами текущего
пользователя. До любого `y` или `a` REPL печатает полный terminal-safe escaped
payload: command для `bash`, path+content для `write` и path+old+new для
`edit`. Полный отрендеренный payload ограничен 4 096 UTF-8 bytes; если он не
помещается, запрос fail closed и не получает усечённого confirmation prompt.
Единая terminal-safe output boundary также применяется к stream/non-stream
ответам модели, результатам инструментов/файлов, trace, memory views, ошибкам
и launcher menu: C0/C1, DEL и Unicode format/bidi controls рендерятся
видимыми escape-последовательностями, а обычные newline сохраняются. Поэтому
данные не могут изменить терминал перед следующим confirmation prompt.
`/git` разбирается как argv и переходит в этот же `bash` path, поэтому не
открывает отдельный subprocess с path-based `cwd`. Jailed `write`/`edit` принимают только относительные пути и
используют handle-relative temporary-file replace без path-based fallback;
при недоступности native no-follow capability они отказывают. Секретов нет
(см. ROADMAP: SecretVault — отдельный трек, сюда не тянем).

На Linux jail использует `openat2(RESOLVE_NO_XDEV|RESOLVE_NO_SYMLINKS|...)`
и `renameat2`: tree inspection отвергает hard-link, link и mount crossing;
`edit` проверяет полный inode+content snapshot до и после atomic exchange и
откатывает доказуемо неуспешный commit. После commit сверяется staged inode;
при недоказуемом результате инструмент возвращает ошибку и сохраняет random
recovery entry, а не сообщает успех. На Windows конкретный `read` идёт только
по relative project path через native root-relative handle. Но до появления
native handle-relative directory
enumeration и identity-CAS rename jail намеренно разрешает там только чтение
конкретного файла и создание нового `write`-файла: `glob`/`grep`, `edit` и
перезапись существующего `write`-файла fail closed. Это ограничение снимается
только вместе с эквивалентной kernel-backed гарантией, не path-based fallback.

На Windows boundary начинается ещё до `ToolRunner`: выбранный project path,
его предки и `.git` marker обходятся native относительно drive handle с
`OBJ_DONT_REPARSE`. UNC и любой junction/symlink отклоняются до выбора root;
regular `.git` file Git worktree допустим. Drive-relative `C:project` не
принимается, чтобы namespace не зависел от process-local current directory.

Для POSIX `edit` точка линеаризации — успешный `RENAME_EXCHANGE`: сразу после
неё хешируется уже отвязанный old inode и при несовпадении выполняется
identity-checked rollback. Писатель, удерживающий дескриптор старого inode и
записывающий после этой точки, меняет уже отвязанный файл, а не опубликованный
результат; это обычная семантика атомарной replace, не восстановимое durable
`allow` или path race.

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
| `--request-max-tokens <n>` | жёсткий потолок полного provider-запроса            |
| `--output-reserve <n>`   | completion reserve и лимит ответа модели              |
| `--trace`                | трейс событий памяти                                  |

Сессии хранятся в user-owned
`<state>/<project-namespace>/sessions/<name>.json` (в дополнение к холодной
зоне в `agent.db`); `-p` и REPL автоматически продолжают сессию проекта.

## 5. Конфигурация и бэкенды

Конфиг: user-owned `config.toml` (tomllib, stdlib в py3.11+) либо явно
переданный доверенный файл через `--config`; repository-local config никогда
не выбирается автоматически. Секреты агент берёт только из env
(`PP_OPENAI_API_KEY` для OpenAI и `PP_HTTPX_API_KEY` для HTTPX), а поля
`api_key` и `api_key_env` из TOML не читает и не использует. Это не удаляет
plaintext из уже существующего TOML: владелец должен убрать такой ключ из файла,
Git и backup. Config не может выбрать произвольную переменную окружения.
Endpoint задаётся только явным user-owned/`--config` полем или документированной
`PP_*` override-переменной; он определяет получателя bearer token и должен быть
доверенным. Agent OpenAI backend использует прямой OpenAI-compatible HTTP
transport с явным endpoint и `trust_env=False`, а не SDK, который может считывать
дополнительные `OPENAI_*` переменные и custom headers. Поэтому
`PP_OPENAI_API_KEY` обязателен для OpenAI; неаутентифицированные локальные шлюзы
используют backend `httpx`.

```toml
[llm]
backend = "ollama"              # ollama | openai | httpx
chat_model = ""                 # "" = дефолт бэкенда
embed_model = ""                # "" = дефолт бэкенда; задаёт глобальный override

[llm.ollama]
host = "http://localhost:11434"

[llm.openai]
model = "gpt-4o-mini"
embed_model = "text-embedding-3-small"
# Optional override for a trusted OpenAI-compatible endpoint.  Otherwise the
# agent uses https://api.openai.com/v1; PP_OPENAI_BASE_URL overrides this value.
# This setting determines where PP_OPENAI_API_KEY is sent.
# base_url = "https://trusted-gateway.example/v1"

[llm.httpx]
base_url = "http://localhost:8000/v1"   # любой OpenAI-совместимый шлюз
# Credentials are never stored here.  Use PP_HTTPX_API_KEY (or the
# compatibility fallback PP_OPENAI_API_KEY) in the process environment.
# PP_HTTPX_BASE_URL overrides base_url.  Configure a gateway only if it is
# trusted with the selected bearer token; prefer PP_HTTPX_API_KEY over the
# OpenAI-key fallback for a generic gateway.

[memory]
max_tokens = 2048
recall_cooldown_steps = 10

[agent]
request_max_tokens = 8192
output_reserve_tokens = 1024
```

`memory.max_tokens` ограничивает горячую память и не является окном модели.
Перед каждым вызовом `AgentCore` передаёт rendered `WorkingMemory`, raw tail
и обязательный final input в `TokenBudgetedContextBuilder.plan_messages()` с
`include_rag=False` и `include_session=False`. Полученный immutable
`ContextPlan` владеет payload и receipt; именно его `receipt.input_tokens`
идёт в `/cost`, а `receipt.output_reserve_tokens` — в `max_tokens` клиента.
После text-action assistant action XML и synthetic user tool output становятся
одним multi-message `final_messages` continuation, поэтому trimming не может
оторвать результат инструмента от вызвавшего его действия.

`factory.make_llm(backend, cfg)` возвращает `LLMClientProtocol` (Ollama —
`OllamaClient`; OpenAI и generic gateway — изолированный
`HttpxLLMClient`), оборачивается в `CachedLLMClient` с `InMemoryEmbeddingCache` — эмбеддинги не бьют по API
повторно. **Все бэкенды равноправны** — protocol-agnostic tool-calling (D2)
делает их взаимозаменяемыми; единственная разница — строки настроек.

Дефолт: `ollama` (офлайн, обкатан в demo); при недоступности — явная ошибка с
подсказкой `--config` / `PP_LLM_BACKEND`.

## 6. Персистентность

Каталог состояния находится вне репозитория: `%LOCALAPPDATA%\\ProtoPrompt\\agent`
на Windows, `$XDG_STATE_HOME/protoprompt/agent` (или
`~/.local/state/protoprompt/agent`) на POSIX. `PROTOPROMPT_AGENT_STATE_DIR`
— явный process-level override для тестов, контейнеров и managed deployments.
Для каждого canonical root с filesystem device/inode и stable creation
generation создаётся отдельный namespace; при подмене каталога по тому же пути
состояние и policy не наследуются, включая случай переиспользованного inode.
На Linux требуется `statx` `BTIME`; без него запуск fail closed, поскольку
directory `ctime` меняется при обычной работе и не является generation.

| Файл             | Содержимое                                      | Обновление        |
|------------------|-------------------------------------------------|-------------------|
| `agent.db`       | `SqliteStore` — холодная зона (chunks+metadata) | на каждое выселение |
| `sessions/<name>.json` | `export_state()` активной сессии          | авто-сохранение раз в K ходов, на выходе |
| `state.json`     | legacy-снимок (`/resume-state`)                 | только вручную    |
| `perms.json`     | только persistent запреты инструментов (`deny`) | при `/deny`       |
| `config.toml`    | user-owned конфигурация                         | `/init`/вручную   |

- `namespace = sha256(canonical root + filesystem device/inode + stable
  creation generation)[:12]` — два проекта не смешивают холодильники в одной
  базе, а подмена каталога по тому же пути не получает предыдущую память или
  policy, даже при inode reuse. Это v3 namespace, поэтому state прежней схемы
  не мигрирует автоматически.
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
| **CLI-3** | персистентность: user-owned cold zone + `state.json`, авто-save/авто-resume, namespace по identity проекта | перезапуск в том же каталоге продолжает холодную память; два проекта и подменённый каталог изолированы |
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
| D5 | `namespace` = хэш canonical root + filesystem device/inode + stable creation generation; live identity удерживает root descriptor; два проекта и подменённый каталог изолированы | принято |
| D6 | Бэкенды ollama/openai/httpx равноправны через фабрику, дефолт ollama | принято |
| D7 | Только stdlib в REPL (argparse + readline + asyncio + tomllib); rich — опциональный extra, не в ядро | принято |
| D8 | Автоматический конфиг user-owned; repository-local конфиг — только явно доверенный `--config`; секреты только через env | принято |
| D9 | Диалог: system-промпт = `assemble().render()`, raw tail = optional history, а текущий user/tool continuation = mandatory final payload | принято |
| D10 | Права на инструменты `ask/allow/deny`, дефолт — `ask` для пишущих; только persistent `deny` в user-owned `perms.json`; jailed file-tools используют native handle-relative boundary, блокируют links/reparse/hard links и mount crossing, а при недоступности fail closed (Windows tree/overwrite — явно ограничены) | принято |
| D11 | Каждый provider request строится только через immutable `ContextPlan` с exact receipt и completion reserve | принято |

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
- [x] user-owned персистентность: холод + сессии + авто-resume по identity проекта; repository-local state/config не используются автоматически
- [x] три бэкенда равноправны через фабрику; `-p` print-режим, `--output-format json`
- [x] сессии `/sessions` `/resume` `/new`, `--session`, план-режим, `/compact`, `/cost`, стриминг
- [x] единый bounded final-request path для обычного хода, plan и compact;
  exact receipt, completion reserve и неразрывный action-result continuation
- [x] профиль НЕ используется (проверка: рендер секции профиля отключён)
- [x] тесты всех фаз на моках (паттерн `_mocks.MockLLM`), CI-job, docs RU/EN, CHANGELOG
