"""LangGraph thread history plus a scoped cross-thread user profile."""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.runtime import Runtime
from langgraph.store.memory import InMemoryStore

from protoprompt import ContextBuilder, ContextInput, InMemStore, MemoryScope
from protoprompt.integrations import (
    ProtoPromptStoreAdapter,
    create_sync_build_context_node,
)
from protoprompt.profile.store import profile_from_dict, profile_to_dict
from protoprompt.profile.types import Preferences, UserProfile


class ZeroEmbeddings:
    async def embed(self, texts: list[str], model: str = "") -> list[list[float]]:
        return [[0.0] * 8 for _ in texts]


class State(TypedDict, total=False):
    messages: Annotated[list, add_messages]
    profile: dict[str, Any]
    context: str
    context_provenance: dict[str, Any]


def main() -> None:
    scope = MemoryScope(tenant="demo", user="alice", kind="assistant")
    long_term = ProtoPromptStoreAdapter(InMemoryStore(), scope=scope)
    profile = UserProfile(
        user_id="alice",
        preferences=Preferences(language="ru", format="bullets"),
        facts={"role": "lawyer"},
    )
    long_term.put(("profile",), "current", profile_to_dict(profile))

    def load_profile(state: State, runtime: Runtime) -> dict[str, Any]:
        item = runtime.store.get(("profile",), "current")
        return {"profile": item.value if item is not None else {}}

    def context_input(state: State, query: str) -> ContextInput:
        current = profile_from_dict(state["profile"])
        return ContextInput(
            query=query,
            system_prompt="Follow the durable user profile.",
            include_rag=False,
            include_session=False,
            include_profile=True,
            profile=current,
            language="en",
        )

    builder = ContextBuilder(InMemStore(), ZeroEmbeddings(), scope=scope)
    build_context = create_sync_build_context_node(
        builder,
        input_factory=context_input,
    )

    def respond(state: State) -> dict[str, Any]:
        history_size = len(state.get("messages", []))
        return {"messages": [{
            "role": "assistant",
            "content": f"thread history items before reply: {history_size}",
        }]}

    graph = StateGraph(State)
    graph.add_node("load_profile", load_profile)
    graph.add_node("build_context", build_context)
    graph.add_node("respond", respond)
    graph.add_edge(START, "load_profile")
    graph.add_edge("load_profile", "build_context")
    graph.add_edge("build_context", "respond")
    graph.add_edge("respond", END)
    app = graph.compile(checkpointer=InMemorySaver(), store=long_term)

    thread_a = {"configurable": {"thread_id": "legal-case"}}
    thread_b = {"configurable": {"thread_id": "personal-chat"}}
    first = app.invoke(
        {"messages": [{"role": "user", "content": "Review the contract"}]},
        thread_a,
    )
    second = app.invoke(
        {"messages": [{"role": "user", "content": "Check clause 7"}]},
        thread_a,
    )
    other = app.invoke(
        {"messages": [{"role": "user", "content": "Hello from another thread"}]},
        thread_b,
    )

    print("thread A items after first turn:", len(first["messages"]))
    print("thread A items after second turn:", len(second["messages"]))
    print("thread B items after first turn:", len(other["messages"]))
    print("same cross-thread profile in A:", "role: lawyer" in second["context"])
    print("same cross-thread profile in B:", "role: lawyer" in other["context"])


if __name__ == "__main__":
    main()
