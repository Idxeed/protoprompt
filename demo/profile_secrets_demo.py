"""Вербозное демо профиля + секретов + сборщика на живой Ollama.

Всё происходящее видно в консоли: LLM-вызовы и сырой JSON модели,
дельта → merge → версионирование, события бюджета (хуки), шифрование
секретов и scope-изоляция. Плюс включены внутренние логи protoprompt.

    python demo/profile_secrets_demo.py

Требует запущенный Ollama с моделями llama3.1:8b и nomic-embed-text.

Маркеры:
  [llm]  вызов чат-модели (показывается сырой ответ)
  [emb]  эмбеддинг N текстов
  [+]    факт добавлен      [~] факт обновлён     [x] факт забыт
  [sec]  секция влезла в бюджет   [drop] блок сброшен
  [key]  ключ шифрования    [grant]/[deny] доступ к секрету
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

# — читаемый и цветной вывод в консоли Windows —
if os.name == "nt":
    import ctypes

    _k32 = ctypes.windll.kernel32
    _k32.SetConsoleOutputCP(65001)
    _h = _k32.GetStdHandle(-11)
    _mode = ctypes.c_uint32()
    if _k32.GetConsoleMode(_h, ctypes.byref(_mode)):
        _k32.SetConsoleMode(_h, _mode.value | 0x0004)  # ANSI/VT
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from protoprompt import (  # noqa: E402
    CachedLLMClient,
    ContextHooks,
    ContextInput,
    InMemoryEmbeddingCache,
    InMemStore,
    RegexTokenCounter,
    TokenBudgetedContextBuilder,
)
from protoprompt.integrations import OllamaClient  # noqa: E402
from protoprompt.profile import (  # noqa: E402
    LLMProfileSource,
    ProfileManager,
    Signal,
    SqliteProfileStore,
)
from protoprompt.secrets import (  # noqa: E402
    EncryptedSqliteSecretStore,
    FileKeyProvider,
    SecretAccess,
)

HOST = "http://localhost:11434"
CHAT = "llama3.1:8b"
EMBED = "nomic-embed-text"

# ── оформление ───────────────────────────────────────────────────

RST, BOLD, DIM = "\x1b[0m", "\x1b[1m", "\x1b[2m"
CYAN, GREEN, YELLOW, RED, BLUE, MAGENTA, GRAY = (
    "\x1b[96m", "\x1b[92m", "\x1b[93m", "\x1b[91m",
    "\x1b[94m", "\x1b[95m", "\x1b[90m",
)
W = 100


def c(t: object, *s: str) -> str:
    return "".join(s) + str(t) + RST


def rule(ch: str = "─") -> str:
    return GRAY + ch * W + RST


def header(num: str, title: str) -> None:
    pad = "═" * max(4, W - len(title) - 12)
    print(f"\n{c(f'== {num} > {title} ', BOLD, CYAN)}{GRAY}{pad}{RST}")


def info(msg: str) -> None:
    print(f"  {c('[i]', GRAY)} {msg}")


def ok(msg: str) -> None:
    print(f"  {c('[✓]', GREEN, BOLD)} {msg}")


def box_top(width: int = W - 2) -> str:
    return GRAY + "┌" + "─" * (width - 1) + RST


def box_bottom(width: int = W - 2) -> str:
    return GRAY + "└" + "─" * (width - 1) + RST


def box_line(text: str, width: int = W - 2) -> str:
    return f"  {GRAY}│{RST} {text}"


# ── логирование protoprompt ──────────────────────────────────────


class _ColorLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        name = record.name.split(".")[-1]
        color = {"guard": MAGENTA, "source": CYAN, "store": GRAY}.get(
            name, GRAY
        )
        return f"{DIM}[log:{name}]{RST} {color}{msg}{RST}"


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_ColorLogFormatter("%(levelname)s %(message)s"))
    root = logging.getLogger("protoprompt")
    root.setLevel(logging.DEBUG)
    root.handlers = [handler]
    root.propagate = False


class DeltaRecorder:
    """Запоминает последнюю дельту, чтобы показать её после update."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self.last = None

    async def extract(self, user_id, signals):
        self.last = await self._inner.extract(user_id, signals)
        return self.last


# ── трейсинг LLM-вызовов ─────────────────────────────────────────


class TracingLLM:
    """Прокидывает вызовы в OllamaClient, показывая каждый шаг."""

    def __init__(self, inner, *, clip: int = 220) -> None:
        self._inner = inner
        self._clip = clip
        self.chat_calls = 0
        self.embed_calls = 0

    async def chat(self, messages, model="", **options):
        self.chat_calls += 1
        n = sum(len(str(m.get("content", ""))) for m in messages)
        print(f"  {c('[llm]', CYAN, BOLD)} chat → {model or CHAT} "
              f"{GRAY}({n} символов, {', '.join(f'{k}={v}' for k, v in options.items())}){RST}")
        resp = await self._inner.chat(messages, model=model, **options)
        clipped = resp if len(resp) <= self._clip else resp[: self._clip] + "…"
        print(f"       {DIM}← {clipped!r}{RST}")
        return resp

    async def embed(self, texts, model=""):
        self.embed_calls += 1
        print(f"  {c('[emb]', GREEN, BOLD)} embed {len(texts)} текст(ов) "
              f"→ {model or EMBED}")
        return await self._inner.embed(texts, model=model)

    async def aclose(self) -> None:
        await self._inner.aclose()


# ── панели ───────────────────────────────────────────────────────


def delta_panel(delta, title: str) -> None:
    print(f"  {box_top()}")
    print(box_line(c(f"дельта {title}", BOLD, MAGENTA)))
    if not delta.fact_ops and not delta.traits and not delta.preferences \
            and delta.topics is None and not delta.summary:
        print(box_line(f"  {c('(пустая дельта — merge ничего не меняет)', DIM)}"))
    for op in delta.fact_ops:
        mark = {"add": (c("[+]", GREEN, BOLD)), "update": (c("[~]", YELLOW, BOLD)),
                "forget": (c("[x]", RED, BOLD))}[op.op]
        print(box_line(f"  {mark} fact {op.op:<7} {c(op.key, BOLD)} = {op.value!r}"))
    for name, value in delta.traits.items():
        print(box_line(f"  {c('[~]', YELLOW, BOLD)} trait {c(name, BOLD)} = {value}"))
    for name, value in delta.preferences.items():
        print(box_line(f"  {c('[~]', YELLOW, BOLD)} pref  {c(name, BOLD)} = {value}"))
    if delta.topics is not None:
        print(box_line(f"  {c('[~]', YELLOW, BOLD)} topics = {delta.topics}"))
    if delta.summary:
        print(box_line(f"  {c('[~]', YELLOW, BOLD)} summary = {delta.summary!r}"))
    print(f"  {box_bottom()}")


def profile_panel(p) -> None:
    print(f"  {box_top()}")
    print(box_line(c("профиль после merge", BOLD, CYAN)))
    print(box_line(f"  {GRAY}version={RST}{c(p.version, BOLD)} "
                   f"{GRAY}source={RST}{p.source} "
                   f"{GRAY}updated_at={RST}{p.updated_at[:19]}"))
    if p.facts:
        print(box_line(f"  {c('facts:', MAGENTA, BOLD)}"))
        for k, v in p.facts.items():
            print(box_line(f"    {k} = {v}"))
    t = {k: v for k, v in vars(p.traits).items() if v}
    pr = {k: v for k, v in vars(p.preferences).items() if v}
    if t:
        print(box_line(f"  {c('traits:', CYAN, BOLD)} {t}"))
    if pr:
        print(box_line(f"  {c('preferences:', GREEN, BOLD)} {pr}"))
    if p.summary:
        print(box_line(f"  {c('summary:', YELLOW, BOLD)} {p.summary}"))
    print(f"  {box_bottom()}")


def budget_panel(report) -> None:
    print(f"  {box_top()}")
    print(box_line(c("📋 сборка контекста", BOLD)))
    for label, tokens in report.section_tokens.items():
        print(box_line(f"    {c('●', CYAN)} {c(f'{label:<14}', GRAY)} "
                       f"{c(f'{tokens:>5} tok', DIM)}"))
    pct = round(100 * report.used_tokens / max(report.budget, 1))
    ratio = min(1.0, report.used_tokens / max(report.budget, 1))
    fill = round(22 * ratio)
    col = GREEN if ratio < 0.7 else YELLOW if ratio < 0.9 else RED
    bar = col + "█" * fill + GRAY + "░" * (22 - fill) + RST
    print(box_line(f"    {bar} {c(f'{report.used_tokens}/{report.budget} tok', BOLD)} "
                   f"{c(f'{pct}%', DIM)}"))
    if report.dropped_blocks:
        print(box_line(f"    {c('⚠ сброшено: ' + ', '.join(report.dropped_blocks), RED, BOLD)}"))
    print(f"  {box_bottom()}")


# ── сценарий ─────────────────────────────────────────────────────


async def main() -> None:
    setup_logging()
    print()
    print(rule("═"))
    print(f"  {c('protoprompt', BOLD, CYAN)} "
          f"{c('— профиль + секреты + сборщик, вербозно', BOLD)}")
    print(rule("═"))

    raw = OllamaClient(host=HOST, chat_model=CHAT, embed_model=EMBED)
    llm = TracingLLM(CachedLLMClient(raw, InMemoryEmbeddingCache(capacity=256)))

    # ── 1. профиль ────────────────────────────────────────────────
    header("1", "Профиль: сигналы → LLM → дельта → merge → store")
    store = SqliteProfileStore(":memory:")
    source = DeltaRecorder(LLMProfileSource(llm, language="ru"))
    manager = ProfileManager(store, source)

    signals = [
        Signal("u1", "message", "Я бэкендер на Python и SQLite, настраиваю RAG.",
               role="user"),
        Signal("u1", "message", "Люблю короткие ответы списками, без воды.",
               role="user"),
    ]
    info("входящие сигналы:")
    for s in signals:
        print(f"    {c(f'[{s.kind}]', CYAN)} {c(f'[{s.role}]', GRAY)} {s.text}")

    p1 = await manager.update("u1", signals)
    delta_panel(source.last, "первый extract")
    profile_panel(p1)

    header("2", "Инкрементальный merge (второй update)")
    signals2 = [
        Signal("u1", "feedback", "Мне нравятся технические детали и примеры кода.",
               role="user"),
    ]
    info("новый сигнал:")
    print(f"    {c('[feedback]', CYAN)} {signals2[0].text}")
    p2 = await manager.update("u1", signals2)
    delta_panel(source.last, "второй extract")
    info(f"version {c(p1.version, BOLD)} → {c(p2.version, BOLD, GREEN)}")
    profile_panel(p2)

    # ── 3. сборщик с хуками ──────────────────────────────────────
    header("3", "Сборщик: RAG + профиль + токен-бюджет (хуки)")
    used: list[tuple[str, int]] = []
    dropped: list[tuple[str, str]] = []
    hooks = ContextHooks(
        on_section_used=lambda l, t: (used.append((l, t)),
                                      print(f"    {c('[sec]', GREEN)} влезло "
                                            f"{c(f'{l:<10}', BOLD)} {t} tok")),
        on_block_dropped=lambda l, r: (dropped.append((l, r)),
                                       print(f"    {c('[drop]', RED, BOLD)} сброшено "
                                             f"{c(l, BOLD)} ({r})")),
    )

    kb = InMemStore()
    docs = [
        "protoprompt собирает промпт из трёх слоёв: RAG, сжатая история, профиль.",
        "Токен-бюджет распределяется по приоритетам: system, session, profile, rag.",
    ]
    embs = await llm.embed(docs, model=EMBED)
    kb.add("handbook", docs, embs)
    ok("база знаний проиндексирована (2 чанка)")

    builder = TokenBudgetedContextBuilder(
        kb, llm, counter=RegexTokenCounter(), max_tokens=900, hooks=hooks,
    )
    inp = ContextInput(
        query="как protoprompt собирает контекст?",
        system_prompt="Ты ассистент по библиотеке protoprompt. Отвечай по-русски.",
        doc_ids=["handbook"],
        include_profile=True,
        profile=p2,
        language="ru",
    )
    messages = await builder.build_messages(inp, user_message=inp.query)
    budget_panel(builder.last_report)

    print(f"    {c('system_prompt →', DIM)}")
    for line in messages[0]["content"].splitlines()[:14]:
        print(f"      {GRAY}{line[:90]}{RST}")
    if len(messages[0]["content"].splitlines()) > 14:
        print(f"      {DIM}... (обрезано){RST}")

    answer = await llm.chat(messages, model=CHAT, temperature=0.3)
    print(f"\n    {c('ответ модели:', CYAN, BOLD)}\n    {answer.strip()}")

    # ── 4. секреты ───────────────────────────────────────────────
    header("4", "Секреты: ключ → шифрование → scope-изоляция → TTL")
    key_file = Path(tempfile.gettempdir()) / "protoprompt_demo_master.key"
    vault = EncryptedSqliteSecretStore(
        ":memory:", key_provider=FileKeyProvider(str(key_file))
    )
    print(f"  {c('[key]', YELLOW, BOLD)} мастер-ключ: "
          f"{GRAY}{key_file}{RST} ({key_file.stat().st_size} байт)")

    def authenticate(token: str) -> dict:
        return {"authenticated": token.startswith("ghp_")}

    operations = {"authenticate": authenticate}
    ilya = SecretAccess(vault, scope="ilya:myapp", operations=operations)
    mallory = SecretAccess(vault, scope="mallory:myapp", operations=operations)

    await ilya.store("github_token", "ghp_demo_secret", ttl=3600)
    print(f"  {c('[+sec]', GREEN, BOLD)} положен github_token "
          f"({GRAY}scope=ilya:myapp, ttl=3600{RST})")

    for name, access in (("Илья", ilya), ("Маллори", mallory)):
        result = await access.execute("github_token", "authenticate")
        mark = (c("[grant]", GREEN, BOLD) if result else c("[deny]", RED, BOLD))
        print(f"  {mark} {name} → authenticated = "
              f"{c(result if result else None, BOLD if result else DIM)}")

    await ilya.store("expired", "x", ttl=-1)
    print(f"  {c('[ttl]', YELLOW)} expired (ttl=-1) → "
          f"{c(await ilya.execute('expired', 'authenticate'), DIM)}")

    print(f"\n    {c('шифротекст в БД:', GRAY)}")
    row = vault._conn.execute(
        "SELECT token FROM secrets WHERE key='github_token'"
    ).fetchone()
    print(f"      {DIM}{row[0][:70]}…{RST}")

    print()
    print(rule("═"))
    print(f"  итого вызовов: {c(f'{llm.chat_calls} chat', CYAN)}, "
          f"{c(f'{llm.embed_calls} embed', GREEN)}")
    print(rule("═"))

    await raw.aclose()


if __name__ == "__main__":
    asyncio.run(main())
