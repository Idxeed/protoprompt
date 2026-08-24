"""Эксперимент: рабочая память protoprompt.agent на реальном проекте.

ВЕРБОЗНЫЙ режим: каждое событие памяти печатается полностью —
добавления, ссылки, квитанции скоринга, выселения, recall.

    python demo/coder_agent.py

Маркеры:
  [+] добавлен элемент          [x] выселен в холодильник
  [>] ссылка на определение     [R] восстановлено из холода
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import urllib.request
import uuid
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
    InMemoryEmbeddingCache,
    RegexTokenCounter,
    SqliteStore,
)
from protoprompt.agent import ScorerWeights, WorkingMemory  # noqa: E402
from protoprompt.integrations import OllamaClient  # noqa: E402

HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
CHAT_MODEL = os.environ.get("OLLAMA_CHAT_MODEL", "")
EMBED_MODEL = "nomic-embed-text"
SRC = Path(__file__).resolve().parent / "tenacity_src" / "tenacity-main"
DB_PATH = Path(__file__).resolve().parent / "agent_memory.db"
BUDGET = 900
CLIP = 650
W = 108

# ── цвета и маркеры ──────────────────────────────────────────────

RST, BOLD, DIM = "\x1b[0m", "\x1b[1m", "\x1b[2m"
CYAN, GREEN, YELLOW, RED, BLUE, MAGENTA, GRAY = (
    "\x1b[96m", "\x1b[92m", "\x1b[93m", "\x1b[91m",
    "\x1b[94m", "\x1b[95m", "\x1b[90m",
)
KIND_COLOR = {
    "edit": GREEN, "note": MAGENTA, "recalled": CYAN, "file": BLUE,
    "test_result": YELLOW, "tool_output": YELLOW, "log": GRAY,
}


def c(t: object, *s: str) -> str:
    return "".join(s) + str(t) + RST


def rule(ch: str = "-") -> str:
    return GRAY + ch * W + RST


def clip(text: str, limit: int = CLIP) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...(обрезано, всего {len(text)} символов)"


def header(num: str, title: str) -> None:
    pad = "═" * max(4, W - len(title) - 12)
    print(f"\n{c(f'== {num} > {title} ', BOLD, CYAN)}{GRAY}{pad}{RST}")


def info(msg: str) -> None:
    print(f"  {GRAY}[i]{RST} {msg}")


def terms_line(terms: dict[str, float]) -> str:
    def f(v: float) -> str:
        s = f"{v:+.2f}"
        return c(s, GREEN if v >= 0 else RED)

    total = terms.get("total", 0.0)
    col = GREEN if total >= 2.0 else YELLOW if total >= 1.0 else RED
    return (f"kind {f(terms.get('kind', 0))} | "
            f"refs {f(terms.get('refs', 0))} | "
            f"sem {f(terms.get('semantic', 0))} | "
            f"rec {f(terms.get('recency', 0))} | "
            f"size {f(terms.get('size', 0))} "
            f"{GRAY}=>{RST} {col}{total:.2f}{RST}")


class Tracer:
    """Печатает каждое событие памяти."""

    def __init__(self, mem: WorkingMemory) -> None:
        self.mem = mem

    def __call__(self, event: str, d: dict) -> None:
        if event == "add":
            color = KIND_COLOR.get(d["kind"], GRAY)
            pin = c(" PINNED", MAGENTA, BOLD) if d["pinned"] else ""
            print(f"  {c('[+]', GREEN, BOLD)} {c(d['item_id'], BOLD)} "
                  f"{c(str(d['kind']), color, BOLD)} "
                  f"{d['tokens']:>4} tok{pin}")
            print(f"       summary : {d['summary'][:80]}")
            print(f"       defs    : {', '.join(d['defs'])[:70] or '-'}")
            print(f"       idents  : {d['n_refs']} шт")
            print(f"       score@birth: {terms_line(d['terms'])}")

        elif event == "reference":
            src, tgt = d["source_id"], d["target_id"]
            names = ", ".join(d["names"])[:60]
            print(f"  {c('[>]', CYAN, BOLD)} ссылка: {src} упоминает "
                  f"{c(names, YELLOW)} определённые в {tgt} "
                  f"{GRAY}(refs цели -> {d['target_refs']}, шаг {d['step']}){RST}")

        elif event == "evict":
            print(f"  {c('[x]', RED, BOLD)} ВЫСЕЛЕН {c(d['item_id'], BOLD)} "
                  f"[{d['kind']}] {d['summary'][:56]} "
                  f"{c(f'({d['tokens']} tok)', DIM)}")
            print(f"       причина : {c(d['reason'], RED)} "
                  f"@шаг {d['step']} -> холодный документ "
                  f"{GRAY}{d['cold_doc']}{RST}")
            if d["terms"]:
                print(f"       квитанция: {terms_line(d['terms'])}")

        elif event == "recall":
            sim = d.get("similarity")
            sim_s = f"{sim:.3f}" if isinstance(sim, (int, float)) else "?"
            print(f"  {c('[R]', MAGENTA, BOLD)} восстановлено "
                  f"{c(d['restored_id'], BOLD)} из холода "
                  f"{GRAY}(было {d.get('cold_orig_id')}, "
                  f"cos={sim_s}, возврат #{d.get('recall_count', '?')}){RST}")

        elif event == "dedup":
            print(f"  {c('[=]', YELLOW, BOLD)} дубликат заметки -> слит в "
                  f"{c(d['kept_id'], BOLD)} {GRAY}(бюджет не тронут){RST}")

        elif event == "dedup_replaced":
            delta = f"{d['tokens_before']}->{d['tokens_after']} tok"
            print(f"  {c('[=]', YELLOW, BOLD)} дубликат: текст длиннее — "
                  f"заменён в {c(d['kept_id'], BOLD)} "
                  f"{c(delta, DIM)}")

        elif event == "unpin_auto":
            print(f"  {c('[-]', RED, BOLD)} авто-снятие пина: {d['item_id']} "
                  f"{GRAY}(пины {d['pinned_tokens_before']} tok > "
                  f"лимит {d['cap']}){RST}")

        elif event == "recall_cooldown":
            age = self.mem.step - d["evicted_at"]
            print(f"  {c('[~]', CYAN)} карантин: {d['orig_id']} выгнан "
                  f"{age} шагов назад <{d['cooldown']} — recall отложен")

        elif event == "recall_skipped":
            print(f"  {c('[!]', RED)} recall скип: {d['orig_id']} "
                  f"{GRAY}({d['reason']}, нужен {d.get('would_cost')} tok){RST}")

        elif event == "recall_churned":
            print(f"  {c('[~]', YELLOW, BOLD)} возврат не прижился: "
                  f"{d['orig_id']} тут же вытеснен сильнейшими")


kind_key = "kind"  # noqa: E305  (используется выше только как заглушка)


def memory_table(mem: WorkingMemory) -> None:
    rows = []
    for item in mem.items.values():
        t = mem.scorer.explain(
            item, now=mem.step, goal_vector=mem.goal.vector)
        rows.append((t["total"], item, t))
    rows.sort(reverse=True, key=lambda r: r[0])

    hdr = (f"  {'id':<9}{'kind':<13}{'tok':>4} {'rc':>2} "
           f"{'kind':>6}{'refs':>6}{'sem':>6}{'rec':>6}{'size':>7}"
           f"{'ИТОГ':>8}  label")
    print(rule())
    print(c(hdr, BOLD))
    for total, item, t in rows:
        color = KIND_COLOR.get(item.kind, GRAY)
        pin = "*" if item.pinned else " "
        cells = (f"  {item.id:<9}{item.kind:<13}{item.tokens:>4} "
                 f"{pin:>2}{t['kind']:>6.2f}{t['refs']:>6.2f}"
                 f"{t['semantic']:>6.2f}{t['recency']:>6.2f}"
                 f"{t['size']:>7.2f}")
        col = GREEN if total >= 2 else YELLOW if total >= 1 else RED
        print(f"{cells}{c(f'{total:>8.2f}', col, BOLD)}  "
              f"{c(item.kind, color)} {item.label[:64]}")
    used = mem.used_tokens
    ratio = min(1.0, used / BUDGET)
    fill = round(20 * ratio)
    bcol = GREEN if ratio < 0.7 else YELLOW if ratio < 0.9 else RED
    bar = bcol + "#" * fill + GRAY + "." * (20 - fill) + RST
    print(f"  бюджет {bar} {c(f'{used}/{BUDGET}', BOLD)} tok · "
          f"{c(str(mem.evictions), BOLD)} выселений · "
          f"{c(str(len(mem.manifest.entries)), BOLD)} записей в холодильнике")
    print(rule())


# ── инфраструктура ───────────────────────────────────────────────


def check_ollama() -> None:
    try:
        with urllib.request.urlopen(f"{HOST}/api/version", timeout=3) as r:
            version = json.load(r).get("version", "?")
            print(f"  {c('[i]', GRAY)} Ollama {version} на {HOST}")
    except Exception as exc:
        print(c(f"Ollama недоступна ({exc})", RED, BOLD), file=sys.stderr)
        sys.exit(1)


def detect_chat_model() -> str:
    if CHAT_MODEL:
        return CHAT_MODEL
    with urllib.request.urlopen(f"{HOST}/api/tags", timeout=5) as r:
        names = [m["name"] for m in json.load(r).get("models", [])]
    for pref in ("llama3.1:8b", "llama3.1", "llama3.2:3b", "qwen2.5"):
        for n in names:
            if n.startswith(pref):
                return n
    return next((n for n in names if "embed" not in n), "")


class CountingOllama(OllamaClient):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.embed_calls = 0
        self.embedded_texts = 0

    async def embed(self, texts, model=""):
        self.embed_calls += 1
        self.embedded_texts += len(texts)
        return await super().embed(texts, model=model)


async def llm_lines(llm, model, prompt: str) -> list[str]:
    raw = await llm.chat([{"role": "user", "content": prompt}],
                         model=model, temperature=0.2)
    return [ln.strip("-• ").strip() for ln in raw.splitlines()
            if ln.strip() and not ln.strip().startswith("```")]


async def llm_code(llm, model, prompt: str) -> str:
    raw = await llm.chat([{"role": "user", "content": prompt}],
                         model=model, temperature=0.2)
    lines = [ln for ln in raw.splitlines() if not ln.strip().startswith("```")]
    return "\n".join(lines).strip()


def run_pytests() -> str:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_attempts.py", "-q",
         "--no-header"],
        cwd=str(SRC), env=env, capture_output=True, text=True, timeout=180,
        encoding="utf-8", errors="replace",
    )
    out = (proc.stdout + proc.stderr).strip()
    return out[-900:] if len(out) > 900 else out


# ── сценарий ─────────────────────────────────────────────────────


async def main() -> None:
    print()
    print(GRAY + "=" * W + RST)
    print(f"  {c('protoprompt.agent :: кодер-агент на tenacity', BOLD, CYAN)} "
          f"{c('(verbose memory trace)', DIM)}")
    print(GRAY + "=" * W + RST)
    check_ollama()
    model = detect_chat_model()
    print(f"  {c('[i]', GRAY)} модель: {c(model, BOLD, GREEN)} · "
          f"эмбеддинги: {EMBED_MODEL} · бюджет памяти: {BUDGET} tok")

    raw = CountingOllama(host=HOST)
    llm = CachedLLMClient(raw, InMemoryEmbeddingCache(capacity=256))
    store = SqliteStore(str(DB_PATH))
    tracer = Tracer(None)
    # уникальный неймспейс на прогон: база накапливает историю,
    # но прогоны не смешивают свои холодильники
    run_ns = f"coder-{uuid.uuid4().hex[:8]}"
    mem = WorkingMemory(store=store, llm=llm,
                        counter=RegexTokenCounter(),
                        max_tokens=BUDGET, namespace=run_ns,
                        trace=tracer,
                        max_pinned_tokens=BUDGET // 3,
                        recall_cooldown_steps=10,
                        weights=ScorerWeights(ref_half_life=20))
    tracer.mem = mem
    info(f"неймспейс прогона: {run_ns}")
    info(f"ручки долгой задачи: пины<={BUDGET // 3} tok · "
         f"карантин recall=10 шагов · полураспад ссылок=20 шагов · "
         f"дедуп заметок>=0.92 cos")

    await mem.set_goal(
        "изучить библиотеку tenacity и добавить хелпер подсчёта попыток"
    )
    info(f"цель установлена: '{mem.goal.text}' (вектор готов: "
         f"{mem.goal.ready})")

    # 1. разведка
    header("1", "Разведка: структура и ключевые файлы")
    tree = "\n".join(sorted(
        str(p.relative_to(SRC)) for p in SRC.rglob("*.py")
        if "test" not in p.name
    ))
    tid = await mem.add("tool_output", f"дерево проекта:\n{tree}",
                        summary="дерево py-файлов tenacity")
    for rel, summ in [
        ("tenacity/retry.py", "условия повтора retry_*"),
        ("tenacity/stop.py", "условия остановки stop_after_*"),
        ("tenacity/_utils.py", "внутренние утилиты"),
    ]:
        content = clip((SRC / rel).read_text(encoding="utf-8"))
        await mem.add("file", f"# {rel}\n{content}", summary=f"{rel}: {summ}")
    memory_table(mem)

    # 2. структуризация
    header("2", "Структуризация: агент пишет заметки")
    obs = await llm_lines(llm, model,
        "Ты изучил модули tenacity: retry.py (условия повтора), stop.py "
        "(когда остановиться), _utils.py (утилиты). Запиши 4 коротких "
        "факта об устройстве библиотеки, по одному в строке, без "
        "нумерации и вступлений."
    )
    for o in obs[:5]:
        await mem.note(o[:300])
    memory_table(mem)

    # 3. задача A
    header("3", "Задача A: хелпер count_attempts + тест")
    code_a = await llm_code(llm, model,
        "Напиши самостоятельную функцию count_attempts(retry_state) на "
        "python: возвращает атрибут attempt_number объекта retry_state, "
        "а если его нет — 0. Только код, без пояснений."
    )
    aid = await mem.add("edit", "# tenacity/attempts.py\n" + code_a,
                        summary="новый модуль attempts.py: count_attempts()")
    (SRC / "tenacity" / "attempts.py").write_text(code_a, encoding="utf-8")
    (SRC / "tests" / "test_attempts.py").write_text(
        "from tenacity.attempts import count_attempts\n\n"
        "class FakeState:\n"
        "    def __init__(self, attempt_number):\n"
        "        self.attempt_number = attempt_number\n\n"
        "def test_counts_attempt_number():\n"
        "    assert count_attempts(FakeState(7)) == 7\n\n"
        "def test_missing_attribute_gives_zero():\n"
        "    class Empty: pass\n"
        "    assert count_attempts(Empty()) == 0\n",
        encoding="utf-8",
    )
    info("файлы записаны, гоняю pytest…")
    log_a = run_pytests()
    await mem.add("test_result",
                  "$ pytest tests/test_attempts.py\n" + log_a,
                  summary="pytest: тесты attempts.py")
    passed = "passed" in log_a and "failed" not in log_a
    info(f"результат pytest: "
         f"{c('PASS', GREEN, BOLD) if passed else c('FAIL', RED, BOLD)}")
    memory_table(mem)

    # 4. шум
    header("4", "Шум: большие файлы давят на бюджет")
    for rel, summ in [
        ("tenacity/wait.py", "стратегии ожидания wait_*"),
        ("tenacity/__init__.py", "публичный API и Retrying"),
    ]:
        content = clip((SRC / rel).read_text(encoding="utf-8"), 800)
        await mem.add("file", f"# {rel}\n{content}", summary=f"{rel}: {summ}")
    memory_table(mem)

    # 5. задача B
    header("5", "Задача B: format_attempts поверх задачи A")
    code_b = await llm_code(llm, model,
        "В модуле tenacity.attempts есть функция count_attempts("
        "retry_state), возвращающая число попыток. Напиши рядом функцию "
        "format_attempts(retry_state) -> str: использует count_attempts "
        "и возвращает строку вида 'attempt N'. Только код."
    )
    bid = await mem.add("edit",
                        "# продолжение tenacity/attempts.py\n" + code_b,
                        summary="format_attempts() использует count_attempts()")
    memory_table(mem)

    # 6. финал
    header("6", "Финал: холодильник и контрольный recall")
    info(f"всего событий выселения: {c(str(mem.evictions), BOLD, YELLOW)}")
    for e in mem.manifest.entries:
        print(f"  {c('[cold]', CYAN)} {e.item_id} [{e.kind}] "
              f"{e.summary[:52]} ({e.tokens} tok, выгнан @шаг {e.evicted_at})")

    question = "что делает функция count_attempts из задачи A?"
    info(f"recall запрос: '{question}'")
    restored = await mem.recall(question)
    for rid in restored:
        it = mem.items.get(rid)
        if it:
            print(f"  {c('[R]', MAGENTA, BOLD)} {rid} [{it.kind}] "
                  f"{it.label[:64]}")

    print()
    print(rule("="))
    live_edits = sum(1 for i in mem.items.values() if i.kind == "edit")
    live_notes = sum(1 for i in mem.items.values() if i.kind == "note")
    raw_left = sum(1 for i in mem.items.values()
                   if i.kind in ("log", "test_result"))
    print(f"  ИТОГ: правок живо={live_edits} заметок={live_notes} "
          f"сырых логов={raw_left} | выселено={mem.evictions} "
          f"холодных={len(mem.manifest.entries)}")
    print(f"  эмбеддинги: {raw.embed_calls} вызовов API / "
          f"{raw.embedded_texts} текстов "
          f"(кэш отсеял повторы scoring-пересчётов)")
    print(rule("="))
    await raw.aclose()


if __name__ == "__main__":
    asyncio.run(main())
