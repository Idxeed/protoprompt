"""Render a :class:`UserProfile` into the text injected into the prompt."""

from __future__ import annotations

from protoprompt.i18n import section_header
from protoprompt.profile.types import UserProfile


def render(profile: UserProfile, *, language: str = "ru") -> str:
    """Render a structured profile as a ready-to-inject section.

    Returns ``""`` when the profile has nothing to show, so callers never
    emit an empty header. The content is language-neutral (``key: value``
    lines); only the section header is localized.
    """
    lines: list[str] = []

    for key, value in profile.facts.items():
        lines.append(f"- {key}: {value}")

    traits = profile.traits
    prefs = profile.preferences
    for label, value in (
        ("style", traits.style),
        ("expertise", traits.expertise),
        ("verbosity", traits.verbosity),
        ("formality", traits.formality),
        ("format", prefs.format),
        ("language", prefs.language),
    ):
        if value:
            lines.append(f"- {label}: {value}")

    if prefs.topics:
        lines.append("- topics: " + ", ".join(prefs.topics))

    if profile.summary:
        lines.append(profile.summary)

    if not lines:
        return ""
    return section_header("profile", language) + "\n" + "\n".join(lines)
