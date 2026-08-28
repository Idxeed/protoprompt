"""AgentCore: цикл хода поверх WorkingMemory, LLM и ToolRunner.

Один ход: записать сообщение пользователя в память, собрать контекст,
спросить LLM, исполнить action-блоки через инструменты, результаты
вернуть в память. Повторять, пока есть действия (с лимитом итераций).
Сырой диалог — скользящий ``tail``, длинное прошлое — только из памяти.

Дополнительно: учёт токенов (``/cost``), plan-режим (без инструментов),
сжатие памяти (``/compact``) и потоковый вывод (``chat_stream``).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from protoprompt.agent import WorkingMemory
from protoprompt.llm import LLMClientProtocol
from protoprompt.tokens.regex_counter import RegexTokenCounter

from protoprompt_cli.actions import parse_actions, strip_actions
from protoprompt_cli.tools import ToolRunner

KIND_BY_TOOL = {
    "bash": "tool_output",
    "read": "file",
    "write": "edit",
    "edit": "edit",
    "glob": "log",
    "grep": "log",
}
PIN_TOOLS = frozenset({"write", "edit"})


@dataclass
class TurnResult:
    """Итог одного хода: финальный ответ и счётчики работы."""

    reply: str = ""
    plan: str = ""
    actions_run: int = 0
    iterations: int = 0
    restored: list[str] = field(default_factory=list)
    streamed: bool = False


class AgentCore:
    def __init__(
        self,
        mem: WorkingMemory,
        llm: LLMClientProtocol,
        tools: ToolRunner,
        *,
        system_prompt: str,
        chat_model: str = "",
        max_iterations: int = 8,
        tail_size: int = 8,
    ) -> None:
        self.mem = mem
        self.llm = llm
        self.tools = tools
        self.system_prompt = system_prompt
        self.chat_model = chat_model
        self.max_iterations = max_iterations
        self.tail_size = tail_size
        self.tail: list[dict] = []
        self.plan_mode = False
        self.counter = RegexTokenCounter()
        self.usage = {"chat_calls": 0, "input_tokens": 0, "output_tokens": 0}

    def reset_usage(self) -> None:
        self.usage = {"chat_calls": 0, "input_tokens": 0, "output_tokens": 0}

    def _message_tokens(self, messages: list[dict]) -> int:
        return sum(4 + self.counter.count(m.get("content", "")) for m in messages)

    def _push_tail(self, message: dict) -> None:
        self.tail.append(message)
        del self.tail[: -self.tail_size]

    async def _chat(self, messages: list[dict], stream_cb=None) -> str:
        streaming = stream_cb is not None and callable(
            getattr(self.llm, "chat_stream", None)
        )
        if streaming:
            return await self.llm.chat_stream(
                messages, model=self.chat_model, on_token=stream_cb
            )
        return await self.llm.chat(messages, model=self.chat_model)

    async def turn(
        self,
        user_text: str,
        *,
        recall_query: str | None = None,
        stream_cb=None,
    ) -> TurnResult:
        result = TurnResult()
        if recall_query:
            result.restored = await self.mem.recall(recall_query)

        if not self.mem.goal.text and user_text.strip():
            await self.mem.set_goal(user_text.strip()[:200])

        await self.mem.add(
            "tool_output", user_text, summary=f"user: {user_text[:60]}"
        )
        self._push_tail({"role": "user", "content": user_text})

        if self.plan_mode:
            result.plan = await self._run_plan(user_text)
            result.reply = result.plan
            result.iterations = 1
            return result

        streamed = stream_cb is not None and callable(
            getattr(self.llm, "chat_stream", None)
        )
        for i in range(self.max_iterations):
            context = await self.mem.assemble()
            system = f"{self.system_prompt}\n\n{context.render()}"
            messages = [{"role": "system", "content": system}, *self.tail]
            self.usage["input_tokens"] += self._message_tokens(messages)
            reply = await self._chat(messages, stream_cb)
            self.usage["chat_calls"] += 1
            self.usage["output_tokens"] += self.counter.count(reply)
            actions = parse_actions(reply)
            result.iterations = i + 1
            last_reply = reply

            if not actions:
                result.reply = reply.strip()
                result.streamed = streamed
                await self.mem.add(
                    "tool_output", reply, summary=f"assistant: {reply[:60]}"
                )
                self._push_tail({"role": "assistant", "content": reply})
                return result

            outputs = []
            for action in actions:
                tool_result = await self.tools.run(action)
                result.actions_run += 1
                kind = KIND_BY_TOOL.get(action.name, "tool_output")
                text = tool_result.output if tool_result.ok else tool_result.error
                await self.mem.add(
                    kind, text, summary=action.summary(),
                    pin=action.name in PIN_TOOLS,
                )
                outputs.append(f"[{action.name}]\n{text[:300]}")

            self._push_tail({"role": "assistant", "content": reply})
            self._push_tail({"role": "user", "content": "\n\n".join(outputs)})

        result.reply = strip_actions(last_reply).strip()
        result.streamed = streamed
        return result

    async def _run_plan(self, user_text: str) -> str:
        context = await self.mem.assemble()
        system = (
            f"{self.system_prompt}\n\n{context.render()}\n\n"
            "Режим планирования: составь детальный план действий. "
            "НЕ вызывай инструменты, не возвращай action-теги."
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_text},
        ]
        self.usage["input_tokens"] += self._message_tokens(messages)
        reply = await self.llm.chat(messages, model=self.chat_model)
        self.usage["chat_calls"] += 1
        plan = strip_actions(reply).strip()
        self.usage["output_tokens"] += self.counter.count(reply)
        if plan:
            await self.mem.note(f"(план) {plan[:2000]}", pin=True)
        self._push_tail({"role": "assistant", "content": plan})
        return plan

    async def compact(self) -> str:
        """Сжать горячий набор в одну пин-заметку (холодная зона цела)."""
        if not self.mem.items:
            return ""
        parts = []
        for item in sorted(self.mem.items.values(), key=lambda i: i.step):
            label = item.summary or item.label
            parts.append(f"[{item.kind}] {label}\n{item.text[:300]}")
        source = "\n\n".join(parts)
        prompt = (
            "Сожми рабочую память кодер-агента в краткий связный обзор: "
            "ключевые факты, решения, затронутые файлы, незавершённые задачи. "
            "Только обзор, без тегов.\n\n"
            + source
        )
        self.usage["input_tokens"] += self.counter.count(prompt)
        summary = await self.llm.chat(
            [{"role": "user", "content": prompt}], model=self.chat_model
        )
        self.usage["chat_calls"] += 1
        self.usage["output_tokens"] += self.counter.count(summary)
        summary = summary.strip()
        if not summary:
            return ""
        for item_id in list(self.mem.items):
            await self.mem.forget(item_id)
        await self.mem.note(f"(сжатая память) {summary}", pin=True)
        return summary
