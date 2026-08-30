# pp-agent

Интерактивный coding-agent CLI поверх `protoprompt.agent.WorkingMemory`.
Профиль пользователя не подключается.

## Установка и запуск

### Из checkout

```powershell
$env:PP_CHAT_MODEL = "llama3.1:8b"
.\.venv\Scripts\pp-agent.exe
.\.venv\Scripts\pp-agent.exe .
```

Без пути в обычном терминале появляется меню: открыть текущий каталог,
указать другой или выйти. Для скриптов и CI используется текущий каталог
без меню; его также можно отключить флагом `--no-menu`.

Для установки приложения из checkout:

```powershell
python -m pip install -e ".[ollama]"
python -m pip install -e "apps/agent-cli[ollama]"
pp-agent C:\path\to\project
```

При установке из checkout сначала ставьте корневой пакет: агент требует
ProtoPrompt `>=0.16.0,<0.17.0`, потому что использует `ContextPlan` и exact
request receipt.

### Из релиза 0.16

`protoprompt-cli` 0.16 распространяется как wheel и sdist в соответствующем
GitHub Release, а не как отдельный PyPI upload. Сначала установите версию
ядра с нужным backend, затем соответствующий wheel из GitHub Release:

```powershell
python -m pip install "protoprompt[ollama]==0.16.0"
python -m pip install "https://github.com/Idxeed/protoprompt/releases/download/v0.16.0/protoprompt_cli-0.16.0-py3-none-any.whl"
pp-agent --version
```

Как альтернатива, после установки соответствующей версии ядра можно поставить агент
прямо из соответствующего тега (например, чтобы проверить исходный код):

```powershell
python -m pip install "git+https://github.com/Idxeed/protoprompt.git@v0.16.0#subdirectory=apps/agent-cli"
```

Ollama должна иметь модели `llama3.1:8b` и `nomic-embed-text`.

## Режимы

```powershell
pp-agent C:\path\to\project
pp-agent -p "объясни структуру проекта" C:\path\to\project
pp-agent -p "составь план исправления" --plan --session fix C:\path\to\project
pp-agent -p "проверь тесты" --output-format json C:\path\to\project
pp-agent -p "проверь тесты" --request-max-tokens 8192 --output-reserve 1024 C:\path\to\project
pp-agent --resume fix C:\path\to\project
pp-agent --continue C:\path\to\project
```

## Контекстный лимит

У агента два независимых бюджета:

- `--budget` / `[memory].max_tokens` — размер долгоживущей рабочей памяти;
- `--request-max-tokens` / `[agent].request_max_tokens` — жёсткий потолок
  каждого полного запроса к модели; `--output-reserve` резервирует и передаёт
  модели лимит ответа.

Обычный ход, plan-режим и `/compact` проходят через один
`TokenBudgetedContextBuilder.plan_messages()` путь. Поэтому system context,
история, текущий user input, provider framing и запас под ответ учитываются
в одном receipt. После текстового action assistant-блок и его tool result
передаются как обязательная пара следующего запроса: лимит не может оставить
только половину продолжения.

Значения по умолчанию: `8192` токена на запрос и `1024` токена резерва.
Их можно задать через `PP_REQUEST_MAX_TOKENS` и
`PP_OUTPUT_RESERVE_TOKENS`, user-owned `config.toml`, либо в явно доверенном
файле через `--config /path/to/config.toml`:

```toml
[memory]
max_tokens = 2048

[agent]
request_max_tokens = 8192
output_reserve_tokens = 1024
```

## Основные команды REPL

`/help` · `/status` · `/context` · `/memory` · `/cold` · `/recall <query>`

`/note <text>` · `/add <path>` · `/compact` · `/plan on|off`

`/sessions` · `/resume <name>` · `/new <name>` · `/save`

`/perms` · `/allow <tool>` · `/deny <tool>` · `/git status` · `/cost`

`/history` · `/init` · `!<command>` · `/resume-state`

Инструменты записи и shell требуют подтверждения. `a` в запросе разрешения и
`/allow` действуют только до конца текущей сессии: постоянные `allow` не
восстанавливаются автоматически после перезапуска. `/deny` остаётся
персистентным ограничением в user-owned state. Shell запускается с working
directory проекта, но **не является sandbox**: разрешённая команда всё ещё
имеет права текущего пользователя.

Перед `y` или `a` CLI показывает полный payload именно запрошенного действия:
для `bash` — команду, для `write` — путь и содержимое, для `edit` — путь,
`old` и `new`. Значения выводятся в terminal-safe escaped-форме (control
characters не исполняются). Если полный вывод не помещается в жёсткий лимит
4 096 UTF-8 bytes, действие отклоняется до запроса ввода — усечённый хвост
никогда не подтверждается.

Та же terminal-safe граница применяется ко всему видимому тексту: стриму и
обычному ответу модели, выводу инструментов и файлов, trace, таблицам памяти,
ошибкам и стартовому меню. C0/C1, DEL и Unicode format/bidi controls
показываются как escape-последовательности (обычные переводы строк остаются
читаемыми), поэтому они не могут изменить экран перед следующим подтверждением.

`/git <args...>` разбирает Git-аргументы и запускает их через тот же
permission/jail путь `bash`; отдельного `subprocess` с path-based `cwd` у
команды нет.

`read`, `edit`, `glob`, `grep` и `/add` имеют отдельные лимиты на входные данные и
обход дерева. `grep` ищет буквальный текст, а не исполняет regex из ответа
модели.

При включённом jail `write` и `edit` принимают только относительные пути
проекта. Linux заменяет файл через привязанный к корню handle, временный файл
и `renameat2`; при замене сохраняются POSIX mode/ACL. `edit` привязан к полному
снимку inode+содержимого и отменяется, если файл изменился до commit. Все
jailed file-tools отсекают symlink/junction, hard link и mount/bind-mount
переходы: на Linux это `openat2` с kernel-enforced `RESOLVE_NO_XDEV`, а на
Windows чтение идёт через native root-relative handle без reparse traversal.
У POSIX-хоста без `openat2` нет path-based fallback: прямой доступ завершается
отказом, а небезопасные tree entries не обходятся.

После commit Linux дополнительно сверяет inode результата со staged inode. При
конкурентной подмене, которую нельзя доказуемо откатить, инструмент возвращает
ошибку и сохраняет случайный `.protoprompt-write-*.tmp` как recovery evidence;
он никогда не сообщает об успешной записи с непроверенным содержимым.

Windows jail намеренно уже: он безопасно читает конкретный файл только по
относительному project path и создаёт новый `write`-файл, но отказывает для
`write`/`edit` существующего файла и для
рекурсивных `glob`/`grep`. Для этих операций Windows пока не даёт нужный
handle-relative compare-and-swap либо обход дерева; использовать небезопасный
path-based fallback агент не будет. В не-jail режиме этой гарантии нет.

На Windows сам путь проекта и поиск ближайшего Git root также проходят
native component-by-component no-reparse проверку до создания namespace.
UNC, junction/symlink в пути проекта и небезопасный `.git` marker завершают
запуск отказом; обычный `.git`-файл Git worktree поддерживается. Не используйте
drive-relative форму `C:project` — указывайте `C:\project` или обычный
относительный путь.

Ключи API берутся только из окружения: `PP_OPENAI_API_KEY` для OpenAI и
`PP_HTTPX_API_KEY` для OpenAI-совместимого HTTPX gateway (`PP_OPENAI_API_KEY`
остаётся совместимым fallback). Агент не читает и не использует `api_key` или
`api_key_env` из `config.toml`, поэтому конфиг не может выбрать произвольную
переменную окружения. Однако plaintext-значение, уже записанное в TOML, физически
остаётся в файле и может попасть в Git или backup — его нужно удалить вручную.

Endpoint также задаётся только явно: `llm.openai.base_url` или
`PP_OPENAI_BASE_URL` для OpenAI, `llm.httpx.base_url` или
`PP_HTTPX_BASE_URL` для generic HTTPX gateway, `llm.ollama.host` или
`PP_OLLAMA_HOST` для Ollama. Endpoint определяет, куда будет отправлен bearer
token, поэтому выбирайте его только в user-owned конфиге или в явно доверенном
`--config`; для чужого gateway не используйте fallback `PP_OPENAI_API_KEY`.
OpenAI backend агента использует прямой совместимый REST-клиент с явным endpoint
и `trust_env=False`: переменные SDK `OPENAI_*`, proxy и CA-настройки процесса не
могут незаметно сменить ключ, заголовки или маршрут. `PP_OPENAI_API_KEY`
обязателен для этого backend; для локального OpenAI-совместимого сервера без
ключа используйте `httpx`.

## Состояние

Состояние создаётся вне репозитория: `%LOCALAPPDATA%\ProtoPrompt\agent` на
Windows, `$XDG_STATE_HOME/protoprompt/agent` (или
`~/.local/state/protoprompt/agent`) на POSIX. Для каждого канонического корня,
filesystem device/inode и stable creation generation используется отдельный
namespace: замена каталога проекта по тому же пути не наследует память или
разрешения, даже если файловая система переиспользовала inode.

Это namespace v3: прежние user-owned agent state/session каталоги намеренно
не импортируются автоматически. На Linux запуск также fail closed, если
filesystem не предоставляет `statx` `BTIME` для корня проекта; `ctime`
использовать нельзя, потому что обычная работа меняет его.

В namespace лежат:

- `agent.db` — холодная векторная зона памяти;
- `sessions/<name>.json` — горячая память и manifest сессии;
- `perms.json` — только постоянные запреты инструментов (`deny`);
- `config.toml` — user-owned настройки этого проекта.

Repository-local `.protoprompt/` больше не читается и не изменяется
автоматически. Если пользователь сознательно доверяет старому или общему
конфигурационному файлу, он передаёт его явно через `--config`; permissions из
репозитория никогда не становятся источником authority.

`--plan` запрещает выполнение инструментов и сохраняет план в памяти.
`/compact` переводит старые hot-items в cold zone и оставляет сводку, поэтому
их можно вернуть через `/recall`.

`/status` показывает последний request receipt: входные токены, резерв ответа
и оставшийся запас; до первого вызова — настроенный потолок.
