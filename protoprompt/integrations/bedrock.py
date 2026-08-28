"""Amazon Bedrock Converse adapter."""

from __future__ import annotations

import asyncio
from typing import Any

from protoprompt.integrations._messages import response_text, split_system_messages, text_blocks


class BedrockConverseClient:
    """Async facade over boto3's provider-neutral Bedrock Converse API.

    boto3 remains synchronous; calls run in a worker thread so they do not
    block ProtoPrompt's async pipeline. IAM, profiles, web identity and instance
    roles keep their standard boto3 credential-chain semantics.
    """

    def __init__(
        self,
        model_id: str = "",
        *,
        region_name: str | None = None,
        profile_name: str | None = None,
        endpoint_url: str | None = None,
        client: Any | None = None,
    ) -> None:
        if client is None:
            try:
                import boto3
            except ImportError as exc:
                raise ImportError(
                    "BedrockConverseClient requires the 'boto3' package. "
                    "Install with: pip install 'protoprompt[bedrock]'"
                ) from exc
            session_kwargs: dict[str, Any] = {}
            if profile_name is not None:
                session_kwargs["profile_name"] = profile_name
            session = boto3.Session(**session_kwargs)
            client_kwargs: dict[str, Any] = {}
            if region_name is not None:
                client_kwargs["region_name"] = region_name
            if endpoint_url is not None:
                client_kwargs["endpoint_url"] = endpoint_url
            client = session.client("bedrock-runtime", **client_kwargs)
        self._client = client
        self._model_id = model_id

    def _request(
        self,
        messages: list[dict],
        model: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        selected_model = model or self._model_id
        if not selected_model:
            raise ValueError("Bedrock Converse requires model_id or chat(model=...)")
        system, turns = split_system_messages(messages)
        converse = {
            "messages": [
                {"role": turn["role"], "content": text_blocks(turn["content"])}
                for turn in turns
            ]
        }
        if system:
            converse["system"] = [{"text": text} for text in system]
        return {"modelId": selected_model, **converse}, converse

    async def chat(
        self,
        messages: list[dict],
        model: str = "",
        temperature: float | None = None,
        max_tokens: int | None = None,
        **options: object,
    ) -> str:
        request, _ = self._request(messages, model)
        inference = dict(options.pop("inferenceConfig", {}) or {})
        if temperature is not None:
            inference["temperature"] = temperature
        if max_tokens is not None:
            inference["maxTokens"] = max_tokens
        if inference:
            request["inferenceConfig"] = inference
        request.update(options)
        response = await asyncio.to_thread(self._client.converse, **request)
        return response_text(response.get("output", {}).get("message", {}).get("content", []))

    async def count_tokens(
        self,
        messages: list[dict],
        model: str = "",
        **options: object,
    ) -> int:
        request, converse = self._request(messages, model)
        converse.update(options)
        response = await asyncio.to_thread(
            self._client.count_tokens,
            modelId=request["modelId"],
            input={"converse": converse},
        )
        return int(response["inputTokens"])

    async def aclose(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            await asyncio.to_thread(close)
