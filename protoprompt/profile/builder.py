from __future__ import annotations

import json
import logging

from protoprompt.llm import LLMClientProtocol
from protoprompt.profile.types import UserProfile

logger = logging.getLogger(__name__)


class ProfileBuilder:
    """Build a structured user profile by asking the LLM to analyse turns.

    The LLM is expected to return strict JSON; on any failure the builder
    logs the original exception and returns a minimal profile with
    ``summary="Не удалось построить профиль"``.
    """

    def __init__(self, llm: LLMClientProtocol) -> None:
        self._llm = llm

    async def build(self, user_id: str, messages: list[dict]) -> UserProfile:
        if not messages:
            return UserProfile(user_id=user_id)

        user_texts = [m["content"] for m in messages if m.get("role") == "user"]
        if not user_texts:
            return UserProfile(user_id=user_id)

        prompt = (
            "Проанализируй сообщения пользователя. Выдели:\n"
            "1. Стиль общения (кратко/развёрнуто, формально/неформально)\n"
            "2. Предпочтения (любит списки, нарратив, технические детали)\n"
            "3. Уровень экспертизы (новичок, средний, эксперт)\n"
            "Ответ дай строго в формате JSON:\n"
            '{"traits": {"style": "...", "expertise": "..."},'
            '"preferences": {"format": "..."}, "summary": "..."}\n\n'
            "Сообщения пользователя:\n" + "\n".join(user_texts[-20:])
        )

        try:
            response = await self._llm.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.3,
                max_tokens=300,
            )
            data = json.loads(response)
            return UserProfile(
                user_id=user_id,
                traits=data.get("traits", {}),
                preferences=data.get("preferences", {}),
                summary=data.get("summary", ""),
            )
        except Exception:
            logger.warning(
                "Failed to build profile for user_id=%s", user_id, exc_info=True
            )
            return UserProfile(
                user_id=user_id,
                summary="Не удалось построить профиль",
            )
