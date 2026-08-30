"""REPL: интерактивный цикл и слэш-команды.

Команды живут в таблице ``COMMANDS``; реализация — методы ``Repl``.
Ввод/вывод инжектируются (``readline`` / ``write``), поэтому REPL
тестируется без терминала.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
from typing import Any, Callable

from protoprompt import TokenBudgetExceededError
from protoprompt.agent import WorkingMemory

from protoprompt_cli import persistence, render
from protoprompt_cli.actions import Action
from protoprompt_cli.config import project_config_path

PROMPT = "pp-agent> "

HELP_TEXT = """Слэш-команды:
  /help                     этот список
  /memory                   таблица горячего набора
  /context                  что реально войдёт в контекст
  /cold                     записи холодильника (Manifest)
  /recall <query>           вернуть холодные элементы по запросу
  /note <text>              пин-заметка в память
  /add <path...>            прочитать файлы в память (пин)
  /pin <id> /unpin <id>     управление пинами
  /forget <id>              ручное выселение в холод
  /goal [text]              показать или установить цель
  /budget <n>               сменить токен-бюджет памяти
  /compact                  сжать горячий набор в обзор
  /model <name>             сменить модель чата
  /plan [on|off]            режим планирования (без инструментов)
  /trace [on|off]           трейс событий памяти
  /status                   сессия, модель, цель, токены
  /cost                     учёт токенов за сессию
  /perms                    текущие права инструментов
  /allow <tool>             всегда разрешать инструмент (в perms.json)
  /deny <tool>              всегда запрещать инструмент
  /sessions                 список сессий проекта
  /resume <name>            переключиться на сессию
  /new [name]               начать новую сессию
  /save                     сохранить сессию
  /resume-state             восстановить последний state.json
  /git <args...>            прогон git в корне проекта
  /history [n]              последние введённые строки
  /init                     создать проектный config.toml
  !<command>                выполнить shell-команду с подтверждением
  /clear                    сбросить горячий набор (с подтверждением)
  /exit                     выход (Ctrl+D)"""


class Tracer:
    """Вербозный трейс событий памяти через ``write``."""

    def __init__(self, write: Callable[[str], None], mem: WorkingMemory) -> None:
        self.write = write
        self.mem = mem

    def __call__(self, event: str, data: dict) -> None:
        if event == "add":
            pin = " PINNED" if data.get("pinned") else ""
            self.write(
                f"[+] {data['item_id']} [{data['kind']}] "
                f"{data['tokens']} tok{pin} :: {data['summary'][:60]}"
            )
        elif event == "evict":
            self.write(
                f"[x] {data['item_id']} [{data['kind']}] "
                f"{data['reason']} :: {data['summary'][:60]}"
            )
        elif event == "recall":
            self.write(
                f"[R] {data['restored_id']} из холода "
                f"(было {data.get('cold_orig_id', '?')})"
            )
        elif event == "reference":
            self.write(
                f"[>] {data['source_id']} упоминает {data['target_id']} "
                f"({', '.join(sorted(data['names']))[:60]})"
            )
        elif event == "dedup":
            self.write(f"[=] дубликат заметки -> {data['kept_id']}")
        elif event == "dedup_replaced":
            self.write(
                f"[=] дубликат заменён текстом в {data['kept_id']} "
                f"({data['tokens_before']}->{data['tokens_after']} tok)"
            )
        elif event == "unpin_auto":
            self.write(f"[-] авто-снятие пина: {data['item_id']}")
        elif event == "recall_cooldown":
            self.write(f"[~] карантин: {data['orig_id']}")
        elif event == "recall_skipped":
            self.write(f"[!] recall скип: {data['orig_id']} ({data['reason']})")


class Repl:
    def __init__(
        self,
        core,
        mem: WorkingMemory,
        tools,
        *,
        root,
        cfg: dict | None = None,
        write: Callable[[str], None] | None = None,
        readline: Callable[[str], str] | None = None,
        save_every: int = 5,
        session: str = persistence.DEFAULT_SESSION,
        stream: bool = True,
    ) -> None:
        self.core = core
        self.mem = mem
        self.tools = tools
        self.root = root
        self.cfg = cfg or {}
        self.session = session
        self.stream = stream
        self.write = write or (lambda line: print(line))
        self._readline = readline or (lambda prompt: input(prompt))
        self.trace_enabled = False
        self.tracer = Tracer(self.write, mem)
        self.tools.ask_callback = self._ask_permission
        self._turns_since_save = 0
        self.save_every = max(1, save_every)
        self._history: list[str] = []
        self._history_index = 0

    # ── ввод/вывод ─────────────────────────────────────────────

    async def _read_line(self, prompt: str = PROMPT) -> str:
        return await asyncio.to_thread(self._readline, prompt)

    def _remember(self, line: str) -> None:
        if line and (not self._history or self._history[-1] != line):
            self._history.append(line)
        self._history_index = len(self._history)

    async def _read_multiline(self, first: str) -> str:
        lines = [first]
        while _unbalanced(lines):
            try:
                next_line = await self._read_line("...> ")
            except EOFError:
                break
            except KeyboardInterrupt:
                self.write("")
                return "\n".join(lines)
            lines.append(next_line)
        return "\n".join(lines)

    async def _ask_permission(self, action) -> bool:
        prompt = f"разрешить {action.name} ({action.summary(40)})? [y/N/a] "
        try:
            answer = await self._read_line(prompt)
        except EOFError:
            return False
        choice = answer.strip().lower()
        if choice in ("a", "always"):
            self.tools.perms[action.name] = "allow"
            self._persist_perms()
            self.write(f"инструмент {action.name} разрешён всегда")
            return True
        return choice in ("y", "yes", "д", "да")

    def _persist_perms(self) -> None:
        persistence.save_json(persistence.perms_json_path(self.root), self.tools.perms)

    def _set_trace(self, enabled: bool) -> None:
        self.trace_enabled = enabled
        self.mem._trace = self.tracer if enabled else None

    def _stream_token(self, token: str) -> None:
        self.write(token)

    def _write_budget_error(self, exc: TokenBudgetExceededError) -> None:
        """Render one actionable error for every bounded request path."""
        self.write(
            "контекст не помещается в лимит "
            f"({exc.section}: {exc.used}/{exc.budget} tok); "
            "увеличьте request_max_tokens или сократите ввод"
        )

    def _save(self) -> None:
        persistence.save_session(self.mem, self.root, self.session)

    # ── цикл ────────────────────────────────────────────────────

    async def run(self) -> None:
        self.write("pp-agent · protoprompt")
        self.write(f"project: {self.root} · session: {self.session}")
        self.write("/help for commands · Ctrl+D to exit")
        while True:
            try:
                line = await self._read_line()
            except EOFError:
                self.write("")
                break
            except KeyboardInterrupt:
                self.write("")
                continue
            text = line.strip()
            if not text:
                continue
            self._remember(line)
            if text.startswith("/"):
                if await self.dispatch(text):
                    break
            elif text.startswith("!"):
                action = Action(name="bash", body=text[1:].strip())
                result = await self.tools.run(action)
                output = result.output if result.ok else result.error
                self.write(output)
                await self.mem.add("tool_output", output,
                                   summary=f"shell: {action.body[:60]}")
            else:
                text = await self._read_multiline(line)
                try:
                    result = await self.core.turn(
                        text, stream_cb=self._stream_token if self.stream else None
                    )
                except TokenBudgetExceededError as exc:
                    self._write_budget_error(exc)
                    continue
                if result.streamed:
                    self.write("")
                elif result.reply:
                    self.write(result.reply)
                for restored_id in result.restored:
                    self.write(f"[R] восстановлено: {restored_id}")
                self._turns_since_save += 1
                if self._turns_since_save >= self.save_every:
                    self._save()
                    self._turns_since_save = 0

    async def dispatch(self, line: str) -> bool:
        import inspect

        parts = line.split(maxsplit=1)
        command = parts[0].lower()
        arg = parts[1].strip() if len(parts) > 1 else ""
        handler = COMMANDS.get(command)
        if handler is None:
            self.write(f"неизвестная команда: {command} (см. /help)")
            return False
        value = handler(self, arg)
        if inspect.isawaitable(value):
            value = await value
        return bool(value)

    # ── команды ─────────────────────────────────────────────────

    def _cmd_help(self, arg: str) -> bool:
        self.write(HELP_TEXT)
        return False

    def _cmd_memory(self, arg: str) -> bool:
        for line in render.memory_table(self.mem, color=True):
            self.write(line)
        return False

    async def _cmd_context(self, arg: str) -> bool:
        context = await self.mem.assemble()
        for line in render.context_lines(context):
            self.write(line)
        return False

    def _cmd_cold(self, arg: str) -> bool:
        for line in render.cold_table(self.mem):
            self.write(line)
        return False

    async def _cmd_recall(self, arg: str) -> bool:
        if not arg:
            self.write("использование: /recall <query>")
            return False
        restored = await self.mem.recall(arg)
        if restored:
            for item_id in restored:
                item = self.mem.items.get(item_id)
                label = render.format_item(item) if item else item_id
                self.write(f"[R] {label}")
        else:
            self.write("ничего не найдено в холодильнике")
        return False

    async def _cmd_note(self, arg: str) -> bool:
        if not arg:
            self.write("использование: /note <текст>")
            return False
        item_id = await self.mem.note(arg, pin=True)
        self.write(f"[+] заметка {item_id}")
        return False

    async def _cmd_add(self, arg: str) -> bool:
        if not arg:
            self.write("использование: /add <путь...>")
            return False
        for raw_path in arg.split():
            try:
                target = self.tools._resolve(raw_path)
            except Exception as exc:
                self.write(f"пропуск {raw_path}: {exc}")
                continue
            if not target.is_file():
                self.write(f"пропуск {raw_path}: не файл")
                continue
            content = target.read_text(encoding="utf-8", errors="replace")
            rel = str(target.relative_to(self.root))
            await self.mem.add(
                "file", f"# {rel}\n{content}",
                summary=f"{rel}: загружен пользователем", pin=True,
            )
            self.write(f"[+] {rel}")
        return False

    async def _cmd_pin(self, arg: str) -> bool:
        if not arg:
            self.write("использование: /pin <id>")
            return False
        self.write("ok" if self.mem.pin(arg) else f"нет такого элемента: {arg}")
        return False

    async def _cmd_unpin(self, arg: str) -> bool:
        if not arg:
            self.write("использование: /unpin <id>")
            return False
        self.write("ok" if self.mem.unpin(arg) else f"нет такого элемента: {arg}")
        return False

    async def _cmd_forget(self, arg: str) -> bool:
        if not arg:
            self.write("использование: /forget <id>")
            return False
        self.write("ok" if await self.mem.forget(arg) else f"нет такого элемента: {arg}")
        return False

    async def _cmd_goal(self, arg: str) -> bool:
        if arg:
            await self.mem.set_goal(arg)
            self.write(f"цель установлена: {arg}")
        else:
            self.write(self.mem.goal.text or "(цель не задана)")
        return False

    def _cmd_budget(self, arg: str) -> bool:
        if not arg or not arg.isdigit():
            self.write("использование: /budget <n>")
            return False
        self.mem._max_tokens = int(arg)
        self.write(f"бюджет памяти: {int(arg)} токенов")
        return False

    async def _cmd_compact(self, arg: str) -> bool:
        try:
            summary = await self.core.compact()
        except TokenBudgetExceededError as exc:
            self._write_budget_error(exc)
            return False
        if summary:
            self.write("[+] горячий набор сжат в обзор:")
            self.write(summary[:400])
        else:
            self.write("память пуста, сжимать нечего")
        return False

    def _cmd_model(self, arg: str) -> bool:
        if not arg:
            self.write(self.core.chat_model or "(модель бэкенда по умолчанию)")
            return False
        self.core.chat_model = arg
        self.write(f"модель чата: {arg}")
        return False

    def _cmd_plan(self, arg: str) -> bool:
        if arg == "on":
            self.core.plan_mode = True
            self.write("режим планирования: инструменты отключены")
        elif arg == "off":
            self.core.plan_mode = False
            self.write("режим планирования выключен")
        else:
            self.write(f"план-режим: {'включён' if self.core.plan_mode else 'выключен'}")
        return False

    def _cmd_trace(self, arg: str) -> bool:
        if arg == "on":
            self._set_trace(True)
            self.write("трейс включён")
        elif arg == "off":
            self._set_trace(False)
            self.write("трейс выключен")
        else:
            self.write(f"трейс: {'включён' if self.trace_enabled else 'выключен'}")
        return False

    def _cmd_status(self, arg: str) -> bool:
        backend = self.cfg.get("llm", {}).get("backend", "?")
        used = self.mem.used_tokens
        budget = getattr(self.mem, "_max_tokens", 0)
        usage = self.core.usage
        receipt = (
            self.core.last_context_plan.receipt
            if self.core.last_context_plan is not None
            else None
        )
        request_line = (
            f"запрос : {receipt.input_tokens}/{receipt.max_tokens} tok вход · "
            f"резерв {receipt.output_reserve_tokens} · "
            f"свободно {receipt.remaining_tokens}"
            if receipt is not None
            else f"запрос : ≤ {self.core.request_max_tokens} tok · "
            f"резерв ответа {self.core.output_reserve_tokens}"
        )
        lines = [
            f"сессия : {self.session}",
            f"корень : {self.root}",
            f"бэкенд : {backend} · модель: {self.core.chat_model or 'дефолт'}",
            f"цель   : {self.mem.goal.text[:60] or '-'}",
            f"память : {used}/{budget} tok · эвикций {self.mem.evictions} · "
            f"холод {len(self.mem.manifest.entries)}",
            request_line,
            f"вызовы : {usage['chat_calls']} · токенов входа {usage['input_tokens']} · "
            f"выхода {usage['output_tokens']}",
        ]
        for line in lines:
            self.write(line)
        return False

    def _cmd_cost(self, arg: str) -> bool:
        usage = self.core.usage
        self.write(f"вызовов LLM: {usage['chat_calls']}")
        self.write(f"токенов входа: {usage['input_tokens']}")
        self.write(f"токенов выхода: {usage['output_tokens']}")
        self.write(f"итого: {usage['input_tokens'] + usage['output_tokens']}")
        return False

    def _cmd_perms(self, arg: str) -> bool:
        for tool in sorted(self.tools.perms):
            self.write(f"  {tool:<12} {self.tools.perms[tool]}")
        return False

    def _cmd_allow(self, arg: str) -> bool:
        if not arg or arg not in self.tools.perms:
            self.write(f"использование: /allow <tool> (доступны: "
                       f"{', '.join(sorted(self.tools.perms))})")
            return False
        self.tools.perms[arg] = "allow"
        self._persist_perms()
        self.write(f"инструмент {arg} разрешён всегда")
        return False

    def _cmd_deny(self, arg: str) -> bool:
        if not arg or arg not in self.tools.perms:
            self.write(f"использование: /deny <tool> (доступны: "
                       f"{', '.join(sorted(self.tools.perms))})")
            return False
        self.tools.perms[arg] = "deny"
        self._persist_perms()
        self.write(f"инструмент {arg} запрещён")
        return False

    def _cmd_sessions(self, arg: str) -> bool:
        sessions = persistence.list_sessions(self.root)
        if not sessions:
            self.write("(сессий ещё нет)")
            return False
        for entry in sessions:
            marker = "*" if entry["name"] == self.session else " "
            goal = entry["goal"] or "-"
            self.write(f"  {marker} {entry['name']:<16} "
                       f"{entry['items']:>3} элементов · {goal}")
        return False

    async def _cmd_resume(self, arg: str) -> bool:
        if not arg:
            self.write("использование: /resume <имя>")
            return False
        if not persistence.session_exists(self.root, arg):
            self.write(f"нет сессии {arg!r} (см. /sessions)")
            return False
        target = persistence._sanitize_session(arg)
        self._save()
        if not persistence.load_session(self.mem, self.root, target):
            self.write(f"не удалось загрузить сессию {target!r}: файл повреждён")
            return False
        self.session = target
        self.core.reset_conversation()
        self.core.reset_usage()
        self.write(f"сессия: {self.session}")
        return False

    async def _cmd_new(self, arg: str) -> bool:
        self._save()
        # Sessions persist both the hot items and their cold-manifest view.
        # Reset via the public importer rather than clearing only a subset of
        # private fields, otherwise a newly created session can inherit stale
        # manifest lines from the preceding one.
        self.mem.import_state({})
        self.core.reset_conversation()
        self.core.reset_usage()
        self.session = persistence._sanitize_session(arg or "default")
        self.write(f"новая сессия: {self.session}")
        return False

    def _cmd_save(self, arg: str) -> bool:
        self._save()
        self.write(f"сессия {self.session!r} сохранена")
        return False

    def _cmd_resume_state(self, arg: str) -> bool:
        loaded = persistence.load_state(self.mem, self.root)
        self.write("состояние восстановлено" if loaded else "состояния нет")
        return False

    def _cmd_git(self, arg: str) -> bool:
        if not arg:
            self.write("использование: /git <status|diff|log|commit ...>")
            return False
        try:
            proc = subprocess.run(
                ["git"] + arg.split(),
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=60,
                errors="replace",
            )
        except FileNotFoundError:
            self.write("git недоступен")
            return False
        except subprocess.TimeoutExpired:
            self.write("git таймаут")
            return False
        output = (proc.stdout or "") + (proc.stderr or "")
        self.write(output.strip() or f"(пусто, exit={proc.returncode})")
        return False

    def _cmd_history(self, arg: str) -> bool:
        count = int(arg) if arg.isdigit() else 20
        for index, line in enumerate(self._history[-count:], 1):
            self.write(f"{index:>3}  {line}")
        return False

    def _cmd_init(self, arg: str) -> bool:
        target = project_config_path(self.root)
        if target.exists():
            self.write(f"config уже существует: {target}")
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            '[llm]\nbackend = "ollama"\nchat_model = ""\n'
            'embed_model = "nomic-embed-text"\n\n'
            '[llm.ollama]\nhost = "http://localhost:11434"\n\n'
            '[memory]\nmax_tokens = 2048\n\n'
            '[agent]\nrequest_max_tokens = 8192\n'
            'output_reserve_tokens = 1024\n',
            encoding="utf-8",
        )
        self.write(f"создан config: {target}")
        return False

    def _cmd_clear(self, arg: str) -> bool:
        if arg not in ("yes", "y"):
            self.write("подтвердите: /clear yes")
            return False
        self.mem._items.clear()
        self.mem._index._owners.clear()
        self.mem._step = 0
        self.write("горячий набор сброшен (холодная зона сохранена)")
        return False

    def _cmd_exit(self, arg: str) -> bool:
        self._save()
        self.write("bye")
        return True


def _unbalanced(lines: list[str]) -> bool:
    opens = sum(line.count("{") for line in lines)
    closes = sum(line.count("}") for line in lines)
    return opens > closes


COMMANDS: dict[str, Callable[[Repl, str], Any]] = {
    "/help": Repl._cmd_help,
    "/memory": Repl._cmd_memory,
    "/context": Repl._cmd_context,
    "/cold": Repl._cmd_cold,
    "/recall": Repl._cmd_recall,
    "/note": Repl._cmd_note,
    "/add": Repl._cmd_add,
    "/pin": Repl._cmd_pin,
    "/unpin": Repl._cmd_unpin,
    "/forget": Repl._cmd_forget,
    "/goal": Repl._cmd_goal,
    "/budget": Repl._cmd_budget,
    "/compact": Repl._cmd_compact,
    "/model": Repl._cmd_model,
    "/plan": Repl._cmd_plan,
    "/trace": Repl._cmd_trace,
    "/status": Repl._cmd_status,
    "/cost": Repl._cmd_cost,
    "/perms": Repl._cmd_perms,
    "/allow": Repl._cmd_allow,
    "/deny": Repl._cmd_deny,
    "/sessions": Repl._cmd_sessions,
    "/resume": Repl._cmd_resume,
    "/new": Repl._cmd_new,
    "/save": Repl._cmd_save,
    "/resume-state": Repl._cmd_resume_state,
    "/git": Repl._cmd_git,
    "/history": Repl._cmd_history,
    "/init": Repl._cmd_init,
    "/clear": Repl._cmd_clear,
    "/exit": Repl._cmd_exit,
}
