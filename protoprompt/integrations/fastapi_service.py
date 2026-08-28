"""FastAPI recipe for a host-authenticated, scope-pinned MemoryService."""

import inspect
import json
from typing import Any, Callable

from protoprompt.connectivity import MemoryService
from protoprompt.scope import MemoryScope


def create_fastapi_memory_app(
    service_factory: Callable[[MemoryScope], MemoryService | Any],
    scope_resolver: Callable[[Any], MemoryScope | Any],
    authorize: Callable[[Any], Any],
    *,
    title: str = "protoprompt memory service",
    lifespan: Any | None = None,
    max_text_chars: int = 100_000,
    max_metadata_bytes: int = 32_768,
) -> Any:
    """Create an API without accepting tenant/user/thread from request bodies.

    ``authorize`` authenticates the request. ``scope_resolver`` derives a
    :class:`MemoryScope` from trusted host state (for example verified JWT
    claims), and ``service_factory`` returns a service pinned to that scope.
    All three are mandatory so the recipe has no insecure header-based default.
    """

    try:
        from fastapi import Depends, FastAPI, HTTPException, Request
        from pydantic import BaseModel, ConfigDict, Field
    except ImportError as exc:
        raise ImportError(
            "create_fastapi_memory_app requires FastAPI. Install with: "
            "pip install 'protoprompt[fastapi]'"
        ) from exc
    if max_text_chars < 1 or max_metadata_bytes < 2:
        raise ValueError("request limits must be positive")

    class RememberRequest(BaseModel):
        model_config = ConfigDict(extra="forbid")
        text: str = Field(min_length=1, max_length=max_text_chars)
        memory_id: str | None = Field(default=None, min_length=1, max_length=256)
        metadata: dict[str, Any] | None = None

    class SearchRequest(BaseModel):
        model_config = ConfigDict(extra="forbid")
        query: str = Field(min_length=1, max_length=max_text_chars)
        top_k: int = Field(default=5, ge=1, le=100)
        score_threshold: float | None = Field(default=None, ge=-1.0, le=1.0)

    class ProfileSignalRequest(BaseModel):
        model_config = ConfigDict(extra="forbid")
        text: str = Field(min_length=1, max_length=max_text_chars)

    app = FastAPI(title=title, version="1", lifespan=lifespan)

    async def _service(request: Request) -> MemoryService:
        try:
            await _maybe_await(authorize(request))
            scope = await _maybe_await(scope_resolver(request))
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=401, detail="authentication failed") from exc
        if not isinstance(scope, MemoryScope) or scope.is_empty:
            raise HTTPException(status_code=403, detail="no authorized memory scope")
        service = await _maybe_await(service_factory(scope))
        if not isinstance(service, MemoryService):
            raise HTTPException(status_code=500, detail="invalid memory service factory")
        if service.scope != scope:
            raise HTTPException(status_code=500, detail="memory service scope mismatch")
        return service

    def _check_metadata(metadata: dict[str, Any] | None) -> None:
        if metadata is None:
            return
        try:
            size = len(json.dumps(metadata, ensure_ascii=False).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="metadata must be JSON") from exc
        if size > max_metadata_bytes:
            raise HTTPException(status_code=413, detail="metadata is too large")

    @app.get("/healthz", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/v1/memories", status_code=201)
    async def remember(
        body: RememberRequest,
        service: MemoryService = Depends(_service),
    ) -> dict[str, Any]:
        _check_metadata(body.metadata)
        return await _service_call(
            service.remember,
            body.text,
            memory_id=body.memory_id,
            metadata=body.metadata,
            HTTPException=HTTPException,
        )

    @app.post("/v1/memories/search")
    async def search(
        body: SearchRequest,
        service: MemoryService = Depends(_service),
    ) -> dict[str, Any]:
        results = await _service_call(
            service.search,
            body.query,
            top_k=body.top_k,
            score_threshold=body.score_threshold,
            HTTPException=HTTPException,
        )
        return {"results": results}

    @app.delete("/v1/memories/{memory_id}")
    async def forget(
        memory_id: str,
        service: MemoryService = Depends(_service),
    ) -> dict[str, Any]:
        if not 1 <= len(memory_id) <= 256:
            raise HTTPException(status_code=422, detail="invalid memory_id")
        return await _service_call(
            service.forget, memory_id, HTTPException=HTTPException
        )

    @app.get("/v1/memory/explain")
    async def explain(
        service: MemoryService = Depends(_service),
    ) -> dict[str, Any]:
        return service.explain()

    @app.get("/v1/memory/manifest")
    async def manifest(
        service: MemoryService = Depends(_service),
    ) -> dict[str, Any]:
        return service.manifest()

    @app.get("/v1/memory/budget-report")
    async def budget_report(
        service: MemoryService = Depends(_service),
    ) -> dict[str, Any]:
        return {"report": service.budget_report()}

    @app.get("/v1/profile")
    async def profile(
        service: MemoryService = Depends(_service),
    ) -> dict[str, Any]:
        return {"profile": await service.current_profile()}

    @app.post("/v1/profile/signals")
    async def profile_signal(
        body: ProfileSignalRequest,
        service: MemoryService = Depends(_service),
    ) -> dict[str, Any]:
        return await _service_call(
            service.profile_update, body.text, HTTPException=HTTPException
        )

    return app


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _service_call(method: Callable[..., Any], *args: Any, HTTPException: Any, **kwargs: Any) -> Any:
    try:
        return await _maybe_await(method(*args, **kwargs))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
