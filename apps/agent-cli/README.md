# pp-agent

Интерактивный coding-agent CLI поверх `protoprompt.agent.WorkingMemory`.
Профиль пользователя не подключается.

## Запуск из репозитория

```powershell
$env:PP_CHAT_MODEL = "llama3.1:8b"
.\.venv\Scripts\pp-agent.exe
.\.venv\Scripts\pp-agent.exe .
```

Без пути в обычном терминале появляется меню: открыть текущий каталог,
указать другой или выйти. Для скриптов и CI используется текущий каталог
без меню; его также можно отключить флагом `--no-menu`.

Для установки приложения:

```powershell
python -m pip install -e ".[ollama]"
python -m pip install -e "apps/agent-cli[ollama]"
pp-agent C:\path\to\project
```

При установке из checkout сначала ставьте корневой пакет: агент требует
ProtoPrompt `>=0.9.0,<1.0.0`, потому что использует `ContextPlan` и exact
request receipt.

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
`PP_OUTPUT_RESERVE_TOKENS` либо в `.protoprompt/config.toml`:

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

Инструменты записи и shell требуют подтверждения. `a` в запросе разрешения
сохраняет постоянное разрешение в `.protoprompt/perms.json`.

## Состояние

В корне проекта создаётся `.protoprompt/`:

- `agent.db` — холодная векторная зона памяти;
- `sessions/<name>.json` — горячая память и manifest сессии;
- `perms.json` — права инструментов;
- `config.toml` — настройки проекта.

`--plan` запрещает выполнение инструментов и сохраняет план в памяти.
`/compact` переводит старые hot-items в cold zone и оставляет сводку, поэтому
их можно вернуть через `/recall`.

`/status` показывает последний request receipt: входные токены, резерв ответа
и оставшийся запас; до первого вызова — настроенный потолок.
