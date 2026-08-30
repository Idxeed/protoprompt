from __future__ import annotations

import asyncio
import io
import inspect
import json
import os
from pathlib import Path
import sqlite3
import stat
from typing import Any

import httpx
import pytest

from protoprompt import RegexTokenCounter
from protoprompt_ollama_chat.app import (
    ConversationRepository,
    Runtime,
    RuntimeConfig,
    create_app,
)


class FakeOllama:
    """Offline embedding/chat double used by the reference-app tests."""

    def __init__(self) -> None:
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
                1.0 if not any(word in lowered for word in ("alpha", "beta", "gamma")) else 0.0,
            ])
        return vectors

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        model: str = "",
        *,
        on_token: Any = None,
        max_tokens: int | None = None,
        **_: Any,
    ) -> str:
        self.chat_calls.append({
            "messages": messages,
            "model": model,
            "max_tokens": max_tokens,
            "num_ctx": _.get("num_ctx"),
        })
        parts = ["локальный ", "ответ"]
        for part in parts:
            if on_token is not None:
                callback_result = on_token(part)
                if inspect.isawaitable(callback_result):
                    await callback_result
        return "".join(parts)


class PartialFailureOllama(FakeOllama):
    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        model: str = "",
        *,
        on_token: Any = None,
        max_tokens: int | None = None,
        **_: Any,
    ) -> str:
        self.chat_calls.append({"messages": messages, "model": model, "max_tokens": max_tokens})
        if on_token is not None:
            callback_result = on_token("partial answer")
            if inspect.isawaitable(callback_result):
                await callback_result
        raise RuntimeError("stream interrupted")


class BlockingEmbedOllama(FakeOllama):
    """Blocks exactly one context-planning embed call for cancellation tests."""

    def __init__(self) -> None:
        super().__init__()
        self.embedding_started = asyncio.Event()
        self._block_once = True

    async def embed(self, texts: list[str], model: str = "") -> list[list[float]]:
        if self._block_once:
            self._block_once = False
            self.embedding_started.set()
            await asyncio.Event().wait()
        return await super().embed(texts, model=model)


def _text_pdf_bytes(text: str) -> bytes:
    pypdf = pytest.importorskip("pypdf")
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=200, height=200)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({
            NameObject("/F1"): writer._add_object(font),
        }),
    })
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 20 100 Td ({text}) Tj ET".encode("ascii"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


def _config(**overrides: Any) -> RuntimeConfig:
    values: dict[str, Any] = {
        "ollama_host": "http://127.0.0.1:11434",
        "chat_model": "fake-chat",
        "embed_model": "fake-embed",
        "request_max_tokens": 384,
        "output_reserve_tokens": 64,
        "history_messages": 20,
        "memory_interval": 99,
        "memory_message_chars": 500,
        "max_upload_bytes": 1_024_000,
    }
    values.update(overrides)
    return RuntimeConfig(**values)


def _make_app(
    tmp_path: Path,
    config: RuntimeConfig | None = None,
    llm: FakeOllama | None = None,
) -> tuple[Any, FakeOllama, dict[str, Runtime]]:
    llm = llm or FakeOllama()
    holder: dict[str, Runtime] = {}

    def factory(root: Path) -> Runtime:
        runtime = Runtime(root, config=config or _config(), llm=llm)
        holder["runtime"] = runtime
        return runtime

    return create_app(tmp_path, runtime_factory=factory), llm, holder


@pytest.fixture
async def app_client(tmp_path: Path):
    app, llm, holder = _make_app(tmp_path)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            yield client, llm, holder["runtime"]


def _sse_payload(response: httpx.Response, event_name: str) -> Any:
    prefix = f"event: {event_name}\ndata: "
    for frame in response.text.split("\n\n"):
        if frame.startswith(prefix):
            return json.loads(frame[len(prefix):])
    raise AssertionError(f"missing SSE event {event_name!r}: {response.text!r}")


async def test_chat_uses_exact_bounded_plan_and_persists_answer(app_client: Any) -> None:
    client, llm, runtime = app_client
    conversation = await client.post("/api/conversations")
    conversation_id = conversation.json()["id"]

    response = await client.post("/api/chat", json={
        "conversation_id": conversation_id,
        "message": "Расскажи про alpha",
        "model": "fake-model",
    })

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    context = _sse_payload(response, "context")
    done = _sse_payload(response, "done")
    assert done == {"conversation_id": conversation_id, "completed": True}
    assert "event: token" in response.text
    receipt = context["receipt"]
    assert receipt["input_tokens"] + receipt["output_reserve_tokens"] <= receipt["max_tokens"]
    assert len(llm.chat_calls) == 1
    call = llm.chat_calls[0]
    assert call["model"] == "fake-model"
    assert call["max_tokens"] == receipt["output_reserve_tokens"]
    assert call["num_ctx"] == receipt["max_tokens"]
    assert RegexTokenCounter().count_messages(call["messages"]) == receipt["input_tokens"]
    assert call["messages"][-1] == {"role": "user", "content": "Расскажи про alpha"}

    history = await client.get(f"/api/conversations/{conversation_id}/messages")
    assert [item["content"] for item in history.json()["messages"]] == [
        "Расскажи про alpha",
        "локальный ответ",
    ]
    assert conversation_id not in runtime._turn_locks


async def test_oversized_message_is_rejected_before_embedding_or_transcript_write(
    tmp_path: Path,
) -> None:
    app, llm, _ = _make_app(
        tmp_path,
        _config(request_max_tokens=80, output_reserve_tokens=32),
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            conversation = await client.post("/api/conversations")
            conversation_id = conversation.json()["id"]
            response = await client.post("/api/chat", json={
                "conversation_id": conversation_id,
                "message": "token " * 200,
            })
            assert response.status_code == 413, response.text
            assert llm.embed_calls == []
            assert llm.chat_calls == []
            history = await client.get(f"/api/conversations/{conversation_id}/messages")
            assert history.json()["messages"] == []


async def test_chunked_oversized_chat_body_is_rejected_before_json_parsing(
    app_client: Any,
) -> None:
    client, llm, runtime = app_client
    conversation = await client.post("/api/conversations")
    conversation_id = conversation.json()["id"]

    async def chunked_body():
        yield (
            b'{"conversation_id":"' + conversation_id.encode("ascii")
            + b'","message":"'
        )
        for _ in range(5):
            yield b"x" * 70_000
        yield b'"}'

    response = await client.post(
        "/api/chat",
        content=chunked_body(),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413, response.text
    assert llm.embed_calls == []
    assert llm.chat_calls == []
    history = await client.get(f"/api/conversations/{conversation_id}/messages")
    assert history.json()["messages"] == []
    assert conversation_id not in runtime._turn_locks


async def test_cancelled_context_planning_releases_the_conversation_lock(
    tmp_path: Path,
) -> None:
    llm = BlockingEmbedOllama()
    app, _, holder = _make_app(tmp_path, llm=llm)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            conversation = await client.post("/api/conversations")
            conversation_id = conversation.json()["id"]
            pending = asyncio.create_task(client.post("/api/chat", json={
                "conversation_id": conversation_id,
                "message": "alpha cancelled before SSE starts",
            }))
            await asyncio.wait_for(llm.embedding_started.wait(), timeout=1)
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending

            # A subsequent turn should not wait on the cancelled request's
            # per-conversation lock.
            response = await asyncio.wait_for(
                client.post("/api/chat", json={
                    "conversation_id": conversation_id,
                    "message": "alpha after cancellation",
                }),
                timeout=1,
            )
            assert response.status_code == 200
            assert conversation_id not in holder["runtime"]._turn_locks


async def test_message_cursor_returns_an_oldest_to_newest_page(app_client: Any) -> None:
    client, _, runtime = app_client
    conversation = await client.post("/api/conversations")
    conversation_id = conversation.json()["id"]
    for number in range(1, 6):
        runtime.repository.append_message(conversation_id, "user", f"message {number}")

    newest = await client.get(
        f"/api/conversations/{conversation_id}/messages?limit=2"
    )
    newest_payload = newest.json()
    assert [item["content"] for item in newest_payload["messages"]] == [
        "message 4", "message 5"
    ]
    assert newest_payload["total"] == 5
    assert newest_payload["has_more"] is True
    assert newest_payload["next_before_id"] == newest_payload["messages"][0]["id"]

    older = await client.get(
        f"/api/conversations/{conversation_id}/messages?limit=2"
        f"&before_id={newest_payload['next_before_id']}"
    )
    older_payload = older.json()
    assert [item["content"] for item in older_payload["messages"]] == [
        "message 2", "message 3"
    ]
    assert older_payload["has_more"] is True

    oldest = await client.get(
        f"/api/conversations/{conversation_id}/messages?limit=2"
        f"&before_id={older_payload['next_before_id']}"
    )
    oldest_payload = oldest.json()
    assert [item["content"] for item in oldest_payload["messages"]] == ["message 1"]
    assert oldest_payload["has_more"] is False
    assert oldest_payload["next_before_id"] is None


async def test_archived_memory_survives_history_window_and_is_recalled(
    tmp_path: Path,
) -> None:
    app, llm, holder = _make_app(
        tmp_path,
        _config(history_messages=1, memory_interval=2),
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            conversation = await client.post("/api/conversations")
            conversation_id = conversation.json()["id"]
            first = await client.post("/api/chat", json={
                "conversation_id": conversation_id,
                "message": "alpha durable fact",
            })
            assert first.status_code == 200
            runtime = holder["runtime"]
            archive_ids = runtime.repository.memory_document_ids(conversation_id)
            assert len(archive_ids) == 1

            second = await client.post("/api/chat", json={
                "conversation_id": conversation_id,
                "message": "alpha question",
            })

            assert second.status_code == 200
            context = _sse_payload(second, "context")
            assert context["memory_block_count"] >= 1
            # The history input only had the previous assistant turn, so the
            # early fact can be present only through the durable archive.
            assert "alpha durable fact" in llm.chat_calls[-1]["messages"][0]["content"]


async def test_unanswered_user_turn_does_not_stall_memory_watermark(tmp_path: Path) -> None:
    app, _, holder = _make_app(tmp_path, _config(memory_interval=2))
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            conversation = await client.post("/api/conversations")
            conversation_id = conversation.json()["id"]
            runtime = holder["runtime"]
            # Simulate an interrupted prior request: its user message remains
            # in the durable transcript but there is no assistant answer.
            runtime.repository.append_message(conversation_id, "user", "alpha interrupted")

            response = await client.post("/api/chat", json={
                "conversation_id": conversation_id,
                "message": "beta next turn",
            })

            assert response.status_code == 200
            archive_ids = runtime.repository.memory_document_ids(conversation_id)
            assert len(archive_ids) == 1
            assert runtime.vector_store_sync.get(archive_ids[0]) is not None


async def test_invalid_pdf_leaves_no_upload_or_vector_rows(app_client: Any) -> None:
    client, _, runtime = app_client
    response = await client.post(
        "/api/documents",
        files={"file": ("not-a-pdf.pdf", b"plain text", "application/pdf")},
    )

    assert response.status_code == 422
    assert list(runtime.upload_dir.iterdir()) == []
    assert await runtime.store.count() == 0


async def test_text_pdf_is_indexed_and_retrieved_in_a_bounded_chat(
    app_client: Any,
) -> None:
    client, llm, runtime = app_client
    upload = await client.post(
        "/api/documents",
        files={"file": ("evidence.pdf", _text_pdf_bytes("alpha evidence"), "application/pdf")},
    )

    assert upload.status_code == 200, upload.text
    document = upload.json()
    assert document["chunks"] >= 1
    assert runtime.repository.get_document(document["id"])["status"] == "ready"
    assert runtime.upload_path(
        runtime.repository.get_document(document["id"])["storage_name"]
    ).is_file()

    conversation = await client.post("/api/conversations")
    response = await client.post("/api/chat", json={
        "conversation_id": conversation.json()["id"],
        "message": "alpha question",
    })

    assert response.status_code == 200
    assert _sse_payload(response, "context")["rag_block_count"] == 1
    assert _sse_payload(response, "sources")[0]["document_id"] == document["id"]
    assert "alpha evidence" in llm.chat_calls[-1]["messages"][0]["content"]


async def test_malformed_pdf_is_rejected_and_leaves_no_upload_or_vector_rows(
    app_client: Any,
) -> None:
    client, _, runtime = app_client
    response = await client.post(
        "/api/documents",
        files={"file": ("truncated.pdf", b"%PDF-1.7\ntruncated", "application/pdf")},
    )

    assert response.status_code == 422
    assert list(runtime.upload_dir.iterdir()) == []
    assert await runtime.store.count() == 0


async def test_chunked_oversized_pdf_is_rejected_before_multipart_spooling(
    tmp_path: Path,
) -> None:
    app, _, holder = _make_app(tmp_path, _config(max_upload_bytes=64))

    async def chunked_body():
        boundary = b"--proto-boundary\r\n"
        headers = (
            b'Content-Disposition: form-data; name="file"; filename="large.pdf"\r\n'
            b"Content-Type: application/pdf\r\n\r\n"
        )
        yield boundary + headers + b"%PDF-1.7\n"
        for _ in range(5):
            yield b"x" * 70_000
        yield b"\r\n--proto-boundary--\r\n"

    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.post(
                "/api/documents",
                content=chunked_body(),
                headers={"Content-Type": "multipart/form-data; boundary=proto-boundary"},
            )
            runtime = holder["runtime"]
            assert response.status_code == 413, response.text
            assert list(runtime.upload_dir.iterdir()) == []
            assert await runtime.store.count() == 0


async def test_pending_pdf_is_never_retrieved_before_it_is_ready(app_client: Any) -> None:
    client, llm, runtime = app_client
    document_id = "pdf-crash-left"
    runtime.repository.reserve_document(document_id, "crash.pdf", "crash.pdf")
    await runtime.store.add(
        document_id,
        ["alpha unlisted PDF content"],
        [[1.0, 0.0, 0.0, 0.0]],
        {"name": "crash.pdf", "kind": "document"},
    )
    conversation = await client.post("/api/conversations")
    response = await client.post("/api/chat", json={
        "conversation_id": conversation.json()["id"],
        "message": "alpha question",
    })

    assert response.status_code == 200
    assert _sse_payload(response, "context")["rag_block_count"] == 0
    assert "alpha unlisted PDF content" not in llm.chat_calls[-1]["messages"][0]["content"]


async def test_startup_reconciles_a_crash_left_pending_pdf(tmp_path: Path) -> None:
    runtime = Runtime(tmp_path, config=_config(), llm=FakeOllama())
    document_id = "pdf-pending-startup"
    storage_name = "pdf-pending-startup-recover.pdf"
    upload = runtime.upload_path(storage_name)
    upload.write_bytes(b"%PDF-1.7\ncrash-left")
    runtime.repository.reserve_document(document_id, "recover.pdf", storage_name)
    await runtime.store.add(
        document_id,
        ["alpha crash projection"],
        [[1.0, 0.0, 0.0, 0.0]],
        {"name": "recover.pdf", "kind": "document"},
    )
    await runtime.store.add(
        "pdf-untracked-legacy-crash",
        ["alpha legacy orphan"],
        [[1.0, 0.0, 0.0, 0.0]],
        {"name": "lost.pdf", "kind": "document"},
    )
    await runtime.store.add(
        "session-deleted-legacy",
        ["alpha stale legacy session"],
        [[1.0, 0.0, 0.0, 0.0]],
        {"kind": "session"},
    )
    await runtime.close()

    app, _, holder = _make_app(tmp_path)
    async with app.router.lifespan_context(app):
        recovered = holder["runtime"]
        assert recovered.repository.get_document(document_id) is None
        assert not upload.exists()
        assert await recovered.store.count() == 0


async def test_pending_memory_segment_is_not_recalled_until_ready(app_client: Any) -> None:
    client, llm, runtime = app_client
    conversation = await client.post("/api/conversations")
    conversation_id = conversation.json()["id"]
    runtime.repository.append_message(conversation_id, "user", "alpha original")
    pending_id = f"conversation-memory-{conversation_id}-1-1"
    runtime.repository.reserve_memory_segment(pending_id, conversation_id, 1, 1)
    await runtime.store.add(
        pending_id,
        ["alpha pending archive content"],
        [[1.0, 0.0, 0.0, 0.0]],
        {"chat_id": conversation_id, "kind": "conversation_memory"},
    )

    response = await client.post("/api/chat", json={
        "conversation_id": conversation_id,
        "message": "alpha follow-up",
    })

    assert response.status_code == 200
    assert _sse_payload(response, "context")["memory_block_count"] == 0
    assert "alpha pending archive content" not in llm.chat_calls[-1]["messages"][0]["content"]


async def test_partial_stream_failure_is_not_saved_as_an_assistant_answer(
    tmp_path: Path,
) -> None:
    app, _, holder = _make_app(
        tmp_path,
        _config(memory_interval=2),
        PartialFailureOllama(),
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            conversation = await client.post("/api/conversations")
            conversation_id = conversation.json()["id"]
            response = await client.post("/api/chat", json={
                "conversation_id": conversation_id,
                "message": "alpha should not get a partial answer",
            })

            assert response.status_code == 200
            assert _sse_payload(response, "done")["completed"] is False
            assert "Ollama не смогла завершить ответ" in response.text
            history = await client.get(f"/api/conversations/{conversation_id}/messages")
            assert [item["role"] for item in history.json()["messages"]] == ["user"]
            assert holder["runtime"].repository.memory_document_ids(conversation_id) == []


async def test_document_delete_removes_upload_and_index(app_client: Any) -> None:
    client, _, runtime = app_client
    document_id = "pdf-test"
    storage_name = "pdf-test-knowledge.pdf"
    upload = runtime.upload_path(storage_name)
    upload.write_bytes(b"%PDF-1.7\nplaceholder")
    await runtime.store.add(
        document_id,
        ["alpha evidence"],
        [[1.0, 0.0, 0.0, 0.0]],
        {"name": "knowledge.pdf", "kind": "document"},
    )
    runtime.repository.add_document(document_id, "knowledge.pdf", storage_name, 1)

    response = await client.delete(f"/api/documents/{document_id}")

    assert response.status_code == 204
    assert runtime.repository.get_document(document_id) is None
    assert not upload.exists()
    assert await runtime.store.count() == 0


async def test_conversation_delete_removes_session_memory(app_client: Any) -> None:
    client, _, runtime = app_client
    conversation = await client.post("/api/conversations")
    conversation_id = conversation.json()["id"]
    runtime.repository.append_message(conversation_id, "user", "alpha")
    await runtime.store.add(
        f"session_{conversation_id}",
        ["alpha memory"],
        [[1.0, 0.0, 0.0, 0.0]],
        {"chat_id": conversation_id, "kind": "session"},
    )
    await runtime.store.add(
        f"session_{conversation_id}_new",
        ["alpha interrupted legacy memory"],
        [[1.0, 0.0, 0.0, 0.0]],
        {"chat_id": conversation_id, "kind": "session"},
    )
    archive_id = f"conversation-memory-{conversation_id}-1-2"
    await runtime.store.add(
        archive_id,
        ["alpha durable memory"],
        [[1.0, 0.0, 0.0, 0.0]],
        {"chat_id": conversation_id, "kind": "conversation_memory"},
    )
    runtime.repository.add_memory_segment(archive_id, conversation_id, 1, 2)
    pending_archive_id = f"conversation-memory-{conversation_id}-3-4"
    runtime.repository.reserve_memory_segment(
        pending_archive_id, conversation_id, 3, 4
    )
    await runtime.store.add(
        pending_archive_id,
        ["alpha pending durable memory"],
        [[1.0, 0.0, 0.0, 0.0]],
        {"chat_id": conversation_id, "kind": "conversation_memory"},
    )
    await runtime.store.add(
        "conversation-memory-crash-untracked",
        ["alpha untracked durable memory"],
        [[1.0, 0.0, 0.0, 0.0]],
        {"chat_id": conversation_id, "kind": "conversation_memory"},
    )
    await runtime.store.add(
        "unexpected-legacy-session-id",
        ["alpha untracked legacy session"],
        [[1.0, 0.0, 0.0, 0.0]],
        {"chat_id": conversation_id, "kind": "session"},
    )

    response = await client.delete(f"/api/conversations/{conversation_id}")

    assert response.status_code == 204
    assert conversation_id not in {
        item["id"] for item in runtime.repository.list_conversations()
    }
    assert await runtime.store.count() == 0
    assert conversation_id not in runtime._turn_locks


async def test_deleted_conversation_cannot_be_resurrected_by_a_chat_request(
    app_client: Any,
) -> None:
    client, _, runtime = app_client
    conversation = await client.post("/api/conversations")
    conversation_id = conversation.json()["id"]
    assert (await client.delete(f"/api/conversations/{conversation_id}")).status_code == 204

    response = await client.post("/api/chat", json={
        "conversation_id": conversation_id,
        "message": "do not recreate this dialog",
    })

    assert response.status_code == 404
    assert not runtime.repository.has_conversation(conversation_id)


def test_legacy_database_migrates_storage_name_and_deletes_messages(tmp_path: Path) -> None:
    uploads = tmp_path / "uploads"
    uploads.mkdir()
    expected_storage_name = "old-pdf-old.pdf"
    (uploads / expected_storage_name).write_bytes(b"%PDF-1.7\nlegacy")
    path = tmp_path / "chat.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE conversations (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY(conversation_id) REFERENCES conversations(id)
        );
        CREATE TABLE documents (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            chunks INTEGER NOT NULL,
            uploaded_at TEXT NOT NULL
        );
        INSERT INTO conversations VALUES ('legacy', 'Legacy', 'now', 'now');
        INSERT INTO messages(conversation_id, role, content, created_at)
            VALUES ('legacy', 'user', 'kept locally', 'now');
        INSERT INTO documents VALUES ('old-pdf', 'old.pdf', 1, 'now');
        """
    )
    connection.commit()
    connection.close()

    repository = ConversationRepository(path)
    try:
        assert repository.get_document("old-pdf")["storage_name"] == expected_storage_name
        assert repository.delete_conversation("legacy") is True
        assert repository.get_messages("legacy")[0] == []
    finally:
        repository.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions are not available on Windows")
async def test_local_data_paths_are_owner_private_on_posix(tmp_path: Path) -> None:
    app, _, holder = _make_app(tmp_path)
    async with app.router.lifespan_context(app):
        runtime = holder["runtime"]
        assert stat.S_IMODE(runtime.data_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(runtime.upload_dir.stat().st_mode) == 0o700
        assert stat.S_IMODE(runtime.chat_db_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(runtime.memory_db_path.stat().st_mode) == 0o600
        upload = runtime.upload_path("private.pdf")
        with runtime.open_private_upload(upload) as output:
            output.write(b"%PDF-1.7\nprivate")
        assert stat.S_IMODE(upload.stat().st_mode) == 0o600


def test_remote_ollama_requires_explicit_opt_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OLLAMA_HOST", "http://192.0.2.10:11434")
    monkeypatch.delenv("OLLAMA_CHAT_ALLOW_REMOTE", raising=False)
    with pytest.raises(ValueError, match="not loopback"):
        RuntimeConfig.from_env()
    monkeypatch.setenv("OLLAMA_CHAT_ALLOW_REMOTE", "1")
    assert RuntimeConfig.from_env().ollama_host == "http://192.0.2.10:11434"


async def test_health_marks_explicit_remote_ollama_mode(tmp_path: Path) -> None:
    app, _, _ = _make_app(
        tmp_path,
        _config(ollama_host="https://ollama.example.test", request_max_tokens=384),
    )
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            response = await client.get("/api/health")
    assert response.json()["mode"] == "remote"
