from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class UserProfile:
    user_id: str = ""
    traits: dict[str, str] = field(default_factory=dict)
    preferences: dict[str, str] = field(default_factory=dict)
    summary: str = ""
    updated_at: str = ""
