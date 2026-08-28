"""Full-stack демо: RAG + профиль + секреты + сборщик на живой Ollama.

Все три слоя protoprompt в одном прогоне, вербозно:

    python demo/full_stack_demo.py

Требует запущенный Ollama с llama3.1:8b и nomic-embed-text.

Маркеры:
  [llm] чат-вызов (сырой ответ)   [emb] эмбеддинг N текстов
  [doc] индексация документа      [hit] найденный чанк (provenance)
  [+] fact add   [~] trait/pref   [sec] секция влезла в бюджет
  [key]/[grant]/[deny] — секреты
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import tempfile
from pathlib import Path

if os.name == "nt":
    import ctypes

    _k32 = ctypes.windll.kernel32
    _k32.SetConsoleOutputCP(65001)
    _h = _k32.GetStdHandle(-11)
    _mode = ctypes.c_uint32()
    if _k32.GetConsoleMode(_h, ctypes.byref(_mode)):
        _k32.SetConsoleMode(_h, _mode.value | 0x0004)
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
from protoprompt.rag import (  # noqa: E402
    DocumentIndexer,
    FixedSizeChunker,
    LLMReranker,
    Retriever,
)
from protoprompt.secrets import (  # noqa: E402
    EncryptedSqliteSecretStore,
    FileKeyProvider,
    SecretAccess,
)

HOST = "http://localhost:11434"
CHAT = "llama3.1:8b"
EMBED = "nomic-embed-text"

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


def ok(msg: str) -> None:
    print(f"  {c('[✓]', GREEN, BOLD)} {msg}")


def info(msg: str) -> None:
    print(f"  {c('[i]', GRAY)} {msg}")


def box_top() -> str:
    return GRAY + "┌" + "─" * (W - 3) + RST


def box_bottom() -> str:
    return GRAY + "└" + "─" * (W - 3) + RST


def box_line(text: str) -> str:
    return f"  {GRAY}│{RST} {text}"


class _ColorLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        msg = super().format(record)
        color = {"guard": MAGENTA, "source": CYAN, "store": GRAY}.get(
            record.name.split(".")[-1], GRAY
        )
        return f"{DIM}[log:{record.name.split('.')[-1]}]{RST} {color}{msg}{RST}"


def setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_ColorLogFormatter("%(levelname)s %(message)s"))
    root = logging.getLogger("protoprompt")
    root.setLevel(logging.DEBUG)
    root.handlers = [handler]
    root.propagate = False


class TracingLLM:
    def __init__(self, inner, *, clip: int = 200) -> None:
        self._inner = inner
        self._clip = clip
        self.chat_calls = 0
        self.embed_calls = 0

    async def chat(self, messages, model="", **options):
        self.chat_calls += 1
        n = sum(len(str(m.get("content", ""))) for m in messages)
        print(f"  {c('[llm]', CYAN, BOLD)} chat → {model or CHAT} "
              f"{GRAY}({n} символов){RST}")
        resp = await self._inner.chat(messages, model=model, **options)
        clipped = resp if len(resp) <= self._clip else resp[: self._clip] + "…"
        print(f"       {DIM}← {clipped!r}{RST}")
        return resp

    async def embed(self, texts, model=""):
        self.embed_calls += 1
        print(f"  {c('[emb]', GREEN, BOLD)} embed {len(texts)} текст(ов) → {model or EMBED}")
        return await self._inner.embed(texts, model=model)

    async def aclose(self) -> None:
        await self._inner.aclose()


KNOWLEDGE = [
    ("kb_layer", "protoprompt собирает промпт из трёх слоёв: RAG по документам, "
                 "сжатая история сессии и профиль пользователя."),
    ("kb_rag", "RAG ищет документы по смыслу через векторный стор: запрос эмбеддится, "
               "берутся ближайшие чанки по косинусной близости."),
    ("kb_budget", "Токен-бюджет распределяется по приоритетам: system, затем profile, "
                  "затем session, затем rag. Не влезло — блок обрезается."),
    ("kb_secrets", "Секреты хранятся отдельно от памяти: шифруются, имеют срок жизни "
                   "и никогда не попадают в промпт автоматически."),
]


def show_chunks(chunks, title: str) -> None:
    print(box_line(c(title, BOLD, MAGENTA)))
    for i, ch in enumerate(chunks):
        text = ch.text.replace("\n", " ")
        print(box_line(
            f"  {c(f'[{ch.doc_id}#{ch.index}]', CYAN, BOLD)} "
            f"{c(f'score={ch.score:.2f}', DIM)} {text[:72]}"
        ))


def budget_panel(report) -> None:
    print(f"  {box_top()}")
    print(box_line(c("📋 сборка контекста", BOLD)))
    for label, tokens in report.section_tokens.items():
        print(box_line(f"    {c('●', CYAN)} {c(f'{label:<12}', GRAY)} "
                       f"{c(f'{tokens:>4} tok', DIM)}"))
    pct = round(100 * report.used_tokens / max(report.budget, 1))
    ratio = min(1.0, report.used_tokens / max(report.budget, 1))
    fill = round(20 * ratio)
    col = GREEN if ratio < 0.7 else YELLOW if ratio < 0.9 else RED
    bar = col + "█" * fill + GRAY + "░" * (20 - fill) + RST
    print(box_line(f"    {bar} {c(f'{report.used_tokens}/{report.budget} tok', BOLD)} "
                   f"{c(f'{pct}%', DIM)}"))
    print(f"  {box_bottom()}")


async def main() -> None:
    setup_logging()
    print()
    print(rule("═"))
    print(f"  {c('protoprompt', BOLD, CYAN)} "
          f"{c('— full-stack: RAG + профиль + секреты', BOLD)}")
    print(rule("═"))

    raw = OllamaClient(host=HOST, chat_model=CHAT, embed_model=EMBED)
    llm = TracingLLM(CachedLLMClient(raw, InMemoryEmbeddingCache(capacity=256)))

    # ── 1. RAG: индексация + поиск ────────────────────────────────
    header("1", "RAG: чанкинг → индексация → поиск (provenance)")
    store = InMemStore()
    indexer = DocumentIndexer(store, llm, chunker=FixedSizeChunker(200, overlap=40))
    for doc_id, text in KNOWLEDGE:
        n = await indexer.index(doc_id, text)
        print(f"  {c('[doc]', YELLOW, BOLD)} {doc_id} → {n} чанк(ов)")

    retriever = Retriever(store, llm)
    q = "как protoprompt собирает контекст?"
    chunks = await retriever.retrieve(q, top_k=3, score_threshold=0.2)
    show_chunks(chunks, "retrieve(score_threshold=0.2):")

    # ── 2. RAG: rerank ────────────────────────────────────────────
    header("2", "RAG: rerank через LLMReranker")
    reranked = await Retriever(
        store, llm, reranker=LLMReranker(llm)
    ).retrieve(q, top_k=3)
    show_chunks(reranked, "после LLMReranker:")

    # ── 3. Профиль ────────────────────────────────────────────────
    header("3", "Профиль: LLMProfileSource → ProfileManager")
    manager = ProfileManager(
        SqliteProfileStore(":memory:"), LLMProfileSource(llm, language="ru")
    )
    profile = await manager.update("u1", [
        Signal("u1", "message", "Я бэкендер на Python и SQLite, настраиваю RAG.", role="user"),
        Signal("u1", "message", "Люблю короткие ответы списками.", role="user"),
    ])
    print(f"  {box_top()}")
    print(box_line(c(f"профиль v{profile.version} [{profile.source}]", BOLD, CYAN)))
    for k, v in profile.facts.items():
        print(box_line(f"  fact {c(k, BOLD)} = {v}"))
    t = {k: v for k, v in vars(profile.traits).items() if v}
    pr = {k: v for k, v in vars(profile.preferences).items() if v}
    print(box_line(f"  traits: {t}"))
    print(box_line(f"  preferences: {pr}"))
    print(f"  {box_bottom()}")

    # ── 4. Секреты ────────────────────────────────────────────────
    header("4", "Секреты: scope-изоляция + шифрование")
    key_file = Path(tempfile.gettempdir()) / "protoprompt_demo_master.key"
    vault = EncryptedSqliteSecretStore(
        ":memory:", key_provider=FileKeyProvider(str(key_file))
    )
    def authenticate(token: str) -> dict:
        return {"authenticated": token.startswith("ghp_")}

    operations = {"authenticate": authenticate}
    ilya = SecretAccess(vault, scope="ilya:myapp", operations=operations)
    mallory = SecretAccess(vault, scope="mallory:myapp", operations=operations)
    await ilya.store("github_token", "ghp_demo", ttl=3600)
    print(f"  {c('[grant]', GREEN, BOLD)} Илья    → "
          f"{await ilya.execute('github_token', 'authenticate')}")
    print(f"  {c('[deny]', RED, BOLD)} Маллори → "
          f"{await mallory.execute('github_token', 'authenticate')}")

    # ── 5. Контекст: всё вместе ───────────────────────────────────
    header("5", "Сборка контекста: RAG + профиль + токен-бюджет")
    used: list[str] = []
    hooks = ContextHooks(
        on_section_used=lambda l, t: used.append(l),
        on_block_dropped=lambda l, r: print(f"    {c('[drop]', RED, BOLD)} {l} ({r})"),
    )
    builder = TokenBudgetedContextBuilder(
        store, llm, counter=RegexTokenCounter(), max_tokens=1024,
        hooks=hooks, retriever=retriever,
    )
    inp = ContextInput(
        query=q,
        system_prompt="Ты ассистент по библиотеке protoprompt. Отвечай по-русски.",
        include_profile=True,
        profile=profile,
        score_threshold=0.2,
    )
    messages = await builder.build_messages(inp, user_message=q)
    for sec in used:
        print(f"    {c('[sec]', GREEN)} влезло {c(sec, BOLD)}")
    budget_panel(builder.last_report)

    print(f"    {c('system_prompt →', DIM)}")
    for line in messages[0]["content"].splitlines()[:16]:
        print(f"      {GRAY}{line[:92]}{RST}")

    answer = await llm.chat(messages, model=CHAT, temperature=0.3)
    print(f"\n    {c('ответ модели:', CYAN, BOLD)}\n    {answer.strip()}")

    print()
    print(rule("═"))
    print(f"  итого: {c(f'{llm.chat_calls} chat', CYAN)}, "
          f"{c(f'{llm.embed_calls} embed', GREEN)}")
    print(rule("═"))
    await raw.aclose()


if __name__ == "__main__":
    asyncio.run(main())
