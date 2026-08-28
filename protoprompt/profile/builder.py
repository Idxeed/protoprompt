from __future__ import annotations

import json
import logging
import warnings

from protoprompt.llm import ChatClientProtocol
from protoprompt.profile.codec import coerce_topics
from protoprompt.profile.types import Preferences, Traits, UserProfile

logger = logging.getLogger(__name__)


class ProfileBuilder:
    """Build a structured user profile by asking the LLM to analyse turns.

    The LLM is expected to return strict JSON; on any failure the builder
    logs the original exception and returns a minimal profile with
    ``summary="Не удалось построить профиль"``.

    .. deprecated:: 0.3.0
        Superseded by the cross-session profile engine
        (``ProfileProtocol`` sources + ``ProfileManager``). Kept as a
        compatibility alias for now.
    """

    def __init__(self, llm: ChatClientProtocol) -> None:
        warnings.warn(
            "ProfileBuilder is deprecated; use ProfileManager with "
            "LLMProfileSource (or RuleProfileSource) for the cross-session "
            "profile engine.",
            DeprecationWarning,
            stacklevel=2,
        )
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
            if not isinstance(data, dict):
                raise ValueError("profile payload is not an object")
            traits = data.get("traits", {}) or {}
            prefs = data.get("preferences", {}) or {}
            return UserProfile(
                user_id=user_id,
                traits=Traits(
                    style=str(traits.get("style", "")),
                    expertise=str(traits.get("expertise", "")),
                    verbosity=str(traits.get("verbosity", "")),
                    formality=str(traits.get("formality", "")),
                ),
                preferences=Preferences(
                    format=str(prefs.get("format", "")),
                    language=str(prefs.get("language", "")),
                    topics=coerce_topics(prefs.get("topics")),
                ),
                summary=str(data.get("summary", "")),
            )
        except Exception:
            logger.warning(
                "Failed to build profile for user_id=%s", user_id, exc_info=True
            )
            return UserProfile(
                user_id=user_id,
                summary="Не удалось построить профиль",
            )
