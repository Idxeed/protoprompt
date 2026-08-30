from __future__ import annotations

import pytest

from protoprompt import InMemStore, MemoryScope, TokenBudgetExceededError
from protoprompt.injector_budgeted import TokenBudgetedContextBuilder
from protoprompt.integrations.agents_sdk import (
    ProtoPromptSession,
    create_session_input_callback,
)
from protoprompt.rag import DocumentIndexer
from protoprompt.tokens.regex_counter import RegexTokenCounter

from _mocks import MockLLM

try:
    from agents.memory.session import Session as AgentsSession
except ImportError:  # pragma: no cover - exercised in minimal environments
    AgentsSession = None


@pytest.mark.asyncio
@pytest.mark.skipif(AgentsSession is None, reason="openai-agents extra not installed")
async def test_protoprompt_session_matches_upstream_protocol_and_tail_semantics(tmp_path):
    session = ProtoPromptSession("thread-1", tmp_path / "agents.db")
    assert isinstance(session, AgentsSession)
    items = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
        {"role": "user", "content": "third"},
    ]
    await session.add_items(items)
    assert await session.get_items(limit=2) == items[-2:]
    assert await session.pop_item() == items[-1]
    assert await session.get_items() == items[:-1]
    await session.clear_session()
    assert await session.get_items() == []
    assert await session.pop_item() is None


@pytest.mark.asyncio
async def test_budgeted_session_callback_injects_recall_and_preserves_new_turn():
    store = InMemStore()
    llm = MockLLM(embed_dim=8)
    scope = MemoryScope(tenant="acme", user="alice", thread="thread-1")
    await DocumentIndexer(store, llm, scope=scope).index(
        "contract",
        "The old contract renews in May",
    )
    builder = TokenBudgetedContextBuilder(
        store,
        llm,
        counter=RegexTokenCounter(),
        max_tokens=45,
        scope=scope,
    )
    callback = create_session_input_callback(
        builder,
        session_id="thread-1",
        system_prompt="You are a legal assistant.",
    )
    history = [
        {"role": "user", "content": f"old turn {index} " + "noise " * 8}
        for index in range(8)
    ]
    new_input = [{"role": "user", "content": "When does the contract renew?"}]

    prepared = await callback(history, new_input)

    assert prepared[-1] == new_input[0]
    assert prepared.count(new_input[0]) == 1
    assert prepared[0]["role"] == "system"
    assert "renews in May" in prepared[0]["content"]
    assert len(prepared) < len(history) + len(new_input) + 1
    assert builder.last_report.history_kept == len(prepared) - 2


@pytest.mark.asyncio
async def test_session_callback_reads_structured_agents_content_blocks():
    store = InMemStore()
    llm = MockLLM(embed_dim=4)
    builder = TokenBudgetedContextBuilder(
        store,
        llm,
        max_tokens=30,
    )
    seen_queries: list[str] = []

    def factory(query: str):
        from protoprompt import ContextInput

        seen_queries.append(query)
        return ContextInput(query=query, include_rag=False, include_session=False)

    callback = create_session_input_callback(
        builder,
        session_id="s",
        context_factory=factory,
    )
    new_input = [{
        "role": "user",
        "content": [{"type": "input_text", "text": "structured question"}],
    }]
    prepared = await callback([], new_input)
    assert seen_queries == ["structured question"]
    assert prepared[-1] == new_input[0]


@pytest.mark.asyncio
async def test_session_callback_reserves_output_before_system_and_history():
    counter = RegexTokenCounter()
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        MockLLM(embed_dim=2),
        counter=counter,
        max_tokens=30,
    )

    def factory(query: str):
        from protoprompt import ContextInput

        return ContextInput(
            query=query,
            system_prompt="system instruction",
            include_rag=False,
            include_session=False,
        )

    callback = create_session_input_callback(
        builder,
        session_id="s",
        context_factory=factory,
        output_reserve=7,
    )
    new_input = [{"role": "user", "content": "final question"}]
    prepared = await callback(
        [{"role": "user", "content": "old turn " * 10}],
        new_input,
    )

    assert prepared[-1] == new_input[0]
    assert counter.count_messages(prepared) + 7 <= 30
    assert builder.last_report.output_reserve_tokens == 7
    assert builder.last_report.used_tokens == counter.count_messages(prepared)


@pytest.mark.asyncio
async def test_session_callback_rejects_tool_call_arguments_that_exceed_budget():
    counter = RegexTokenCounter()
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        MockLLM(embed_dim=2),
        counter=counter,
        max_tokens=30,
    )

    def factory(query: str):
        from protoprompt import ContextInput

        return ContextInput(query=query, include_rag=False, include_session=False)

    callback = create_session_input_callback(
        builder,
        session_id="s",
        context_factory=factory,
    )
    new_input = [{
        "role": "assistant",
        "content": None,
        "tool_calls": [{
            "id": "call_1",
            "type": "function",
            "function": {
                "name": "search",
                "arguments": "argument " * 200,
            },
        }],
    }]

    assert counter.count_messages(new_input) > 30
    with pytest.raises(TokenBudgetExceededError) as exc:
        await callback([], new_input)
    assert exc.value.section == "new_input"


@pytest.mark.asyncio
async def test_session_callback_drops_responses_call_history_atomically_when_it_does_not_fit():
    counter = RegexTokenCounter()
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        MockLLM(embed_dim=2),
        counter=counter,
        max_tokens=45,
    )

    def factory(query: str):
        from protoprompt import ContextInput

        return ContextInput(query=query, include_rag=False, include_session=False)

    callback = create_session_input_callback(
        builder,
        session_id="s",
        context_factory=factory,
    )
    history = [
        {"type": "reasoning", "id": "rsn_1", "summary": []},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "search",
            "arguments": "argument " * 40,
        },
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "small result",
        },
    ]
    new_input = [{"role": "user", "content": "continue"}]

    prepared = await callback(history, new_input)

    assert prepared == new_input
    assert {"history[0]", "history[1]", "history[2]"} <= set(
        builder.last_report.dropped_blocks
    )


@pytest.mark.asyncio
async def test_session_callback_drops_out_of_order_responses_call_pair():
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        MockLLM(embed_dim=2),
        counter=RegexTokenCounter(),
        max_tokens=500,
    )

    def factory(query: str):
        from protoprompt import ContextInput

        return ContextInput(query=query, include_rag=False, include_session=False)

    callback = create_session_input_callback(
        builder,
        session_id="s",
        context_factory=factory,
    )
    history = [
        {"type": "function_call_output", "call_id": "call_1", "output": "result"},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "search",
            "arguments": "{}",
        },
    ]
    new_input = [{"role": "user", "content": "continue"}]

    prepared = await callback(history, new_input)

    assert prepared == new_input
    assert {"history[0]", "history[1]"} <= set(builder.last_report.dropped_blocks)


@pytest.mark.asyncio
async def test_session_callback_keeps_anonymous_server_tool_search_pair():
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        MockLLM(embed_dim=2),
        counter=RegexTokenCounter(),
        max_tokens=500,
    )

    def factory(query: str):
        from protoprompt import ContextInput

        return ContextInput(query=query, include_rag=False, include_session=False)

    callback = create_session_input_callback(
        builder,
        session_id="s",
        context_factory=factory,
    )
    # This is the shape produced by the SDK's ResponseToolSearchCall and
    # ResponseToolSearchOutputItem model_dump(exclude_none=True) when the
    # server executes the search: each has its own item id but no call_id.
    history = [
        {
            "type": "tool_search_call",
            "id": "tsc_1",
            "arguments": {"query": "search"},
            "execution": "server",
            "status": "completed",
        },
        {
            "type": "tool_search_output",
            "id": "tso_1",
            "tools": [],
            "execution": "server",
            "status": "completed",
        },
    ]
    new_input = [{"role": "user", "content": "continue"}]

    prepared = await callback(history, new_input)

    assert prepared == [*history, *new_input]
    assert builder.last_report.history_kept == len(history)


@pytest.mark.asyncio
async def test_session_callback_keeps_reasoning_with_hosted_response_item():
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        MockLLM(embed_dim=2),
        counter=RegexTokenCounter(),
        max_tokens=500,
    )

    def factory(query: str):
        from protoprompt import ContextInput

        return ContextInput(query=query, include_rag=False, include_session=False)

    callback = create_session_input_callback(
        builder,
        session_id="s",
        context_factory=factory,
    )
    history = [
        {
            "type": "reasoning",
            "id": "rsn_1",
            "summary": [],
            "encrypted_content": "opaque",
        },
        {
            "type": "web_search_call",
            "id": "ws_1",
            "action": {"type": "search", "query": "q"},
            "status": "completed",
        },
    ]
    new_input = [{"role": "user", "content": "continue"}]

    prepared = await callback(history, new_input)

    assert prepared == [*history, *new_input]
    assert builder.last_report.history_kept == len(history)


@pytest.mark.asyncio
async def test_session_callback_keeps_canonical_local_shell_pair():
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        MockLLM(embed_dim=2),
        counter=RegexTokenCounter(),
        max_tokens=500,
    )

    def factory(query: str):
        from protoprompt import ContextInput

        return ContextInput(query=query, include_rag=False, include_session=False)

    callback = create_session_input_callback(
        builder,
        session_id="s",
        context_factory=factory,
    )
    call = {
        "type": "local_shell_call",
        "id": "lsc_item_1",
        "call_id": "local_1",
        "action": {"type": "exec", "command": ["echo", "hi"], "env": {}},
        "status": "completed",
    }
    output = {
        "type": "local_shell_call_output",
        "id": "local_1",
        "output": '{"stdout":"hi"}',
        "status": "completed",
    }

    prepared = await callback([call, output], [{"role": "user", "content": "continue"}])

    assert prepared == [call, output, {"role": "user", "content": "continue"}]
    assert builder.last_report.history_kept == 2

    resumed = await callback([call], [output])

    assert resumed == [call, output]
    assert builder.last_report.history_kept == 1


@pytest.mark.asyncio
async def test_session_callback_keeps_active_program_and_completed_child():
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        MockLLM(embed_dim=2),
        counter=RegexTokenCounter(),
        max_tokens=500,
    )

    def factory(query: str):
        from protoprompt import ContextInput

        return ContextInput(query=query, include_rag=False, include_session=False)

    callback = create_session_input_callback(
        builder,
        session_id="s",
        context_factory=factory,
    )
    program = {
        "type": "program",
        "id": "prog_1",
        "call_id": "program_1",
        "code": 'await tools.search({"q":"x"})',
        "fingerprint": "fp",
    }
    call = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "function_1",
        "name": "search",
        "arguments": "{}",
        "caller": {"type": "program", "caller_id": "program_1"},
    }
    output = {
        "type": "function_call_output",
        "call_id": "function_1",
        "output": "result",
        "caller": {"type": "program", "caller_id": "program_1"},
    }
    history = [program, call, output]
    new_input = [{"role": "user", "content": "continue"}]

    prepared = await callback(history, new_input)

    assert prepared == [*history, *new_input]
    assert builder.last_report.history_kept == len(history)


@pytest.mark.asyncio
async def test_session_callback_keeps_active_program_for_final_child_output():
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        MockLLM(embed_dim=2),
        counter=RegexTokenCounter(),
        max_tokens=500,
    )

    def factory(query: str):
        from protoprompt import ContextInput

        return ContextInput(query=query, include_rag=False, include_session=False)

    callback = create_session_input_callback(
        builder,
        session_id="s",
        context_factory=factory,
    )
    program = {
        "type": "program",
        "id": "prog_1",
        "call_id": "program_1",
        "code": 'await tools.search({"q":"x"})',
        "fingerprint": "fp",
    }
    call = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "function_1",
        "name": "search",
        "arguments": "{}",
        "caller": {"type": "program", "caller_id": "program_1"},
    }
    output = {
        "type": "function_call_output",
        "call_id": "function_1",
        "output": "result",
        "caller": {"type": "program", "caller_id": "program_1"},
    }

    prepared = await callback([program, call], [output])

    assert prepared == [program, call, output]
    assert builder.last_report.history_kept == 2

    output_without_caller = {
        "type": "function_call_output",
        "call_id": "function_1",
        "output": "result",
    }
    prepared = await callback([program, call], [output_without_caller])

    assert prepared == [program, call, output_without_caller]


@pytest.mark.asyncio
async def test_session_callback_rejects_program_dependency_that_cannot_fit():
    counter = RegexTokenCounter()
    program = {
        "type": "program",
        "id": "prog_1",
        "call_id": "program_1",
        "code": "await tools.search(" + '"x"' * 50 + ")",
        "fingerprint": "fp",
    }
    call = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "function_1",
        "name": "search",
        "arguments": "{}",
        "caller": {"type": "program", "caller_id": "program_1"},
    }
    output = {
        "type": "function_call_output",
        "call_id": "function_1",
        "output": "result",
        "caller": {"type": "program", "caller_id": "program_1"},
    }
    history = [program, call]
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        MockLLM(embed_dim=2),
        counter=counter,
        max_tokens=counter.count_messages(history) + counter.count_messages([output]) - 1,
    )

    def factory(query: str):
        from protoprompt import ContextInput

        return ContextInput(query=query, include_rag=False, include_session=False)

    callback = create_session_input_callback(
        builder,
        session_id="s",
        context_factory=factory,
    )

    with pytest.raises(TokenBudgetExceededError) as exc:
        await callback(history, [output])
    assert exc.value.section == "history_dependency"


@pytest.mark.asyncio
async def test_session_callback_keeps_trailing_responses_call_for_final_output():
    counter = RegexTokenCounter()
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        MockLLM(embed_dim=2),
        counter=counter,
        max_tokens=500,
    )

    def factory(query: str):
        from protoprompt import ContextInput

        return ContextInput(query=query, include_rag=False, include_session=False)

    callback = create_session_input_callback(
        builder,
        session_id="s",
        context_factory=factory,
    )
    history = [
        {"type": "reasoning", "id": "rsn_1", "summary": []},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "search",
            "arguments": "{}",
        },
    ]
    new_input = [
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "result",
        }
    ]

    prepared = await callback(history, new_input)

    assert prepared == [*history, *new_input]
    assert builder.last_report.history_kept == len(history)


@pytest.mark.asyncio
async def test_session_callback_keeps_trailing_mcp_approval_request_for_final_response():
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        MockLLM(embed_dim=2),
        counter=RegexTokenCounter(),
        max_tokens=500,
    )

    def factory(query: str):
        from protoprompt import ContextInput

        return ContextInput(query=query, include_rag=False, include_session=False)

    callback = create_session_input_callback(
        builder,
        session_id="s",
        context_factory=factory,
    )
    history = [{
        "type": "mcp_approval_request",
        "id": "approval_1",
        "name": "dangerous",
        "server_label": "mcp",
        "arguments": "{}",
    }]
    new_input = [{
        "type": "mcp_approval_response",
        "approval_request_id": "approval_1",
        "approve": True,
    }]

    prepared = await callback(history, new_input)

    assert prepared == [*history, *new_input]
    assert builder.last_report.history_kept == len(history)


@pytest.mark.asyncio
async def test_session_callback_rejects_required_history_call_that_cannot_fit():
    counter = RegexTokenCounter()
    history = [{
        "type": "function_call",
        "call_id": "call_1",
        "name": "search",
        "arguments": "argument " * 30,
    }]
    new_input = [{
        "type": "function_call_output",
        "call_id": "call_1",
        "output": "result",
    }]
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        MockLLM(embed_dim=2),
        counter=counter,
        max_tokens=counter.count_messages(history) + counter.count_messages(new_input) - 1,
    )

    def factory(query: str):
        from protoprompt import ContextInput

        return ContextInput(query=query, include_rag=False, include_session=False)

    callback = create_session_input_callback(
        builder,
        session_id="s",
        context_factory=factory,
    )

    with pytest.raises(TokenBudgetExceededError) as exc:
        await callback(history, new_input)
    assert exc.value.section == "history_dependency"


@pytest.mark.asyncio
async def test_session_callback_rejects_required_mcp_approval_request_that_cannot_fit():
    counter = RegexTokenCounter()
    history = [{
        "type": "mcp_approval_request",
        "id": "approval_1",
        "name": "dangerous",
        "server_label": "mcp",
        "arguments": "argument " * 30,
    }]
    new_input = [{
        "type": "mcp_approval_response",
        "approval_request_id": "approval_1",
        "approve": True,
    }]
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        MockLLM(embed_dim=2),
        counter=counter,
        max_tokens=counter.count_messages(history) + counter.count_messages(new_input) - 1,
    )

    def factory(query: str):
        from protoprompt import ContextInput

        return ContextInput(query=query, include_rag=False, include_session=False)

    callback = create_session_input_callback(
        builder,
        session_id="s",
        context_factory=factory,
    )

    with pytest.raises(TokenBudgetExceededError) as exc:
        await callback(history, new_input)
    assert exc.value.section == "history_dependency"


@pytest.mark.asyncio
async def test_session_callback_keeps_interleaved_program_graph():
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        MockLLM(embed_dim=2),
        counter=RegexTokenCounter(),
        max_tokens=500,
    )

    def factory(query: str):
        from protoprompt import ContextInput

        return ContextInput(query=query, include_rag=False, include_session=False)

    callback = create_session_input_callback(
        builder,
        session_id="s",
        context_factory=factory,
    )
    p1 = {
        "type": "program",
        "id": "prog_1",
        "call_id": "program_1",
        "code": "await tools.one()",
        "fingerprint": "fp_1",
    }
    f1 = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "function_1",
        "name": "one",
        "arguments": "{}",
        "caller": {"type": "program", "caller_id": "program_1"},
    }
    p2 = {
        "type": "program",
        "id": "prog_2",
        "call_id": "program_2",
        "code": "await tools.two()",
        "fingerprint": "fp_2",
    }
    f2 = {
        "type": "function_call",
        "id": "fc_2",
        "call_id": "function_2",
        "name": "two",
        "arguments": "{}",
        "caller": {"type": "program", "caller_id": "program_2"},
    }
    o1 = {
        "type": "function_call_output",
        "call_id": "function_1",
        "output": "one",
        "caller": {"type": "program", "caller_id": "program_1"},
    }
    o2 = {
        "type": "function_call_output",
        "call_id": "function_2",
        "output": "two",
        "caller": {"type": "program", "caller_id": "program_2"},
    }
    history = [p1, f1, p2, f2, o1, o2]
    new_input = [{"role": "user", "content": "continue"}]

    prepared = await callback(history, new_input)

    assert prepared == [*history, *new_input]
    assert builder.last_report.history_kept == len(history)


@pytest.mark.asyncio
async def test_session_callback_drops_child_after_completed_program_output():
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        MockLLM(embed_dim=2),
        counter=RegexTokenCounter(),
        max_tokens=500,
    )

    def factory(query: str):
        from protoprompt import ContextInput

        return ContextInput(query=query, include_rag=False, include_session=False)

    callback = create_session_input_callback(
        builder,
        session_id="s",
        context_factory=factory,
    )
    program = {
        "type": "program",
        "id": "prog_1",
        "call_id": "program_1",
        "code": "await tools.one()",
        "fingerprint": "fp_1",
    }
    program_output = {
        "type": "program_output",
        "id": "program_out_1",
        "call_id": "program_1",
        "result": "done",
        "status": "completed",
    }
    child_call = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "function_1",
        "name": "one",
        "arguments": "{}",
        "caller": {"type": "program", "caller_id": "program_1"},
    }
    child_output = {
        "type": "function_call_output",
        "call_id": "function_1",
        "output": "one",
        "caller": {"type": "program", "caller_id": "program_1"},
    }
    history = [program, program_output, child_call, child_output]
    new_input = [{"role": "user", "content": "continue"}]

    prepared = await callback(history, new_input)

    assert prepared == new_input
    assert {f"history[{index}]" for index in range(len(history))} <= set(
        builder.last_report.dropped_blocks
    )

    # Responses input permits a child output to omit ``caller``.  Its
    # correlation ID still makes it program-owned, so it cannot appear after
    # the program's terminal result.
    callerless_child_output = {
        key: value for key, value in child_output.items() if key != "caller"
    }
    late_callerless_history = [
        program,
        child_call,
        program_output,
        callerless_child_output,
    ]
    prepared = await callback(late_callerless_history, new_input)

    assert prepared == new_input
    assert {
        f"history[{index}]" for index in range(len(late_callerless_history))
    } <= set(builder.last_report.dropped_blocks)

    # The same derived ownership applies to streamed and anonymous server-side
    # outputs, whose output schemas also omit the parent ``caller``.
    shell_call = {
        "type": "shell_call",
        "id": "shell_1",
        "call_id": "shell_call_1",
        "action": {"commands": ["echo x"]},
        "status": "completed",
        "caller": {"type": "program", "caller_id": "program_1"},
    }
    shell_output = {
        "type": "shell_call_output",
        "id": "shell_out_1",
        "call_id": "shell_call_1",
        "output": [],
        "status": "completed",
    }
    search_call = {
        "type": "tool_search_call",
        "id": "tsc_1",
        "arguments": {"query": "search"},
        "execution": "server",
        "status": "completed",
        "caller": {"type": "program", "caller_id": "program_1"},
    }
    search_output = {
        "type": "tool_search_output",
        "id": "tso_1",
        "tools": [],
        "execution": "server",
        "status": "completed",
    }
    for late_history in (
        [program, shell_call, program_output, shell_output],
        [program, search_call, program_output, search_output],
    ):
        prepared = await callback(late_history, new_input)

        assert prepared == new_input
        assert {
            f"history[{index}]" for index in range(len(late_history))
        } <= set(builder.last_report.dropped_blocks)

    incomplete_output = {
        **program_output,
        "id": "program_out_incomplete",
        "result": "part",
        "status": "incomplete",
    }
    resumable_history = [program, incomplete_output, child_call, child_output]
    prepared = await callback(resumable_history, new_input)

    assert prepared == [*resumable_history, *new_input]

    transition_history = [program, incomplete_output, program_output]
    prepared = await callback(transition_history, new_input)

    assert prepared == [*transition_history, *new_input]


@pytest.mark.asyncio
async def test_session_callback_keeps_streamed_shell_output_sequence():
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        MockLLM(embed_dim=2),
        counter=RegexTokenCounter(),
        max_tokens=500,
    )

    def factory(query: str):
        from protoprompt import ContextInput

        return ContextInput(query=query, include_rag=False, include_session=False)

    callback = create_session_input_callback(
        builder,
        session_id="s",
        context_factory=factory,
    )
    call = {
        "type": "shell_call",
        "id": "shell_1",
        "call_id": "shell_call_1",
        "action": {"commands": ["echo x"]},
        "status": "completed",
    }
    progress = {
        "type": "shell_call_output",
        "id": "shell_out_1",
        "call_id": "shell_call_1",
        "output": [],
        "status": "in_progress",
    }
    terminal = {
        "type": "shell_call_output",
        "id": "shell_out_2",
        "call_id": "shell_call_1",
        "output": [],
        "status": "completed",
    }
    history = [call, progress, terminal]
    new_input = [{"role": "user", "content": "continue"}]

    prepared = await callback(history, new_input)

    assert prepared == [*history, *new_input]

    prepared = await callback([call, progress], [terminal])

    assert prepared == [call, progress, terminal]

    invalid_history = [call, terminal, progress]
    prepared = await callback(invalid_history, new_input)

    assert prepared == new_input


@pytest.mark.asyncio
async def test_session_callback_keeps_streamed_identified_tool_search_output_sequence():
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        MockLLM(embed_dim=2),
        counter=RegexTokenCounter(),
        max_tokens=500,
    )

    def factory(query: str):
        from protoprompt import ContextInput

        return ContextInput(query=query, include_rag=False, include_session=False)

    callback = create_session_input_callback(
        builder,
        session_id="s",
        context_factory=factory,
    )
    call = {
        "type": "tool_search_call",
        "id": "tsc_1",
        "call_id": "tool_search_1",
        "arguments": {"query": "search"},
        "execution": "client",
        "status": "completed",
    }
    progress = {
        "type": "tool_search_output",
        "id": "tso_1",
        "call_id": "tool_search_1",
        "tools": [],
        "execution": "client",
        "status": "in_progress",
    }
    terminal = {
        **progress,
        "id": "tso_2",
        "status": "completed",
    }
    history = [call, progress, terminal]
    new_input = [{"role": "user", "content": "continue"}]

    prepared = await callback(history, new_input)

    assert prepared == [*history, *new_input]

    prepared = await callback([call, progress], [terminal])

    assert prepared == [call, progress, terminal]

    prepared = await callback([call, terminal, progress], new_input)

    assert prepared == new_input


@pytest.mark.asyncio
async def test_session_callback_keeps_mixed_responses_dependency_graph_in_tail():
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        MockLLM(embed_dim=2),
        counter=RegexTokenCounter(),
        max_tokens=500,
    )

    def factory(query: str):
        from protoprompt import ContextInput

        return ContextInput(query=query, include_rag=False, include_session=False)

    callback = create_session_input_callback(
        builder,
        session_id="s",
        context_factory=factory,
    )
    direct = {
        "type": "function_call",
        "id": "fc_direct",
        "call_id": "direct_1",
        "name": "direct",
        "arguments": "{}",
    }
    p1 = {
        "type": "program",
        "id": "prog_1",
        "call_id": "program_1",
        "code": "await tools.one()",
        "fingerprint": "fp_1",
    }
    f1 = {
        "type": "function_call",
        "id": "fc_1",
        "call_id": "function_1",
        "name": "one",
        "arguments": "{}",
        "caller": {"type": "program", "caller_id": "program_1"},
    }
    p2 = {
        "type": "program",
        "id": "prog_2",
        "call_id": "program_2",
        "code": "await tools.two()",
        "fingerprint": "fp_2",
    }
    f2 = {
        "type": "function_call",
        "id": "fc_2",
        "call_id": "function_2",
        "name": "two",
        "arguments": "{}",
        "caller": {"type": "program", "caller_id": "program_2"},
    }
    outputs = [
        {
            "type": "function_call_output",
            "call_id": "direct_1",
            "output": "direct",
        },
        {
            "type": "function_call_output",
            "call_id": "function_1",
            "output": "one",
            "caller": {"type": "program", "caller_id": "program_1"},
        },
        {
            "type": "function_call_output",
            "call_id": "function_2",
            "output": "two",
            "caller": {"type": "program", "caller_id": "program_2"},
        },
    ]
    history = [direct, p1, f1, p2, f2]

    prepared = await callback(history, outputs)

    assert prepared == [*history, *outputs]
    assert builder.last_report.history_kept == len(history)


@pytest.mark.asyncio
async def test_session_callback_keeps_anonymous_server_tool_search_across_boundary():
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        MockLLM(embed_dim=2),
        counter=RegexTokenCounter(),
        max_tokens=500,
    )

    def factory(query: str):
        from protoprompt import ContextInput

        return ContextInput(query=query, include_rag=False, include_session=False)

    callback = create_session_input_callback(
        builder,
        session_id="s",
        context_factory=factory,
    )
    call = {
        "type": "tool_search_call",
        "id": "tsc_1",
        "arguments": {"query": "search"},
        "execution": "server",
        "status": "completed",
    }
    output = {
        "type": "tool_search_output",
        "id": "tso_1",
        "tools": [],
        "execution": "server",
        "status": "completed",
    }
    new_input = [{"role": "user", "content": "continue"}]

    prepared = await callback([call], [output])

    assert prepared == [call, output]
    assert builder.last_report.history_kept == 1

    # A server-side search can be emitted as a normal Responses stream.  It
    # still has no ``call_id``, so the single preceding call is the only safe
    # correlation edge across the history/new-input boundary.
    progress = {**output, "id": "tso_progress", "status": "in_progress"}
    terminal = {**output, "id": "tso_terminal", "status": "completed"}
    prepared = await callback([call], [progress, terminal])

    assert prepared == [call, progress, terminal]

    prepared = await callback([call, terminal, progress], new_input)

    assert prepared == new_input

    # ``execution`` is optional on the public replay schemas.  The SDK still
    # treats the pair as server-side and matches anonymous items by order.
    implicit_call = {key: value for key, value in call.items() if key != "execution"}
    implicit_output = {
        key: value for key, value in output.items() if key != "execution"
    }
    prepared = await callback([implicit_call], [implicit_output])

    assert prepared == [implicit_call, implicit_output]

    client_call = {**call, "execution": "client"}
    client_output = {**output, "execution": "client"}
    with pytest.raises(ValueError, match="matching trailing history call"):
        await callback([client_call], [client_output])


@pytest.mark.asyncio
async def test_session_callback_drops_input_only_items_and_dangling_reasoning():
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        MockLLM(embed_dim=2),
        counter=RegexTokenCounter(),
        max_tokens=500,
    )

    def factory(query: str):
        from protoprompt import ContextInput

        return ContextInput(query=query, include_rag=False, include_session=False)

    callback = create_session_input_callback(
        builder,
        session_id="s",
        context_factory=factory,
    )
    user_message = {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "previous user turn"}],
    }
    history = [
        {
            "type": "reasoning",
            "id": "rs_1",
            "summary": [],
            "encrypted_content": "opaque",
        },
        user_message,
        {"type": "compaction_trigger"},
        {"type": "item_reference", "id": "item_1"},
    ]
    new_input = [{"role": "user", "content": "next"}]

    prepared = await callback(history, new_input)

    assert prepared == [user_message, *new_input]
    assert {"history[0]", "history[2]", "history[3]"} <= set(
        builder.last_report.dropped_blocks
    )


@pytest.mark.asyncio
async def test_session_callback_keeps_reasoning_with_output_compaction_item():
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        MockLLM(embed_dim=2),
        counter=RegexTokenCounter(),
        max_tokens=500,
    )

    def factory(query: str):
        from protoprompt import ContextInput

        return ContextInput(query=query, include_rag=False, include_session=False)

    callback = create_session_input_callback(
        builder,
        session_id="s",
        context_factory=factory,
    )
    history = [
        {
            "type": "reasoning",
            "id": "rs_1",
            "summary": [],
            "encrypted_content": "opaque",
        },
        {
            "type": "compaction",
            "id": "cmp_1",
            "encrypted_content": "opaque-compaction",
        },
    ]
    new_input = [{"role": "user", "content": "next"}]

    prepared = await callback(history, new_input)

    assert prepared == [*history, *new_input]


@pytest.mark.asyncio
async def test_session_callback_keeps_reasoning_with_output_assistant_message():
    builder = TokenBudgetedContextBuilder(
        InMemStore(),
        MockLLM(embed_dim=2),
        counter=RegexTokenCounter(),
        max_tokens=500,
    )

    def factory(query: str):
        from protoprompt import ContextInput

        return ContextInput(query=query, include_rag=False, include_session=False)

    callback = create_session_input_callback(
        builder,
        session_id="s",
        context_factory=factory,
    )
    history = [
        {
            "type": "reasoning",
            "id": "rs_1",
            "summary": [],
            "encrypted_content": "opaque",
        },
        {
            "type": "message",
            "id": "msg_1",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": "answer"}],
        },
    ]
    new_input = [{"role": "user", "content": "next"}]

    prepared = await callback(history, new_input)

    assert prepared == [*history, *new_input]
