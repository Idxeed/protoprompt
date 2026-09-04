"""End-to-end boundaries for the optional local task-resume demo.

The trusted host bridge has its own focused unit tests.  These tests keep the
reference application's integration narrow: a browser only gets normal chat
and PDF-RAG routes, while the application-owned host is responsible for the
one seeded task episode and its cleanup.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from pathlib import Path
import sqlite3
from typing import Any

import httpx
import pytest

import protoprompt_ollama_chat.app as app_module
from protoprompt_ollama_chat.app import (
    Runtime,
    RuntimeConfig,
    _load_task_resume_demo_seed,
    create_app,
)
from protoprompt_ollama_chat.task_resume_demo import (
    TaskResumeDemoHost,
    TaskResumeDemoSeed,
)


class FakeOllama:
    """Offline embedding/chat double matching the app's normal test shape."""

    def __init__(self, **_: Any) -> None:
        self.chat_calls: list[dict[str, Any]] = []
        self.embed_calls: list[list[str]] = []

    async def embed(self, texts: list[str], model: str = "") -> list[list[float]]:
        self.embed_calls.append(list(texts))
        vectors: list[list[float]] = []
        for text in texts:
            lowered = text.lower()
            vectors.append([
                1.0 if "alpha" in lowered else 0.0,
                1.0 if "beta" in lowered else 0.0,
                1.0 if "gamma" in lowered else 0.0,
                1.0
                if not any(word in lowered for word in ("alpha", "beta", "gamma"))
                else 0.0,
            ])
        return vectors

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        model: str = "",
        *,
        on_token: Any = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> str:
        self.chat_calls.append({
            "messages": messages,
            "model": model,
            "max_tokens": max_tokens,
            "num_ctx": kwargs.get("num_ctx"),
        })
        answer = "локальный ответ"
        if on_token is not None:
            callback_result = on_token(answer)
            if inspect.isawaitable(callback_result):
                await callback_result
        return answer


class QueuedFakeOllama(FakeOllama):
    """Blocks the first generation to observe the demo's global queue."""

    def __init__(self) -> None:
        super().__init__()
        self.first_started = asyncio.Event()
        self.release_first = asyncio.Event()
        self.active_generations = 0
        self.max_active_generations = 0
        self._calls_started = 0

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        model: str = "",
        *,
        on_token: Any = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ) -> str:
        self.chat_calls.append({
            "messages": messages,
            "model": model,
            "max_tokens": max_tokens,
            "num_ctx": kwargs.get("num_ctx"),
        })
        self._calls_started += 1
        self.active_generations += 1
        self.max_active_generations = max(
            self.max_active_generations,
            self.active_generations,
        )
        try:
            if self._calls_started == 1:
                self.first_started.set()
                await self.release_first.wait()
            answer = "queued local answer"
            if on_token is not None:
                callback_result = on_token(answer)
                if inspect.isawaitable(callback_result):
                    await callback_result
            return answer
        finally:
            self.active_generations -= 1


def _config(**overrides: Any) -> RuntimeConfig:
    values: dict[str, Any] = {
        "ollama_host": "http://127.0.0.1:11434",
        "chat_model": "fake-chat",
        "embed_model": "fake-embed",
        "request_max_tokens": 512,
        "output_reserve_tokens": 64,
        "history_messages": 1,
        "memory_interval": 2,
        "memory_message_chars": 500,
        "max_upload_bytes": 1_024_000,
    }
    values.update(overrides)
    return RuntimeConfig(**values)


def _seed(conversation_id: str = "task-resume-demo") -> TaskResumeDemoSeed:
    return TaskResumeDemoSeed(
        conversation_id=conversation_id,
        task_descriptor="TASK_DESCRIPTOR_MUST_NOT_REACH_PROVIDER",
        goal="SAFE_TASK_GOAL: confirm the approved local launch state",
        completed_action_refs=("COMPLETED_ACTION_REF_MUST_NOT_REACH_PROVIDER",),
        outcome="interrupted",
        next_action="SAFE_NEXT_ACTION: inspect the local service status",
        lesson="SAFE_LESSON: use only bounded host-confirmed context",
    )


def _sse_payload(response: httpx.Response, event_name: str) -> Any:
    prefix = f"event: {event_name}\ndata: "
    for frame in response.text.split("\n\n"):
        if frame.startswith(prefix):
            return json.loads(frame[len(prefix):])
    raise AssertionError(f"missing SSE event {event_name!r}: {response.text!r}")


def _binding_identifiers(path: Path, conversation_id: str) -> tuple[str, str]:
    """Read host state only to assert that it was never sent to the browser."""

    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT task_ref, checkpoint_id "
            "FROM ollama_chat_task_resume_bindings WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
    assert row is not None
    return str(row[0]), str(row[1])


def _binding_count(path: Path, conversation_id: str) -> int:
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT COUNT(*) FROM ollama_chat_task_resume_bindings "
            "WHERE conversation_id = ?",
            (conversation_id,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_private_seed_parser_is_strict_and_rejects_boolean_schema_versions(
    tmp_path: Path,
) -> None:
    path = tmp_path / "seed.json"
    payload = {
        "schema_version": 1,
        "conversation_id": "seed-parser-demo",
        "task_descriptor": "host-only descriptor",
        "goal": "safe visible goal",
        "completed_action_refs": ["action:host-only"],
        "outcome": "interrupted",
        "next_action": "safe visible next action",
        "lesson": "safe visible lesson",
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    seed = _load_task_resume_demo_seed(path)
    assert seed.conversation_id == "seed-parser-demo"
    assert seed.completed_action_refs == ("action:host-only",)

    payload["schema_version"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="schema_version"):
        _load_task_resume_demo_seed(path)

    payload["schema_version"] = 1
    payload["task_ref"] = "browser-unsupported"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="fields"):
        _load_task_resume_demo_seed(path)

    payload.pop("task_ref")
    payload["conversation_id"] = "not valid for browser"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="conversation_id"):
        _load_task_resume_demo_seed(path)

    payload["conversation_id"] = "seed-parser-demo"
    payload["completed_action_refs"] = ["action:host-only", "action:host-only"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="field values"):
        _load_task_resume_demo_seed(path)


def _make_demo_app(
    tmp_path: Path,
    *,
    seed: TaskResumeDemoSeed,
    llm: FakeOllama,
    config: RuntimeConfig | None = None,
) -> tuple[Any, dict[str, Runtime]]:
    holder: dict[str, Runtime] = {}

    def factory(root: Path) -> Runtime:
        runtime = Runtime(
            root,
            config=config or _config(),
            llm=llm,
            task_resume_demo_seed=seed,
        )
        holder["runtime"] = runtime
        return runtime

    return create_app(tmp_path, runtime_factory=factory), holder


async def test_seeded_demo_chat_keeps_live_pdf_rag_and_never_leaks_host_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The task projection is additive to current PDF evidence, not a new API."""

    seed = _seed()
    llm = FakeOllama()
    local_secret = b"s" * 32
    monkeypatch.setattr(
        app_module.secrets,
        "token_bytes",
        lambda size: local_secret if size == len(local_secret) else b"x" * size,
    )
    app, holder = _make_demo_app(
        tmp_path,
        seed=seed,
        llm=llm,
        config=_config(request_max_tokens=4_096),
    )

    async with app.router.lifespan_context(app):
        runtime = holder["runtime"]
        assert runtime.repository.has_conversation(seed.conversation_id)
        assert runtime.request_max_tokens == 2_048
        assert (tmp_path / "task-resume-ledger.db").is_file()
        assert (tmp_path / "task-resume-ledger.db") != runtime.chat_db_path
        assert (tmp_path / "task-resume-ledger.db") != runtime.memory_db_path
        task_ref, checkpoint_id = _binding_identifiers(
            runtime.chat_db_path, seed.conversation_id
        )

        # The secret is local host material rather than a Ledger/state column.
        assert local_secret not in runtime.chat_db_path.read_bytes()
        assert local_secret not in runtime.memory_db_path.read_bytes()
        assert local_secret not in (tmp_path / "task-resume-ledger.db").read_bytes()
        secret_files = [
            item
            for item in tmp_path.iterdir()
            if item.is_file()
            and item not in {
                runtime.chat_db_path,
                runtime.memory_db_path,
                tmp_path / "task-resume-ledger.db",
            }
        ]
        assert any(local_secret in item.read_bytes() for item in secret_files)

        document_id = "pdf-live-task-demo"
        await runtime.store.add(
            document_id,
            ["LIVE_PDF_EVIDENCE_ALPHA: the current PDF is still available"],
            [[1.0, 0.0, 0.0, 0.0]],
            {"name": "live-evidence.pdf", "kind": "document"},
        )
        runtime.repository.add_document(
            document_id, "live-evidence.pdf", "live-evidence.pdf", 1
        )

        # This is intentionally ready and highly relevant.  The active demo
        # must not retrieve it, nor later archive the current transcript.
        old_memory_id = f"conversation-memory-{seed.conversation_id}-1-2"
        runtime.repository.append_message(
            seed.conversation_id, "user", "old ordinary transcript"
        )
        runtime.repository.append_message(
            seed.conversation_id, "assistant", "old ordinary reply"
        )
        runtime.repository.reserve_memory_segment(
            old_memory_id, seed.conversation_id, 1, 2
        )
        await runtime.store.add(
            old_memory_id,
            ["ARCHIVED_MEMORY_MUST_NOT_REACH_ACTIVE_TASK_RESUME_CHAT alpha"],
            [[1.0, 0.0, 0.0, 0.0]],
            {"chat_id": seed.conversation_id, "kind": "conversation_memory"},
        )
        runtime.repository.mark_memory_segment_ready(old_memory_id)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1"
        ) as client:
            response = await client.post(
                "/api/chat",
                json={
                    "conversation_id": seed.conversation_id,
                    "message": "alpha: what does the live PDF say?",
                    "model": "fake-model",
                },
            )

        assert response.status_code == 200, response.text
        sources = _sse_payload(response, "sources")
        context = _sse_payload(response, "context")
        assert [source["document_id"] for source in sources] == [document_id]
        assert context["rag_block_count"] == 1
        assert context["memory_block_count"] == 0
        # One manually-created archive remains, but the successful turn did
        # not create a second transcript-memory projection.
        assert runtime.repository.memory_document_ids(seed.conversation_id) == [
            old_memory_id
        ]

        provider_payload = json.dumps(
            llm.chat_calls[-1]["messages"], ensure_ascii=False, sort_keys=True
        )
        forbidden = (
            task_ref,
            checkpoint_id,
            seed.task_descriptor,
            seed.completed_action_refs[0],
            f"task-resume-demo-source:{task_ref}",
            "ARCHIVED_MEMORY_MUST_NOT_REACH_ACTIVE_TASK_RESUME_CHAT",
        )
        assert all(value not in provider_payload for value in forbidden)
        assert "LIVE_PDF_EVIDENCE_ALPHA" in provider_payload
        assert seed.goal in provider_payload
        assert seed.next_action in provider_payload
        assert seed.lesson in provider_payload
        assert llm.chat_calls[-1]["num_ctx"] == 2_048
        # SSE is also browser data, so it must not become a side channel for
        # host task IDs, checkpoints, descriptors, or source references.
        assert all(value not in response.text for value in forbidden[:-1])


async def test_seeded_demo_exposes_no_browser_control_plane_and_rejects_extra_fields(
    tmp_path: Path,
) -> None:
    seed = _seed()
    llm = FakeOllama()
    app, holder = _make_demo_app(tmp_path, seed=seed, llm=llm)

    paths = {route.path for route in app.routes}
    assert not any("task" in path.casefold() for path in paths)

    async with app.router.lifespan_context(app):
        assert holder["runtime"].repository.has_conversation(seed.conversation_id)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1"
        ) as client:
            response = await client.post(
                "/api/chat",
                json={
                    "conversation_id": seed.conversation_id,
                    "message": "alpha normal browser request",
                    "task_ref": "browser-must-not-control-task",
                    "checkpoint_id": "browser-must-not-control-checkpoint",
                    "task_descriptor": "browser-must-not-change-descriptor",
                },
            )
            no_route = await client.post("/api/task-resume/seed", json={})

    assert response.status_code == 422
    assert no_route.status_code == 404
    assert llm.chat_calls == []


async def test_demo_conversation_delete_closes_host_mapping_before_transcript_delete(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seed = _seed()
    llm = FakeOllama()
    app, holder = _make_demo_app(tmp_path, seed=seed, llm=llm)
    observed: list[tuple[str, bool]] = []
    original_close = TaskResumeDemoHost.close_binding

    def checked_close(self: TaskResumeDemoHost, conversation_id: str):
        runtime = holder["runtime"]
        observed.append(
            (conversation_id, runtime.repository.has_conversation(conversation_id))
        )
        return original_close(self, conversation_id)

    monkeypatch.setattr(TaskResumeDemoHost, "close_binding", checked_close)
    async with app.router.lifespan_context(app):
        runtime = holder["runtime"]
        assert _binding_count(runtime.chat_db_path, seed.conversation_id) == 1
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1"
        ) as client:
            response = await client.delete(
                f"/api/conversations/{seed.conversation_id}"
            )

        assert response.status_code == 204, response.text
        assert observed == [(seed.conversation_id, True)]
        assert not runtime.repository.has_conversation(seed.conversation_id)
        assert _binding_count(runtime.chat_db_path, seed.conversation_id) == 0


def test_demo_is_rejected_for_remote_ollama_or_nonlocal_browser_hosts(
    tmp_path: Path,
) -> None:
    seed = _seed()

    with pytest.raises(ValueError, match="local|loopback"):
        Runtime(
            tmp_path / "remote-runtime",
            config=_config(ollama_host="https://ollama.example.test"),
            llm=FakeOllama(),
            task_resume_demo_seed=seed,
        )

    with pytest.raises(ValueError, match="local|loopback"):
        create_app(
            tmp_path / "network-app",
            task_resume_demo_seed=seed,
            allowed_hosts=("demo.example.test",),
        )

    with pytest.raises(ValueError, match="OUTPUT_RESERVE below 2048"):
        Runtime(
            tmp_path / "reserve-runtime",
            config=_config(request_max_tokens=4_096, output_reserve_tokens=2_048),
            llm=FakeOllama(),
            task_resume_demo_seed=seed,
        )


async def test_demo_serializes_all_local_model_generations_through_one_queue(
    tmp_path: Path,
) -> None:
    """The demo-safe profile permits one active generation across dialogs."""

    seed = _seed()
    llm = QueuedFakeOllama()
    app, holder = _make_demo_app(tmp_path, seed=seed, llm=llm)
    async with app.router.lifespan_context(app):
        runtime = holder["runtime"]
        ordinary = runtime.repository.create_conversation("ordinary-demo-chat")
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1"
        ) as client:
            first = asyncio.create_task(client.post(
                "/api/chat",
                json={
                    "conversation_id": seed.conversation_id,
                    "message": "alpha first task demo request",
                },
            ))
            await llm.first_started.wait()
            second = asyncio.create_task(client.post(
                "/api/chat",
                json={
                    "conversation_id": ordinary["id"],
                    "message": "beta ordinary request queued behind demo",
                },
            ))
            await asyncio.sleep(0)
            assert llm.max_active_generations == 1
            assert len(llm.chat_calls) == 1
            llm.release_first.set()
            first_response, second_response = await asyncio.gather(first, second)

    assert first_response.status_code == 200, first_response.text
    assert second_response.status_code == 200, second_response.text
    assert llm.max_active_generations == 1
    assert len(llm.chat_calls) == 2


async def test_demo_rejects_a_nonloopback_peer_even_with_a_spoofed_host_header(
    tmp_path: Path,
) -> None:
    """Programmatic ASGI hosts cannot bypass the demo's loopback boundary."""

    seed = _seed()
    app, _ = _make_demo_app(tmp_path, seed=seed, llm=FakeOllama())
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app, client=("192.0.2.25", 43100))
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1"
        ) as client:
            response = await client.get("/api/health", headers={"Host": "127.0.0.1"})
    assert response.status_code == 403
