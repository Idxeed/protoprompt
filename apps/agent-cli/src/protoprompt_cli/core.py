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

from protoprompt import ContextInput, InMemStore, TokenBudgetedContextBuilder
from protoprompt.agent import WorkingMemory
from protoprompt.context_plan import ContextPlan, ContextRequestReceipt
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
    request_receipts: list[ContextRequestReceipt] = field(default_factory=list)
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
        request_max_tokens: int = 8192,
        output_reserve_tokens: int = 1024,
        context_builder: TokenBudgetedContextBuilder | None = None,
    ) -> None:
        if request_max_tokens <= 0:
            raise ValueError("request_max_tokens must be positive")
        if output_reserve_tokens <= 0:
            raise ValueError("output_reserve_tokens must be positive")
        if output_reserve_tokens >= request_max_tokens:
            raise ValueError(
                "output_reserve_tokens must be smaller than request_max_tokens"
            )
        self.mem = mem
        self.llm = llm
        self.tools = tools
        self.system_prompt = system_prompt
        self.chat_model = chat_model
        self.max_iterations = max_iterations
        self.tail_size = tail_size
        self.tail: list[dict] = []
        self._tail_groups: list[list[dict]] = []
        self.plan_mode = False
        self.request_max_tokens = request_max_tokens
        self.output_reserve_tokens = output_reserve_tokens
        # WorkingMemory owns the agent's long-lived context.  The request
        # builder only packs that already-rendered context, raw tail, final
        # user/tool continuation, and completion reserve into one hard cap;
        # it must not run a second RAG/session retrieval path here.
        if context_builder is None:
            self.counter = RegexTokenCounter()
            self.context_builder = TokenBudgetedContextBuilder(
                InMemStore(),
                llm,
                counter=self.counter,
                max_tokens=request_max_tokens,
                output_reserve=output_reserve_tokens,
            )
        else:
            # ``_history_before_final`` deliberately preselects whole textual
            # action/result groups.  It must use the same accounting contract
            # and hard cap as the final planner, otherwise a custom smaller
            # builder could let the generic packer split that group again.
            builder_limit = getattr(context_builder, "_max_tokens", None)
            builder_reserve = getattr(context_builder, "_output_reserve", None)
            builder_counter = getattr(context_builder, "_counter", None)
            if (
                builder_limit != request_max_tokens
                or builder_reserve != output_reserve_tokens
                or builder_counter is None
            ):
                raise ValueError(
                    "context_builder must use the same request limits and "
                    "provide a token counter"
                )
            self.context_builder = context_builder
            self.counter = builder_counter
        self.last_context_plan: ContextPlan | None = None
        self.usage = {"chat_calls": 0, "input_tokens": 0, "output_tokens": 0}

    def reset_usage(self) -> None:
        self.usage = {"chat_calls": 0, "input_tokens": 0, "output_tokens": 0}

    def _push_tail(self, message: dict) -> None:
        self._push_tail_group([message])

    def _push_tail_group(self, messages: list[dict]) -> None:
        """Append one logical tail group without splitting continuations.

        Textual actions are not native provider tool-call payloads, so the
        generic history packer cannot infer their dependency graph.  Keep an
        action XML response and its synthetic user tool result as one local
        group: a small tail may exceed its raw-message target by that pair,
        but it can never retain an orphan result later.
        """
        if not messages:
            return
        self._tail_groups.append(messages)
        if self.tail_size > 0:
            while (
                len(self._tail_groups) > 1
                and sum(len(group) for group in self._tail_groups) > self.tail_size
            ):
                self._tail_groups.pop(0)
        self.tail = [message for group in self._tail_groups for message in group]

    @staticmethod
    def _query_for(final_messages: list[dict]) -> str:
        """Best-effort query metadata without inspecting rich payloads."""
        for message in reversed(final_messages):
            content = message.get("content")
            if isinstance(content, str):
                return content
        return ""

    @staticmethod
    def _same_messages_by_identity(left: list[dict], right: list[dict]) -> bool:
        return len(left) == len(right) and all(
            left_item is right_item for left_item, right_item in zip(left, right)
        )

    def _history_before_final(
        self,
        final_messages: list[dict],
        system_prompt: str,
    ) -> list[dict]:
        """Return whole optional tail groups that fit beside final input.

        A tool continuation is sent as a two-message final payload (the
        assistant action plus the synthetic tool result).  The overlap logic
        keeps it from appearing twice even when a tiny raw-tail setting has
        already evicted older groups.  Older text action/result groups are
        preselected under the exact fixed request cost as well: the generic
        provider history packer only recognizes native ``tool_calls`` and
        would otherwise be allowed to trim this textual pair in half.
        """
        groups = list(self._tail_groups)
        if groups and self._same_messages_by_identity(groups[-1], final_messages):
            groups.pop()

        system_messages = (
            [{"role": "system", "content": system_prompt}]
            if system_prompt
            else []
        )
        remaining = (
            self.request_max_tokens
            - self.output_reserve_tokens
            - self.counter.count_messages(system_messages)
            - self.counter.count_messages(final_messages)
        )
        if remaining <= 0:
            return []

        kept_reversed: list[list[dict]] = []
        for group in reversed(groups):
            cost = self.counter.count_messages(group)
            if cost <= remaining:
                kept_reversed.append(group)
                remaining -= cost
        return [
            message
            for group in reversed(kept_reversed)
            for message in group
        ]

    def reset_conversation(self) -> None:
        """Drop raw request history when switching sessions or starting anew."""
        self.tail.clear()
        self._tail_groups.clear()
        self.last_context_plan = None

    async def _plan_request(
        self,
        *,
        system_prompt: str,
        history: list[dict],
        final_messages: list[dict],
    ) -> ContextPlan:
        """Build one bounded provider request and retain its receipt.

        ``ContextPlan`` snapshots all payloads before its asynchronous
        boundary, records exact message framing, and never trims the final
        input.  That makes this the sole request construction path for normal
        turns, planning, and compaction.
        """
        plan = await self.context_builder.plan_messages(
            ContextInput(
                query=self._query_for(final_messages),
                system_prompt=system_prompt,
                include_rag=False,
                include_session=False,
            ),
            history=history,
            final_messages=final_messages,
            output_reserve=self.output_reserve_tokens,
        )
        if plan.receipt is None:  # defensive: plan_messages always supplies it
            raise RuntimeError("bounded request plan is missing its receipt")
        self.last_context_plan = plan
        return plan

    async def _preflight_final_input(self, final_messages: list[dict]) -> None:
        """Reject an impossible mandatory input before memory can mutate.

        ``recall()`` and ``assemble()`` can update the working-memory state.
        Run this deliberately context-free check first, so a final user input
        that cannot fit even on its own never triggers those side effects.
        It intentionally does not become ``last_context_plan``: the actual
        request plan below remains the only receipt exposed for the turn.
        """
        await self.context_builder.plan_messages(
            ContextInput(
                query=self._query_for(final_messages),
                system_prompt="",
                include_rag=False,
                include_session=False,
            ),
            history=[],
            final_messages=final_messages,
            output_reserve=self.output_reserve_tokens,
        )

    async def _chat(
        self,
        messages: list[dict],
        *,
        output_reserve_tokens: int,
        stream_cb=None,
    ) -> str:
        options = {"max_tokens": output_reserve_tokens}
        streaming = stream_cb is not None and self._streaming_supported()
        if streaming:
            return await self.llm.chat_stream(
                messages, model=self.chat_model, on_token=stream_cb, **options
            )
        return await self.llm.chat(messages, model=self.chat_model, **options)

    def _streaming_supported(self) -> bool:
        """Avoid advertising streaming through a wrapper whose inner client lacks it."""
        if not callable(getattr(self.llm, "chat_stream", None)):
            return False
        inner = getattr(self.llm, "inner", None)
        return inner is None or callable(getattr(inner, "chat_stream", None))

    async def _record_user_turn(self, user_text: str, message: dict) -> None:
        """Commit raw user evidence only after its mandatory input is planned.

        The initial goal is established separately after the side-effect-free
        final-input preflight, so memory ranking can already use it while the
        full request is planned.  The user item and raw tail still wait until
        after that plan succeeds.
        """
        await self.mem.add(
            "tool_output", user_text, summary=f"user: {user_text[:60]}"
        )
        self._push_tail(message)

    async def _establish_initial_goal(self, user_text: str) -> None:
        """Set the first-session goal after input validation, before ranking."""
        if not self.mem.goal.text and user_text.strip():
            await self.mem.set_goal(user_text.strip()[:200])

    async def turn(
        self,
        user_text: str,
        *,
        recall_query: str | None = None,
        stream_cb=None,
    ) -> TurnResult:
        result = TurnResult()
        final_messages = [{"role": "user", "content": user_text}]
        # A rejected mandatory input must not restore cold records, fill
        # missing vectors, establish a goal, or enter the raw tail.
        await self._preflight_final_input(final_messages)
        await self._establish_initial_goal(user_text)
        if recall_query:
            result.restored = await self.mem.recall(recall_query)

        if self.plan_mode:
            result.plan = await self._run_plan(user_text, final_messages)
            result.reply = result.plan
            result.iterations = 1
            if self.last_context_plan and self.last_context_plan.receipt:
                result.request_receipts.append(self.last_context_plan.receipt)
            return result

        streamed = stream_cb is not None and self._streaming_supported()
        last_reply = ""
        user_committed = False
        for i in range(self.max_iterations):
            context = await self.mem.assemble()
            system = f"{self.system_prompt}\n\n{context.render()}"
            request_plan = await self._plan_request(
                system_prompt=system,
                history=self._history_before_final(final_messages, system),
                final_messages=final_messages,
            )
            receipt = request_plan.receipt
            assert receipt is not None
            messages = request_plan.render_messages()
            self.usage["input_tokens"] += receipt.input_tokens
            result.request_receipts.append(receipt)
            if not user_committed:
                await self._record_user_turn(user_text, final_messages[0])
                user_committed = True
            reply = await self._chat(
                messages,
                output_reserve_tokens=receipt.output_reserve_tokens,
                stream_cb=stream_cb,
            )
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

            action_message = {"role": "assistant", "content": reply}
            tool_result_message = {
                "role": "user",
                "content": "\n\n".join(outputs),
            }
            self._push_tail_group([action_message, tool_result_message])
            # This pair is mandatory for the immediate continuation.  Passing
            # it as one final payload means budget trimming cannot retain an
            # action without its corresponding result (or vice versa).
            final_messages = [action_message, tool_result_message]

        result.reply = strip_actions(last_reply).strip()
        result.streamed = streamed
        return result

    async def _run_plan(self, user_text: str, final_messages: list[dict]) -> str:
        context = await self.mem.assemble()
        system = (
            f"{self.system_prompt}\n\n{context.render()}\n\n"
            "Режим планирования: составь детальный план действий. "
            "НЕ вызывай инструменты, не возвращай action-теги."
        )
        request_plan = await self._plan_request(
            system_prompt=system,
            history=[],
            final_messages=final_messages,
        )
        receipt = request_plan.receipt
        assert receipt is not None
        self.usage["input_tokens"] += receipt.input_tokens
        await self._record_user_turn(user_text, final_messages[0])
        reply = await self._chat(
            request_plan.render_messages(),
            output_reserve_tokens=receipt.output_reserve_tokens,
        )
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
        request_plan = await self._plan_request(
            system_prompt="",
            history=[],
            final_messages=[{"role": "user", "content": prompt}],
        )
        receipt = request_plan.receipt
        assert receipt is not None
        self.usage["input_tokens"] += receipt.input_tokens
        summary = await self._chat(
            request_plan.render_messages(),
            output_reserve_tokens=receipt.output_reserve_tokens,
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
