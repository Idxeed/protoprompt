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
python -m pip install -e "apps/agent-cli[ollama]"
pp-agent C:\path\to\project
```

Ollama должна иметь модели `llama3.1:8b` и `nomic-embed-text`.

## Режимы

```powershell
pp-agent C:\path\to\project
pp-agent -p "объясни структуру проекта" C:\path\to\project
pp-agent -p "составь план исправления" --plan --session fix C:\path\to\project
pp-agent -p "проверь тесты" --output-format json C:\path\to\project
pp-agent --resume fix C:\path\to\project
pp-agent --continue C:\path\to\project
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
