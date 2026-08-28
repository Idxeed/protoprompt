"""Run one native provider client selected by PROTOPROMPT_PROVIDER.

Examples:
    PROTOPROMPT_PROVIDER=anthropic ANTHROPIC_API_KEY=... python examples/provider_clients.py
    PROTOPROMPT_PROVIDER=google GEMINI_API_KEY=... python examples/provider_clients.py
    PROTOPROMPT_PROVIDER=bedrock AWS_PROFILE=... BEDROCK_MODEL_ID=... python examples/provider_clients.py
"""

from __future__ import annotations

import asyncio
import os

from protoprompt.integrations import (
    AnthropicClient,
    BedrockConverseClient,
    GoogleGenAIClient,
)


def create_client():
    provider = os.getenv("PROTOPROMPT_PROVIDER", "anthropic").lower()
    if provider == "anthropic":
        return AnthropicClient()
    if provider == "google":
        return GoogleGenAIClient()
    if provider == "bedrock":
        model_id = os.environ["BEDROCK_MODEL_ID"]
        return BedrockConverseClient(model_id)
    raise ValueError("PROTOPROMPT_PROVIDER must be anthropic, google, or bedrock")


async def main() -> None:
    client = create_client()
    messages = [
        {"role": "system", "content": "Answer in one sentence."},
        {"role": "user", "content": "What is semantic long-term memory?"},
    ]
    try:
        print("input tokens:", await client.count_tokens(messages))
        print(await client.chat(messages, max_tokens=120))
    finally:
        await client.aclose()


if __name__ == "__main__":
    asyncio.run(main())
