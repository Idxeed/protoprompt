"""Local embedding backends: sentence-transformers and fastembed.

Both expose only ``embed()`` — there is no chat model behind them, so
``chat()`` raises ``NotImplementedError``. Use them for RAG-only
pipelines, or pair with any chat client via a small composite.
"""

from __future__ import annotations

import asyncio


class _EmbedOnlyClient:
    """Shared plumbing; subclass configures the backend."""

    def __init__(self, batch_size: int = 32) -> None:
        self._batch_size = max(1, batch_size)

    async def chat(
        self,
        messages: list[dict],
        model: str = "",
        **options: object,
    ) -> str:
        raise NotImplementedError(
            f"{type(self).__name__} provides embeddings only; "
            "pair it with a chat-capable client for LLM calls"
        )

    async def embed(
        self, texts: list[str], model: str = ""
    ) -> list[list[float]]:
        if not texts:
            return []
        vectors = await asyncio.to_thread(self._encode_sync, texts)
        return [list(map(float, v)) for v in vectors]

    def _encode_sync(self, texts: list[str]) -> list:
        raise NotImplementedError


class SentenceTransformersClient(_EmbedOnlyClient):
    """Embeddings from ``sentence_transformers`` (runs locally on CPU/GPU).

    Args:
        model_name: any HF model id, e.g.
            ``intfloat/multilingual-e5-small``, or a local path.
        device: ``"cpu"``, ``"cuda"``, etc.; auto-detected when omitted.
    """

    def __init__(
        self,
        model_name: str = "sentence-transformers/all-MiniLM-L6-v2",
        device: str | None = None,
        batch_size: int = 32,
    ) -> None:
        super().__init__(batch_size=batch_size)
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise ImportError(
                "SentenceTransformersClient requires 'sentence-transformers'. "
                "Install with: pip install 'protoprompt[local]'"
            ) from exc
        self._model = SentenceTransformer(model_name, device=device)

    def _encode_sync(self, texts: list[str]) -> list:
        return self._model.encode(
            texts,
            batch_size=self._batch_size,
            convert_to_numpy=True,
        ).tolist()


class FastEmbedClient(_EmbedOnlyClient):
    """Embeddings from ``fastembed`` (ONNX runtime, lightweight install).

    Args:
        model_name: see ``fastembed.SupportedModels``; defaults to a
            compact multilingual-friendly BGE variant.
    """

    def __init__(
        self,
        model_name: str = "BAAI/bge-small-en-v1.5",
        batch_size: int = 32,
        cache_dir: str | None = None,
    ) -> None:
        super().__init__(batch_size=batch_size)
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:
            raise ImportError(
                "FastEmbedClient requires 'fastembed'. "
                "Install with: pip install 'protoprompt[fastembed]'"
            ) from exc
        kwargs: dict = {"model_name": model_name}
        if cache_dir is not None:
            kwargs["cache_dir"] = cache_dir
        self._model = TextEmbedding(**kwargs)

    def _encode_sync(self, texts: list[str]) -> list:
        return [vec.tolist() for vec in self._model.embed(
            texts, batch_size=self._batch_size
        )]
