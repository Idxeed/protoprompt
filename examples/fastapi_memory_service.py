"""Authenticated FastAPI memory service with a host-controlled fixed scope."""

from __future__ import annotations

from contextlib import asynccontextmanager
import hashlib
import os
import secrets

from protoprompt import MemoryScope, MemoryService, SqliteStore, as_async
from protoprompt.integrations import create_fastapi_memory_app


class DeterministicEmbeddings:
    """Offline demo embeddings. Use a real provider in production."""

    async def embed(self, texts: list[str], model: str = "") -> list[list[float]]:
        return [
            [byte / 255.0 for byte in hashlib.sha256(text.encode()).digest()]
            for text in texts
        ]


def build_app():
    api_key = os.environ.get("PROTOPROMPT_API_KEY")
    if not api_key:
        raise RuntimeError("set PROTOPROMPT_API_KEY before starting the service")
    scope = MemoryScope(
        tenant=os.environ.get("PROTOPROMPT_TENANT", "demo"),
        user=os.environ.get("PROTOPROMPT_USER", "demo-user"),
        thread=os.environ.get("PROTOPROMPT_THREAD", "api"),
    )
    database = SqliteStore(os.environ.get("PROTOPROMPT_DB", "protoprompt-api.db"))
    service = MemoryService(as_async(database), DeterministicEmbeddings(), scope)

    async def authorize(request):
        supplied = request.headers.get("authorization", "").removeprefix("Bearer ")
        if not supplied or not secrets.compare_digest(supplied, api_key):
            from fastapi import HTTPException

            raise HTTPException(status_code=401, detail="invalid bearer token")
        request.state.memory_scope = scope

    def resolve_scope(request):
        # Production apps normally derive this from verified JWT claims.
        return request.state.memory_scope

    def service_factory(request_scope):
        if request_scope != scope:
            raise RuntimeError("unexpected scope")
        return service

    @asynccontextmanager
    async def lifespan(app):
        yield
        database.close()

    return create_fastapi_memory_app(
        service_factory,
        resolve_scope,
        authorize,
        lifespan=lifespan,
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(build_app(), host="0.0.0.0", port=8000)
