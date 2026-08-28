from __future__ import annotations

import pytest

from protoprompt import ContextBuilder, ContextInput
from protoprompt.profile.render import render
from protoprompt.profile.types import Preferences, Traits, UserProfile
from protoprompt.store.memory import InMemStore

from _mocks import MockLLM


def _profile() -> UserProfile:
    p = UserProfile(user_id="u1")
    p.traits = Traits(expertise="expert")
    p.preferences = Preferences(language="ru", topics=["ai"])
    p.facts = {"name": "Илья"}
    p.summary = "опытный"
    return p


def test_render_empty_profile():
    assert render(UserProfile(user_id="u1")) == ""


def test_render_contains_content_and_ru_header():
    text = render(_profile())
    assert text.startswith("Профиль пользователя:")
    assert "name: Илья" in text
    assert "expertise: expert" in text
    assert "language: ru" in text
    assert "topics: ai" in text
    assert "опытный" in text


def test_render_en_header():
    text = render(_profile(), language="en")
    assert text.startswith("User profile:")
    assert "Профиль пользователя" not in text


@pytest.mark.asyncio
async def test_context_builder_uses_structured_profile():
    builder = ContextBuilder(InMemStore(), MockLLM(embed_dim=2))
    out = await builder.build(ContextInput(
        query="q",
        system_prompt="sys",
        include_profile=True,
        profile=_profile(),
    ))
    assert out.profile_used is True
    assert "name: Илья" in out.system_prompt
    assert "Профиль пользователя:" in out.system_prompt


@pytest.mark.asyncio
async def test_context_builder_profile_text_still_works():
    builder = ContextBuilder(InMemStore(), MockLLM(embed_dim=2))
    out = await builder.build(ContextInput(
        query="q",
        system_prompt="sys",
        include_profile=True,
        profile_text="expert",
    ))
    assert out.profile_used is True
    assert "expert" in out.system_prompt


@pytest.mark.asyncio
async def test_session_header_localized():
    store = InMemStore()
    store.add("session_c1", ["hello"], [[0.5] * 2])
    builder = ContextBuilder(store, MockLLM(embed_dim=2))
    out = await builder.build(ContextInput(
        query="q",
        system_prompt="sys",
        chat_id="c1",
        language="en",
    ))
    assert "Conversation history" in out.system_prompt
    assert "История диалога" not in out.system_prompt
