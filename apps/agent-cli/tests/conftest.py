"""Фикстуры для тестов protoprompt_cli."""

from __future__ import annotations

import pytest

from _mocks import FakeReader, FakeWriter, MockLLM

from protoprompt.agent import WorkingMemory

from protoprompt_cli.core import AgentCore
from protoprompt_cli.tools import ToolRunner


@pytest.fixture
def mem_factory():
    def make(max_tokens=400, store=None, llm=None, **kw):
        kw.setdefault("namespace", "test")
        return WorkingMemory(max_tokens=max_tokens, store=store, llm=llm, **kw)

    return make


@pytest.fixture
def core_factory():
    def make(
        mem,
        llm=None,
        root=None,
        tools=None,
        system_prompt="Ты — тестовый агент.",
        **kw,
    ):
        llm = llm or MockLLM()
        tools = tools or ToolRunner(root or ".")
        return AgentCore(mem, llm, tools, system_prompt=system_prompt, **kw)

    return make


@pytest.fixture
def repl_parts(mem_factory):
    def make(lines, **mem_kw):
        mem = mem_factory(**mem_kw)
        tools = ToolRunner(".")
        llm = MockLLM()
        core = AgentCore(mem, llm, tools, system_prompt="Ты — тестовый агент.")
        writer = FakeWriter()
        from protoprompt_cli.repl import Repl

        repl = Repl(core, mem, tools, root=".", write=writer,
                    readline=FakeReader(list(lines)))
        return repl, mem, writer, core

    return make