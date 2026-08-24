"""Живое демо protoprompt на локальном Ollama.

    python demo/full_demo.py            # сценарий
    python demo/full_demo.py --chat     # интерактивный чат

Показывает весь стек v0.2.0: OllamaClient, SqliteStore (персистентный),
CachedLLMClient, TokenBudgetedContextBuilder + BudgetReport,
build_messages(), Pipeline со сжатием истории через LLM.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.request

# читаемый и цветной вывод в консоли Windows
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

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from protoprompt import (  # noqa: E402
    CachedLLMClient,
    ContextInput,
    InMemoryEmbeddingCache,
    Pipeline,
    PipelineHooks,
    RegexTokenCounter,
    Session,
    SqliteStore,
    TokenBudgetedContextBuilder,
)
from protoprompt.session.strategy import LLMSummaryStrategy  # noqa: E402
from protoprompt.integrations import OllamaClient  # noqa: E402

# ────────────────────────── оформление ──────────────────────────

RST = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
CYAN = "\x1b[96m"
GREEN = "\x1b[92m"
YELLOW = "\x1b[93m"
RED = "\x1b[91m"
BLUE = "\x1b[94m"
MAGENTA = "\x1b[95m"
GRAY = "\x1b[90m"

SECTION_COLORS = {
    "system": CYAN,
    "profile": MAGENTA,
    "session": YELLOW,
    "rag": GREEN,
    "history": BLUE,
    "summary": MAGENTA,
    "important": GREEN,
    "head": BLUE,
    "tail": YELLOW,
}


def c(text: object, *styles: str) -> str:
    return "".join(styles) + str(text) + RST


def rule(char: str = "─", width: int = 64) -> str:
    return GRAY + char * width + RST


def box_top(width: int = 62) -> str:
    return GRAY + "┌" + "─" * (width - 1) + RST


def box_bottom(width: int = 62) -> str:
    return GRAY + "└" + "─" * (width - 1) + RST


def header(num: str, title: str) -> None:
    print()
    print(c(f"━━ {num} ▸ {title} ", BOLD, CYAN) + GRAY + "━" * max(4, 58 - len(title)) + RST)


def ok(msg: str) -> None:
    print(f"  {c('✓', GREEN, BOLD)} {msg}")


def info(msg: str) -> None:
    print(f"  {c('·', GRAY)} {msg}")


def bar(used: int, total: int, width: int = 26) -> str:
    ratio = min(1.0, used / max(total, 1))
    filled = round(width * ratio)
    color = GREEN if ratio < 0.7 else YELLOW if ratio < 0.9 else RED
    return color + "█" * filled + GRAY + "░" * (width - filled) + RST


def seg_color(label: str) -> str:
    base = label.split("[")[0]
    return SECTION_COLORS.get(base, GRAY)


def budget_panel(report) -> None:
    """Красивая карточка сборки контекста из BudgetReport."""
    print(f"  {box_top()}")
    title = c("📋 сборка контекста", BOLD)
    print(f"  {GRAY}│{RST} {title}")
    for label, tokens in report.section_tokens.items():
        dot = c("●", seg_color(label))
        name = c(f"{label:<14}", seg_color(label))
        num = c(f"{tokens:>5} tok", DIM) if tokens else DIM + "  —   " + RST
        print(f"  {GRAY}│{RST}   {dot} {name} {num}")
    pct = round(100 * report.used_tokens / max(report.budget, 1))
    line = f"  {GRAY}│{RST}   {bar(report.used_tokens, report.budget)}"
    line += c(f" {report.used_tokens}/{report.budget} tok", BOLD)
    line += c(f" · {pct}%", DIM)
    print(line)
    if report.dropped_blocks:
        dropped = ", ".join(report.dropped_blocks)
        print(f"  {GRAY}│{RST}   {c(f'⚠ сброшено: {dropped}', RED, BOLD)}")
    elif report.history_kept:
        kept = c(f"✓ история: {report.history_kept} реплик в бюджете", GREEN)
        print(f"  {GRAY}│{RST}   {kept}")
    else:
        print(f"  {GRAY}│{RST}   {c('✓ все блоки поместились', GREEN)}")
    print(f"  {box_bottom()}")


def block_card(index: int, text: str, segment: str) -> None:
    tag = c(f"[{segment}]", seg_color(segment), BOLD)
    head = text.splitlines()[0]
    rest = text.count("\n")
    more = c(f"  (+{rest} строк)", DIM) if rest else ""
    print(f"  {c(f'#{index}', GRAY)} {tag} {head[:86]}{more}")


# ────────────────────────── конфиг ──────────────────────────

HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
CHAT_MODEL = os.environ.get("OLLAMA_CHAT_MODEL", "")
EMBED_MODEL = "nomic-embed-text"
DB_PATH = os.path.join(os.path.dirname(__file__), "demo_kb.db")
COMPRESS_EVERY_N = 8
SYSTEM_PROMPT = "Ты краткий ассистент по документации protoprompt."

KNOWLEDGE = {
    "handbook": [
        "Protoprompt собирает промпт из трёх слоёв: поиск по документам "
        "(RAG), сжатая история диалога и профиль пользователя.",
        "Токен-бюджет распределяется жадно по приоритетам: system, затем "
        "session, затем rag. Не влезло — блок обрезается по границе слова.",
        "SqliteStore хранит векторы прямо в файле базы SQLite, без "
        "отдельного сервиса. Повторное добавление документа заменяет его.",
        "Кэш эмбеддингов запоминает вектор каждого текста: повторный "
        "вопрос не тратит время модели.",
        "Сжатие сессии выполняет Pipeline: когда сообщений становится "
        "больше порога, история сворачивается в блоки и кладётся в стор.",
    ],
}

PREFERRED_MODELS = (
    "llama3.1:8b", "llama3.1", "llama3.2:3b", "llama3.2",
    "qwen2.5:7b", "qwen2.5:3b", "mistral", "gemma2",
)


# ────────────────────────── запуск ──────────────────────────

def check_ollama() -> None:
    try:
        with urllib.request.urlopen(f"{HOST}/api/version", timeout=3) as r:
            version = json.load(r).get("version", "?")
            ok(f"Ollama {version} на {HOST}")
    except Exception as exc:
        print(
            f"\n{c('✗ Ollama недоступна на ' + HOST, RED, BOLD)} ({exc})\n"
            f"{DIM}Установи: winget install Ollama.Ollama\n"
            f"Модели:   ollama pull {EMBED_MODEL} && ollama pull llama3.1:8b{RST}\n",
            file=sys.stderr,
        )
        sys.exit(1)


def detect_chat_model() -> str:
    if CHAT_MODEL:
        return CHAT_MODEL
    with urllib.request.urlopen(f"{HOST}/api/tags", timeout=5) as r:
        names = [m["name"] for m in json.load(r).get("models", [])]
    for preferred in PREFERRED_MODELS:
        for name in names:
            if name.startswith(preferred):
                return name
    chat_capable = [n for n in names if "embed" not in n and "bge" not in n]
    if not chat_capable:
        print(c("Не найдено чат-моделей. ollama pull llama3.1:8b", RED),
              file=sys.stderr)
        sys.exit(1)
    return chat_capable[0]


async def ask(builder, llm, model, history, question,
              show_panel: bool = True) -> str:
    inp = ContextInput(
        query=question, doc_ids=list(KNOWLEDGE),
        include_session=True, chat_id="demo",
        system_prompt=SYSTEM_PROMPT,
    )
    messages = await builder.build_messages(
        inp, history=history, user_message=question,
    )
    report = builder.last_report
    if show_panel and report is not None:
        budget_panel(report)
    answer = await llm.chat(messages, model=model, temperature=0.4)
    history.extend([
        {"role": "user", "content": question},
        {"role": "assistant", "content": answer},
    ])
    return answer


def make_builder(store, llm):
    drops: list[str] = []
    from protoprompt import ContextHooks

    hooks = ContextHooks(on_block_dropped=lambda l, r: drops.append(l))
    builder = TokenBudgetedContextBuilder(
        store, llm,
        counter=RegexTokenCounter(),
        max_tokens=2048,
        priorities=("system", "session", "profile", "rag"),
        hooks=hooks,
    )
    return builder


async def scenario(llm, raw, model, store, builder) -> None:
    header("1", "Сжатие длинной сессии реальной LLM")
    session = Session(chat_id="demo", messages=[
        {"role": "user", "content": "Меня зовут Илья, настраиваю протопромпт."},
        {"role": "assistant", "content": "Отлично! Какие слои нужны?"},
        {"role": "user", "content": "RAG и память сессии, бюджет 2048."},
        {"role": "assistant", "content": "Принято. Хранилище какое?"},
        {"role": "user", "content": "SQLite локально, важно без сервисов."},
        {"role": "assistant", "content": "Разумно для разработки."},
        {"role": "user", "content": "Хочу ещё профиль пользователя позже."},
        {"role": "assistant", "content": "ProfileBuilder уже есть в ядре."},
        {"role": "user", "content": "Итог: RAG + память, SQLite, бюджет 2048."},
        {"role": "assistant", "content": "Конфигурация ясна, удачи!"},
    ])
    strategy = LLMSummaryStrategy(model=model, window_size=6)
    saved: list[int] = []

    def on_after(_s, blocks):
        saved.append(len(blocks))
        for i, b in enumerate(blocks):
            block_card(i, b.text, b.metadata.get("segment", "?"))

    pipeline = Pipeline(store, llm, strategy=strategy,
                        compress_every_n=COMPRESS_EVERY_N,
                        embedding_model=EMBED_MODEL,
                        hooks=PipelineHooks(on_after_compress=on_after))
    blocks = await pipeline.compress_and_store(session)
    print(f"  {c(f'{len(session.messages)} реплик', BOLD)} "
          f"{c('→', GRAY)} "
          f"{c(f'{len(blocks)} саммари-блоков', GREEN, BOLD)} "
          f"{c('→ сохранено в стор 💾', DIM)}")

    header("2", "Вопрос с RAG + памятью сессии")
    history: list[dict] = []
    question = "Что я решил насчёт хранилища?"
    print(f"\n  {c('👤 ты', BLUE, BOLD)}  {question}\n")
    answer = await ask(builder, llm, model, history, question)
    print(f"\n  {c('🤖 бот', MAGENTA, BOLD)}  {answer.strip()}\n")

    header("3", "Тот же вопрос — эмбеддинг из кэша")
    before = len(getattr(llm.cache, "_data", {}))
    print(f"\n  {c('👤 ты', BLUE, BOLD)}  {question}\n")
    await ask(builder, llm, model, history, question, show_panel=False)
    after = len(getattr(llm.cache, "_data", {}))
    delta = after - before
    verdict = c("всё из кэша ⚡", GREEN, BOLD) if delta == 0 \
        else c(f"+{delta} новых записей", YELLOW)
    print(f"\n  {c('новых записей в кэше эмбеддингов:', DIM)} {verdict}")


async def chat_loop(llm, raw, model, builder) -> None:
    pipeline = Pipeline(builder._store, llm,
                        compress_every_n=COMPRESS_EVERY_N,
                        embedding_model=EMBED_MODEL)
    print(c("\nИнтерактивный чат", BOLD), c("· команды:", DIM),
          c("/stats", YELLOW), c("/compress", YELLOW), c("/exit", YELLOW))

    history: list[dict] = []
    while True:
        try:
            question = input(f"\n{c('👤 ты', BLUE, BOLD)}  ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not question:
            continue
        if question == "/exit":
            print(c("\nпока! 👋", DIM))
            break
        if question == "/stats":
            print(f"  {c('·', GRAY)} история: "
                  f"{c(str(len(history)), BOLD)} реплик | стор: "
                  f"{c(str(builder._store.count()), BOLD)} чанков")
            continue
        if question == "/compress":
            blocks = await pipeline.compress_and_store(Session(
                chat_id="demo", messages=history))
            if blocks:
                for i, b in enumerate(blocks):
                    block_card(i, b.text, b.metadata.get("segment", "?"))
                print(f"  {c('сжато →', DIM)} "
                      f"{c(f'{len(blocks)} блоков', GREEN, BOLD)} "
                      f"{c('— теперь придут как session[*]', DIM)}")
            else:
                print(f"  {c('мало реплик для сжатия', YELLOW)}")
            continue
        answer = await ask(builder, llm, model, history, question)
        print(f"\n{c('🤖 бот', MAGENTA, BOLD)}  {answer.strip()}")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--chat", action="store_true",
                        help="интерактивный режим")
    args = parser.parse_args()

    print()
    print(rule("═"))
    print(f"  {c('⚡ protoprompt', BOLD, CYAN)} "
          f"{c('v0.2.0', DIM)} {c('— живое демо', BOLD)}")
    print(rule("═"))

    check_ollama()
    model = detect_chat_model()
    ok(f"чат-модель: {c(model, BOLD, GREEN)} · "
       f"эмбеддинги: {c(EMBED_MODEL, BOLD, GREEN)}")

    raw = OllamaClient(host=HOST)
    llm = CachedLLMClient(raw, InMemoryEmbeddingCache(capacity=512))

    store = SqliteStore(DB_PATH)
    if store.count() == 0:
        chunks = KNOWLEDGE["handbook"]
        print(f"  {c('⏳ индексирую базу знаний', DIM)} "
              f"({len(chunks)} документов)…")
        store.add("handbook", chunks, await llm.embed(chunks, EMBED_MODEL))
        ok(f"проиндексировано → {DB_PATH}")
    else:
        ok(f"база знаний готова: {c(str(store.count()), BOLD)} чанков")

    builder = make_builder(store, llm)

    if args.chat:
        await chat_loop(llm, raw, model, builder)
    else:
        await scenario(llm, raw, model, store, builder)

    await raw.aclose()


if __name__ == "__main__":
    asyncio.run(main())
