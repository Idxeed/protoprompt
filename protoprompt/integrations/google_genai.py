"""Native Google Gen AI chat and embedding adapter."""

from __future__ import annotations

from typing import Any

from protoprompt.integrations._messages import split_system_messages, text_blocks


class GoogleGenAIClient:
    """Gemini Developer API or Vertex AI client via ``google-genai``.

    Pass ``vertexai=True`` with ``project`` and ``location`` for ADC-backed
    Vertex AI auth, or an API key for the Gemini Developer API.
    """

    def __init__(
        self,
        api_key: str | None = None,
        *,
        vertexai: bool | None = None,
        project: str | None = None,
        location: str | None = None,
        chat_model: str = "gemini-2.5-flash",
        embed_model: str = "gemini-embedding-001",
        http_options: Any | None = None,
        client: Any | None = None,
    ) -> None:
        if client is None:
            try:
                from google import genai
            except ImportError as exc:
                raise ImportError(
                    "GoogleGenAIClient requires the 'google-genai' package. "
                    "Install with: pip install 'protoprompt[google]'"
                ) from exc
            kwargs: dict[str, Any] = {}
            if api_key is not None:
                kwargs["api_key"] = api_key
            if vertexai is not None:
                kwargs["vertexai"] = vertexai
            if project is not None:
                kwargs["project"] = project
            if location is not None:
                kwargs["location"] = location
            if http_options is not None:
                kwargs["http_options"] = http_options
            client = genai.Client(**kwargs)
        self._root_client = client
        self._client = getattr(client, "aio", client)
        self._chat_model = chat_model
        self._embed_model = embed_model

    def _content_request(
        self, messages: list[dict]
    ) -> tuple[list[dict[str, Any]], list[str]]:
        system, turns = split_system_messages(messages)
        contents = [
            {
                "role": "model" if turn["role"] == "assistant" else "user",
                "parts": text_blocks(turn["content"]),
            }
            for turn in turns
        ]
        return contents, system

    async def chat(
        self,
        messages: list[dict],
        model: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
        **options: object,
    ) -> str:
        contents, system = self._content_request(messages)
        config = dict(options.pop("config", {}) or {})
        config.update(options)
        if system:
            config["system_instruction"] = "\n\n".join(system)
        if temperature is not None:
            config["temperature"] = temperature
        if max_tokens is not None:
            config["max_output_tokens"] = max_tokens
        response = await self._client.models.generate_content(
            model=model or self._chat_model,
            contents=contents,
            config=config or None,
        )
        return response.text or ""

    async def embed(
        self,
        texts: list[str],
        model: str = "",
        **options: object,
    ) -> list[list[float]]:
        config = dict(options.pop("config", {}) or {})
        config.update(options)
        response = await self._client.models.embed_content(
            model=model or self._embed_model,
            contents=texts,
            config=config or None,
        )
        embeddings = response.embeddings or []
        if len(embeddings) != len(texts):
            raise ValueError(
                "Google Gen AI returned an embedding count different from input count"
            )
        return [[float(value) for value in (item.values or [])] for item in embeddings]

    async def count_tokens(
        self,
        messages: list[dict],
        model: str = "",
        **options: object,
    ) -> int:
        contents, system = self._content_request(messages)
        config = dict(options.pop("config", {}) or {})
        config.update(options)
        if system:
            config["system_instruction"] = "\n\n".join(system)
        response = await self._client.models.count_tokens(
            model=model or self._chat_model,
            contents=contents,
            config=config or None,
        )
        return int(response.total_tokens or 0)

    async def aclose(self) -> None:
        close = getattr(self._client, "aclose", None)
        if close is not None:
            await close()
            return
        close = getattr(self._root_client, "close", None)
        if close is not None:
            close()
