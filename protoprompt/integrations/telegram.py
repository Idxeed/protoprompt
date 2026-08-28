"""Aiogram 3 bindings for a persistent, scope-safe memory bot."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
import sqlite3
import threading
from typing import Any
import uuid

from protoprompt.connectivity import MemoryService
from protoprompt.context import ContextInput
from protoprompt.injector_budgeted import TokenBudgetedContextBuilder
from protoprompt.llm import LLMClientProtocol
from protoprompt.rag.retriever import Retriever
from protoprompt.scope import MemoryScope
from protoprompt.store.protocol import StoreProtocol


@dataclass(frozen=True, slots=True)
class TelegramMemoryStatus:
    """Content-free memory counters safe to show in chat."""

    current_thread: int
    all_threads: int
    hot_messages: int


class TelegramMemoryRegistry:
    """Persistent index used for explainable bulk deletion.

    Vector stores intentionally expose no global enumeration primitive. This
    small registry records only opaque ids and scopes, never conversation text.
    It can safely share a SQLite file with :class:`SqliteStore`.
    """

    def __init__(self, path: str = ":memory:") -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS telegram_memory_registry ("
                "tenant TEXT NOT NULL, user_id TEXT NOT NULL, "
                "thread_id TEXT NOT NULL, memory_id TEXT NOT NULL, "
                "PRIMARY KEY (tenant, user_id, thread_id, memory_id))"
            )
            self._conn.commit()

    def add(self, scope: MemoryScope, memory_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO telegram_memory_registry "
                "(tenant, user_id, thread_id, memory_id) VALUES (?, ?, ?, ?)",
                (scope.tenant, scope.user, scope.thread, memory_id),
            )
            self._conn.commit()

    def list_user(self, tenant: str, user_id: str) -> list[tuple[str, str]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT thread_id, memory_id FROM telegram_memory_registry "
                "WHERE tenant = ? AND user_id = ? ORDER BY thread_id, memory_id",
                (tenant, user_id),
            ).fetchall()
        return [(str(thread), str(memory_id)) for thread, memory_id in rows]

    def count(self, scope: MemoryScope) -> TelegramMemoryStatus:
        with self._lock:
            current = self._conn.execute(
                "SELECT COUNT(*) FROM telegram_memory_registry "
                "WHERE tenant = ? AND user_id = ? AND thread_id = ?",
                (scope.tenant, scope.user, scope.thread),
            ).fetchone()[0]
            total = self._conn.execute(
                "SELECT COUNT(*) FROM telegram_memory_registry "
                "WHERE tenant = ? AND user_id = ?",
                (scope.tenant, scope.user),
            ).fetchone()[0]
        return TelegramMemoryStatus(
            current_thread=int(current),
            all_threads=int(total),
            hot_messages=0,
        )

    def remove_user(self, tenant: str, user_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM telegram_memory_registry "
                "WHERE tenant = ? AND user_id = ?",
                (tenant, user_id),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class TelegramMemoryBot:
    """Framework-independent application layer used by the aiogram router."""

    def __init__(
        self,
        store: StoreProtocol,
        llm: LLMClientProtocol,
        registry: TelegramMemoryRegistry,
        *,
        tenant: str = "telegram",
        system_prompt: str = (
            "You are a concise assistant. Use recalled memories only when "
            "they are relevant and do not claim that a memory exists otherwise."
        ),
        embedding_model: str = "",
        chat_model: str = "",
        max_tokens: int = 1800,
        max_hot_messages: int = 8,
        top_k: int = 5,
    ) -> None:
        self._store = store
        self._llm = llm
        self._registry = registry
        self._tenant = tenant
        self._system_prompt = system_prompt
        self._embedding_model = embedding_model
        self._chat_model = chat_model
        self._max_tokens = max_tokens
        self._max_hot_messages = max_hot_messages
        self._top_k = top_k
        self._history: dict[tuple[str, str], deque[dict[str, str]]] = defaultdict(
            lambda: deque(maxlen=max_hot_messages)
        )
        self._last_recall: dict[tuple[str, str], dict[str, Any]] = {}

    def scope_for(self, user_id: str | int, chat_id: str | int) -> MemoryScope:
        return MemoryScope(
            tenant=self._tenant,
            user=str(user_id),
            thread=str(chat_id),
            kind="telegram",
        )

    async def reply(
        self,
        user_id: str | int,
        chat_id: str | int,
        text: str,
    ) -> str:
        normalized = text.strip()
        if not normalized:
            raise ValueError("message text must not be empty")
        scope = self.scope_for(user_id, chat_id)
        key = (scope.user, scope.thread)
        retriever = Retriever(
            self._store,
            self._llm,
            document_kind="memory",
            scope=scope,
        )
        builder = TokenBudgetedContextBuilder(
            self._store,
            self._llm,
            max_tokens=self._max_tokens,
            scope=scope,
            retriever=retriever,
        )
        output = await builder.build(ContextInput(
            query=normalized,
            system_prompt=self._system_prompt,
            include_session=False,
            top_k_rag=self._top_k,
            embedding_model=self._embedding_model,
            language="en",
        ))
        messages: list[dict[str, str]] = []
        if output.system_prompt:
            messages.append({"role": "system", "content": output.system_prompt})
        messages.extend(self._history[key])
        messages.append({"role": "user", "content": normalized})
        answer = await self._llm.chat(messages, model=self._chat_model)

        memory_id = uuid.uuid4().hex
        service = self._service(scope, builder=builder)
        await service.remember(
            f"User: {normalized}\nAssistant: {answer}",
            memory_id=memory_id,
            metadata={"channel": "telegram"},
        )
        self._registry.add(scope, memory_id)
        self._history[key].append({"role": "user", "content": normalized})
        self._history[key].append({"role": "assistant", "content": answer})
        self._last_recall[key] = {
            "result_count": len(output.rag_chunks),
            "results": [
                {
                    "memory_id": str(chunk.metadata.get("memory_id", "")),
                    "score": chunk.score,
                    "chunk_index": chunk.index,
                }
                for chunk in output.rag_chunks
            ],
            "budget": service.budget_report(),
        }
        return answer

    def memory_status(
        self,
        user_id: str | int,
        chat_id: str | int,
    ) -> TelegramMemoryStatus:
        scope = self.scope_for(user_id, chat_id)
        status = self._registry.count(scope)
        return TelegramMemoryStatus(
            current_thread=status.current_thread,
            all_threads=status.all_threads,
            hot_messages=len(self._history[(scope.user, scope.thread)]),
        )

    def why(self, user_id: str | int, chat_id: str | int) -> dict[str, Any]:
        scope = self.scope_for(user_id, chat_id)
        return dict(self._last_recall.get(
            (scope.user, scope.thread),
            {"result_count": 0, "results": [], "budget": None},
        ))

    async def forget_user(self, user_id: str | int) -> int:
        identity = str(user_id)
        rows = self._registry.list_user(self._tenant, identity)
        for thread_id, memory_id in rows:
            scope = self.scope_for(identity, thread_id)
            await self._service(scope).forget(memory_id)
            self._history.pop((identity, thread_id), None)
            self._last_recall.pop((identity, thread_id), None)
        self._registry.remove_user(self._tenant, identity)
        return len(rows)

    def _service(
        self,
        scope: MemoryScope,
        *,
        builder: TokenBudgetedContextBuilder | None = None,
    ) -> MemoryService:
        return MemoryService(
            self._store,
            self._llm,
            scope,
            context_builder=builder,
            embedding_model=self._embedding_model,
        )


def create_telegram_router(app: TelegramMemoryBot):
    """Create an aiogram 3 Router with memory and privacy commands."""
    try:
        from aiogram import F, Router
        from aiogram.filters import Command, CommandStart
        from aiogram.types import Message
    except ImportError as exc:
        raise ImportError(
            "The Telegram bot adapter requires 'aiogram'. "
            "Install with: pip install 'protoprompt[telegram]'"
        ) from exc

    router = Router(name="protoprompt-memory")

    @router.message(CommandStart())
    async def start(message: Message) -> None:
        await message.answer(
            "Memory bot is ready. Send a message or use /memory, /why, "
            "/forget. Deletion requires /forget confirm."
        )

    @router.message(Command("memory"))
    async def memory(message: Message) -> None:
        if message.from_user is None:
            return
        status = app.memory_status(message.from_user.id, message.chat.id)
        await message.answer(
            "Memory status\n"
            f"current thread: {status.current_thread}\n"
            f"all threads: {status.all_threads}\n"
            f"hot messages: {status.hot_messages}"
        )

    @router.message(Command("why"))
    async def why(message: Message) -> None:
        if message.from_user is None:
            return
        report = app.why(message.from_user.id, message.chat.id)
        lines = [f"Last recall: {report['result_count']} result(s)"]
        for result in report["results"]:
            lines.append(
                f"• {result['memory_id'] or 'memory'}: "
                f"score={result['score']:.3f}"
            )
        await message.answer("\n".join(lines))

    @router.message(Command("forget"))
    async def forget(message: Message) -> None:
        if message.from_user is None:
            return
        parts = (message.text or "").split(maxsplit=1)
        if len(parts) != 2 or parts[1].strip().lower() != "confirm":
            await message.answer(
                "This deletes your long-term memory in every chat. "
                "Send /forget confirm to continue."
            )
            return
        deleted = await app.forget_user(message.from_user.id)
        await message.answer(f"Deleted {deleted} long-term memory item(s).")

    @router.message(F.text)
    async def answer(message: Message) -> None:
        if message.from_user is None or message.text is None:
            return
        response = await app.reply(
            message.from_user.id,
            message.chat.id,
            message.text,
        )
        await message.answer(response)

    return router
