from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class CompressedBlock:
    text: str
    metadata: dict = field(default_factory=dict)


@dataclass
class Session:
    chat_id: str
    messages: list[dict]
    model: str = ""
    strategy: str = "heuristic"
