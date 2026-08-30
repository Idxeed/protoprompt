"""Regression tests for the additive ContextPlan API."""

from __future__ import annotations

import asyncio
from collections import UserDict
import json
from dataclasses import FrozenInstanceError

import pytest

from protoprompt import (
    ContextBlockDecision,
    ContextInput,
    ContextPlan,
    InMemStore,
    RegexTokenCounter,
    TokenBudgetedContextBuilder,
)

from _mocks import MockLLM


async def test_context_only_plan_is_immutable_and_explain_is_content_free():
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        MockLLM(),
        counter=RegexTokenCounter(),
        max_tokens=40,
    )

    plan = await builder.plan(ContextInput(
        query="q",
        system_prompt="do not leak this system instruction",
        include_rag=False,
        include_session=False,
    ))

    assert plan.receipt is None
    assert plan.render_system_prompt() == "do not leak this system instruction"
    assert any(
        decision.block_id == "system" and decision.decision == "included"
        for decision in plan.decisions
    )
    explanation = json.dumps(plan.explain(), allow_nan=False)
    assert "do not leak this system instruction" not in explanation
    with pytest.raises(ValueError, match="context-only"):
        plan.render_messages()
    with pytest.raises(FrozenInstanceError):
        plan.policy_id = "other"  # type: ignore[misc]


async def test_plan_records_rag_provenance_and_budget_decision():
    store = InMemStore()
    store.add(
        "document-7",
        ["retrieved evidence " * 20],
        [[0.5] * 16],
    )
    builder = TokenBudgetedContextBuilder(
        store,
        MockLLM(),
        counter=RegexTokenCounter(),
        max_tokens=18,
    )

    plan = await builder.plan(ContextInput(
        query="question",
        system_prompt="system",
        doc_ids=["document-7"],
        include_session=False,
        top_k_rag=1,
    ))

    rag_decision = next(
        decision for decision in plan.decisions if decision.block_id == "rag[0]"
    )
    assert rag_decision.origin == "rag"
    assert rag_decision.source_id is not None
    assert rag_decision.source_id.startswith("source:")
    assert rag_decision.decision in {"included", "truncated", "excluded"}
    assert rag_decision.candidate_tokens is not None
    assert rag_decision.reason
    assert "document-7" not in json.dumps(plan.explain(), allow_nan=False)


async def test_plan_provenance_handles_a_surrogate_doc_identifier():
    doc_id = "document-\ud800"
    store = InMemStore()
    store.add(doc_id, ["retrieved evidence"], [[0.5] * 16], {"kind": "document"})
    builder = TokenBudgetedContextBuilder(
        store,
        MockLLM(),
        counter=RegexTokenCounter(),
        max_tokens=40,
    )

    plan = await builder.plan(ContextInput(
        query="question",
        system_prompt="system",
        doc_ids=[doc_id],
        include_session=False,
        top_k_rag=1,
    ))

    rag_decision = next(
        decision for decision in plan.decisions if decision.block_id == "rag[0]"
    )
    assert rag_decision.source_id is not None
    assert rag_decision.source_id.startswith("source:")


async def test_session_scope_identifier_never_enters_plan_explanation():
    store = InMemStore()
    private_chat_id = "private-chat-123"
    store.add(
        f"session_{private_chat_id}",
        ["private prior turn"],
        [[0.5] * 16],
    )
    builder = TokenBudgetedContextBuilder(
        store,
        MockLLM(),
        counter=RegexTokenCounter(),
        max_tokens=40,
    )

    plan = await builder.plan(ContextInput(
        query="q",
        chat_id=private_chat_id,
        system_prompt="system",
        include_rag=False,
        include_session=True,
    ))

    session_decision = next(
        decision for decision in plan.decisions if decision.block_id == "session[0]"
    )
    assert session_decision.source_id is None
    assert private_chat_id not in json.dumps(plan.explain(), allow_nan=False)


async def test_plan_messages_receipt_is_exact_and_detached_from_caller_mutation():
    counter = RegexTokenCounter()
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        MockLLM(),
        counter=counter,
        max_tokens=80,
        output_reserve=4,
    )
    history = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call-1", "function": {"name": "find"}}],
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "result"},
    ]
    final_messages = [{"role": "user", "content": "current question"}]
    inp = ContextInput(
        query="q",
        system_prompt="S",
        include_rag=False,
        include_session=False,
    )

    plan = await builder.plan_messages(
        inp,
        history=history,
        final_messages=final_messages,
    )
    rendered = plan.render_messages()

    assert plan.receipt is not None
    assert plan.receipt.input_tokens == counter.count_messages(rendered)
    assert plan.receipt.input_tokens + plan.receipt.output_reserve_tokens <= 80
    assert plan.receipt.final_input_tokens == counter.count_messages(final_messages)
    assert (
        plan.receipt.context_tokens
        + plan.receipt.history_tokens
        + plan.receipt.final_input_tokens
        + plan.receipt.output_reserve_tokens
        + plan.receipt.remaining_tokens
        == 80
    )
    assert builder.last_receipt == plan.receipt
    assert any(
        decision.block_id == "output_reserve" and decision.decision == "reserved"
        for decision in plan.decisions
    )
    assert any(
        decision.origin == "new_input" and decision.decision == "included"
        for decision in plan.decisions
    )

    history[0]["tool_calls"][0]["function"]["name"] = "mutated"
    final_messages[0]["content"] = "mutated"
    rendered[0]["content"] = "mutated-system"
    rerendered = plan.render_messages()
    assert rerendered[-1]["content"] == "current question"
    assert rerendered[0]["content"] == "S"
    assert rerendered[1]["tool_calls"][0]["function"]["name"] == "find"
    assert "mutated" not in json.dumps(plan.explain(), allow_nan=False)


async def test_plan_messages_snapshots_tool_graph_before_retrieval_await():
    class BlockingLLM(MockLLM):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def embed(self, texts, model=""):
            self.started.set()
            await self.release.wait()
            return await super().embed(texts, model=model)

    llm = BlockingLLM()
    builder = TokenBudgetedContextBuilder(
        InMemStore(), llm, counter=RegexTokenCounter(), max_tokens=100
    )
    history = [{
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": "call-1",
            "type": "function",
            "function": {"name": "lookup", "arguments": "{}"},
        }],
    }]
    final_messages = [{
        "role": "tool",
        "tool_call_id": "call-1",
        "content": "result",
    }]

    task = asyncio.create_task(builder.plan_messages(
        ContextInput(query="q", system_prompt="", include_session=False),
        history=history,
        final_messages=final_messages,
    ))
    await asyncio.wait_for(llm.started.wait(), timeout=1)
    history[0]["tool_calls"][0]["id"] = "mutated-call"
    final_messages[0]["tool_call_id"] = "mutated-output"
    llm.release.set()

    plan = await task
    rendered = plan.render_messages()
    assert rendered[0]["tool_calls"][0]["id"] == "call-1"
    assert rendered[1]["tool_call_id"] == "call-1"


async def test_plan_snapshots_context_input_before_retrieval_await():
    class BlockingLLM(MockLLM):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def embed(self, texts, model=""):
            self.started.set()
            await self.release.wait()
            return await super().embed(texts, model=model)

    store = InMemStore()
    store.add("document-before", ["document from the original filter"], [[0.5] * 16])
    store.add("document-after", ["document from the mutated filter"], [[0.5] * 16])
    store.add("session_before", ["memory from the original session"], [[0.5] * 16])
    store.add("session_after", ["memory from the mutated session"], [[0.5] * 16])
    llm = BlockingLLM()
    builder = TokenBudgetedContextBuilder(
        store, llm, counter=RegexTokenCounter(), max_tokens=100
    )
    inp = ContextInput(
        query="q",
        chat_id="before",
        system_prompt="short system instruction",
        doc_ids=["document-before"],
        include_rag=True,
        include_session=True,
    )

    task = asyncio.create_task(builder.plan(inp))
    await asyncio.wait_for(llm.started.wait(), timeout=1)
    inp.system_prompt = "oversized replacement " * 100
    inp.chat_id = "after"
    assert inp.doc_ids is not None
    inp.doc_ids[:] = ["document-after"]
    llm.release.set()

    plan = await task
    report = builder.last_report

    assert plan.render_system_prompt().startswith("short system instruction")
    assert "document from the original filter" in plan.render_system_prompt()
    assert "document from the mutated filter" not in plan.render_system_prompt()
    assert "memory from the original session" in plan.render_system_prompt()
    assert "memory from the mutated session" not in plan.render_system_prompt()
    assert report is not None
    assert report.used_tokens <= 100
    assert report.remaining_tokens >= 0


async def test_legacy_build_messages_is_plan_messages_projection():
    counter = RegexTokenCounter()
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        MockLLM(),
        counter=counter,
        max_tokens=40,
    )
    inp = ContextInput(
        query="q",
        system_prompt="system",
        include_rag=False,
        include_session=False,
    )
    history = [{"role": "user", "content": "old"}]

    plan = await builder.plan_messages(inp, history=history, user_message="new")
    legacy = await builder.build_messages(inp, history=history, user_message="new")

    assert legacy == plan.render_messages()
    assert all(isinstance(item, ContextBlockDecision) for item in plan.decisions)


def test_public_plan_normalizes_mutable_collections_to_immutable_tuples():
    rag_blocks = ["rag evidence"]
    decisions = [ContextBlockDecision(
        block_id="rag[0]",
        origin="rag",
        decision="included",
        reason="selected",
        token_cost=2,
    )]

    plan = ContextPlan(
        schema_version=1,
        trace_id="trace",
        policy_id="policy",
        system_prompt="system",
        rag_blocks=rag_blocks,  # type: ignore[arg-type]
        decisions=decisions,  # type: ignore[arg-type]
    )
    rag_blocks.append("mutated")
    decisions.append(ContextBlockDecision(
        block_id="rag[1]",
        origin="rag",
        decision="excluded",
        reason="over_budget",
    ))

    assert plan.rag_blocks == ("rag evidence",)
    assert len(plan.decisions) == 1


async def test_plan_messages_accepts_nested_mapping_payloads():
    builder = TokenBudgetedContextBuilder(
        InMemStore(), MockLLM(), counter=RegexTokenCounter(), max_tokens=40
    )
    final_messages = [{
        "role": "user",
        "content": UserDict({"text": "portable nested mapping"}),
    }]

    plan = await builder.plan_messages(
        ContextInput(query="q", system_prompt="", include_rag=False, include_session=False),
        final_messages=final_messages,
    )

    assert plan.render_messages() == [{
        "role": "user",
        "content": {"text": "portable nested mapping"},
    }]
