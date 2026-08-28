"""Localized section headers for prompt assembly.

The section labels injected into the final ``system_prompt`` are the only
hardcoded user-facing strings in the builders. Keeping them here gives a
single place to localize, while the rest of the context stays
language-neutral.
"""

from __future__ import annotations

from typing import Any

_SECTIONS: dict[str, dict[str, str]] = {
    "ru": {
        "profile": "Профиль пользователя:",
        "session": "История диалога (сжатая):",
    },
    "en": {
        "profile": "User profile:",
        "session": "Conversation history (compressed):",
    },
}

_DEFAULT_LANGUAGE = "ru"


def section_header(name: str, language: str = _DEFAULT_LANGUAGE) -> str:
    """Return the localized label for a named section (``""`` if unknown)."""
    table = _SECTIONS.get(language, _SECTIONS[_DEFAULT_LANGUAGE])
    return table.get(name, "")
