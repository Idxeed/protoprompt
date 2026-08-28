"""Интеграционные тесты на реальной Ollama (marker: integration).

Пропускаются, если сервер недоступен. Покрывают реальные эмбеддинги,
чат и полный цикл агента с инструментами.
"""

from __future__ import annotations

import httpx
import pytest

from protoprompt import RegexTokenCounter, SqliteStore
from protoprompt.agent import WorkingMemory

from protoprompt_cli.core import AgentCore
from protoprompt_cli.factory import make_llm
from protoprompt_cli.tools import ToolRunner

HOST = "http://localhost:11434"
CHAT_MODEL = "llama3.1:8b"
EMBED_MODEL = "nomic-embed-text"


def _ollama_available() -> bool:
    try:
        return httpx.get(f"{HOST}/api/tags", timeout=5).status_code == 200
    except Exception:
        return False


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _ollama_available(), reason="Ollama не запущена"),
]


def _llm():
    return make_llm({"llm": {"backend": "ollama",
                             "chat_model": CHAT_MODEL,
                             "embed_model": EMBED_MODEL,
                             "ollama": {"host": HOST}}})


@pytest.fixture
def llm():
    return _llm()


async def test_chat_returns_nonempty_text(llm):
    reply = await llm.chat(
        [{"role": "user", "content": "Ответь одним словом: столица Франции?"}],
        model=CHAT_MODEL,
        max_tokens=20,
    )
    assert reply.strip(), "модель должна вернуть ответ"


async def test_embed_returns_real_vectors(llm):
    vectors = await llm.embed(["первый текст", "второй текст"], model=EMBED_MODEL)
    assert len(vectors) == 2
    assert len(vectors[0]) > 0
    assert all(isinstance(v, float) for v in vectors[0])


async def test_embed_dimension_matches_nomic(llm):
    (vector,) = await llm.embed(["x"], model=EMBED_MODEL)
    assert len(vector) == 768, "nomic-embed-text даёт 768-мерные векторы"


async def test_agent_creates_file_via_action(tmp_path):
    llm = _llm()
    store = SqliteStore()
    mem = WorkingMemory(store=store, llm=llm, counter=RegexTokenCounter(),
                        max_tokens=400, namespace="it-file")
    tools = ToolRunner(tmp_path, perms={"write": "allow"})
    core = AgentCore(mem, llm, tools, system_prompt=(
        "Ты — кодер-агент. Чтобы создать файл, верни тег "
        '<action name="write" path="f.txt">содержимое</action>. '
        "После создания ответь 'готово'."
    ))
    result = await core.turn(
        "Создай файл f.txt с содержимым ровно 'HELLO_AGENT'. Потом ответь 'готово'."
    )
    assert (tmp_path / "f.txt").exists(), "файл должен быть создан агентом"
    content = (tmp_path / "f.txt").read_text(encoding="utf-8", errors="replace")
    assert "HELLO_AGENT" in content
    assert result.actions_run >= 1
    assert result.reply, "должен быть финальный ответ"


async def test_memory_recall_with_real_embeddings(tmp_path):
    llm = _llm()
    store = SqliteStore()
    mem = WorkingMemory(store=store, llm=llm, counter=RegexTokenCounter(),
                        max_tokens=60, namespace="it-recall",
                        recall_cooldown_steps=0)
    anchor = await mem.add(
        "file", "def count_attempts(rs):\n    return rs.attempt_number\n",
        summary="count_attempts определение",
    )
    filler = await mem.add(
        "log", "count_attempts вызывается в обработчике повторов и бэкоффа, важно",
        summary="упоминание count_attempts",
    )
    await mem.forget(filler)
    assert anchor in mem.items
    restored = await mem.recall("count_attempts")
    assert restored, "recall должен вернуть холодный элемент по семантике"
    assert any("count_attempts" in mem.items[i].text for i in restored)


async def test_goal_anchors_semantic_scoring(llm):
    store = SqliteStore()
    mem = WorkingMemory(store=store, llm=llm, counter=RegexTokenCounter(),
                        max_tokens=200, namespace="it-goal")
    await mem.set_goal("реализовать функцию подсчёта попыток count_attempts")
    on_topic = await mem.add("file", "def count_attempts(rs): return rs.attempt_number")
    off_topic = await mem.add("file", "def render_table(rows): return rows[0]")
    terms_on = mem.scorer.explain(mem.items[on_topic], now=mem.step,
                                  goal_vector=mem.goal.vector)
    terms_off = mem.scorer.explain(mem.items[off_topic], now=mem.step,
                                   goal_vector=mem.goal.vector)
    assert terms_on["semantic"] > terms_off["semantic"], (
        "семантический терм должен различать тему и фон"
    )


async def test_cold_zone_persists_in_store(llm):
    store = SqliteStore()
    mem = WorkingMemory(store=store, llm=llm, counter=RegexTokenCounter(),
                        max_tokens=40, namespace="it-cold")
    item_id = await mem.add("log", "длинный шумный лог " * 40)
    assert item_id not in mem.items
    assert store.count() >= 1, "выселенные элементы живут в холодной зоне"
    assert mem.manifest.entries


async def test_chat_stream_emits_tokens(llm):
    chunks: list[str] = []
    reply = await llm.chat_stream(
        [{"role": "user", "content": "Перечисли числа от 1 до 3."}],
        model=CHAT_MODEL,
        on_token=chunks.append,
        max_tokens=30,
    )
    assert reply.strip(), "стрим должен вернуть полный ответ"
    assert "".join(chunks) == reply, "токены должны совпадать с ответом"
    assert len(chunks) >= 1


async def test_cached_client_proxies_chat_stream(llm):
    from protoprompt import CachedLLMClient

    cached = CachedLLMClient(llm.inner)
    chunks: list[str] = []
    reply = await cached.chat_stream(
        [{"role": "user", "content": "Скажи 'pong'."}],
        model=CHAT_MODEL,
        on_token=chunks.append,
        max_tokens=10,
    )
    assert reply.strip(), "стрим должен вернуть ответ"
    assert "".join(chunks) == reply, "прокси должен отдать токены без искажений"


async def test_agent_streams_final_reply(llm, tmp_path):
    from protoprompt_cli.core import AgentCore
    from protoprompt_cli.tools import ToolRunner

    store = SqliteStore()
    mem = WorkingMemory(store=store, llm=llm, counter=RegexTokenCounter(),
                        max_tokens=200, namespace="it-stream")
    tools = ToolRunner(tmp_path)
    core = AgentCore(mem, llm, tools, system_prompt=(
        "Ты — кодер-агент. Отвечай кратко, без action-тегов."
    ))
    chunks: list[str] = []
    result = await core.turn("Ответь одним словом: 7+1?", stream_cb=chunks.append)
    assert result.streamed is True
    assert result.reply, "должен быть ответ"
    assert "".join(chunks) == result.reply