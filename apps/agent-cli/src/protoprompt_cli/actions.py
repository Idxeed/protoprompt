"""Разбор action-блоков из текстового ответа модели.

Формат (D2): модель оборачивает инструмент в тег
``<action name="bash">ls -la</action>``. Атрибуты кроме ``name`` уходят
в ``kwargs`` (например ``path``, ``old``, ``new``). Парсер толерантен
к обрывкам: незакрытый хвостовой блок восстанавливается, битые — молча
пропускаются.
"""

from __future__ import annotations

import re
import json
from dataclasses import dataclass, field

_ACTION_RE = re.compile(
    r"<action(?P<attrs>[^>]*)>(?P<body>.*?)</action>",
    re.DOTALL | re.IGNORECASE,
)
_ATTR_RE = re.compile(r'(\w+)\s*=\s*"([^"]*)"')
_UNCLOSED_RE = re.compile(
    r"<action(?P<attrs>[^>]*)>(?P<body>.*)$", re.DOTALL | re.IGNORECASE
)
_TOOL_CALL_RE = re.compile(
    r"<tool_call>(?P<body>.*?)</tool_call>", re.DOTALL | re.IGNORECASE
)


@dataclass
class Action:
    """Один запрошенный моделью вызов инструмента."""

    name: str
    body: str = ""
    kwargs: dict[str, str] = field(default_factory=dict)

    def summary(self, limit: int = 80) -> str:
        base = self.body.strip().replace("\n", " ")
        if len(base) > limit:
            base = base[:limit] + "…"
        return f"{self.name}: {base}" if base else self.name

    @property
    def is_empty(self) -> bool:
        return not self.body.strip()


def _attrs_to_action(raw_attrs: str, body: str) -> Action | None:
    attrs = dict(_ATTR_RE.findall(raw_attrs))
    name = attrs.pop("name", "")
    if not name:
        bare = raw_attrs.strip()
        if bare and "=" not in bare:
            name = bare.split()[0]
    if not name:
        return None
    return Action(name=name.strip().lower(), body=body, kwargs=attrs)


def parse_actions(text: str) -> list[Action]:
    """Извлечь все action-блоки из ответа модели."""
    matches = list(_ACTION_RE.finditer(text))
    actions = []
    for match in matches:
        action = _attrs_to_action(match.group("attrs"), match.group("body"))
        if action is not None:
            actions.append(action)

    tail_start = matches[-1].end() if matches else 0
    tail = text[tail_start:]
    unclosed = _UNCLOSED_RE.search(tail)
    if unclosed is not None:
        action = _attrs_to_action(unclosed.group("attrs"), unclosed.group("body"))
        if action is not None:
            actions.append(action)
    if actions:
        return actions

    for match in _TOOL_CALL_RE.finditer(text):
        try:
            raw = json.loads(match.group("body"))
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, dict):
            continue
        name = raw.get("name") or raw.get("tool")
        arguments = raw.get("arguments", raw.get("args", {}))
        if not isinstance(name, str) or not isinstance(arguments, dict):
            continue
        body = str(arguments.pop("command", ""))
        actions.append(Action(name=name.lower(), body=body, kwargs={
            str(key): str(value) for key, value in arguments.items()
        }))
    return actions


def strip_actions(text: str) -> str:
    """Удалить action-блоки (включая незакрытый хвост), вернуть остаток."""
    last_action = text.rfind("<action")
    if last_action >= 0 and "</action" not in text[last_action:]:
        text = text[:last_action]
    cleaned = _ACTION_RE.sub("", text)
    return re.sub(r"\n{2,}", "\n", cleaned).strip()
