"""Разбор action-блоков из текстового ответа модели.

Формат (D2): модель оборачивает инструмент в тег
``<action name="bash">ls -la</action>``. Атрибуты кроме ``name`` уходят
в ``kwargs`` (например ``path``, ``old``, ``new``). Парсер толерантен
к обрывкам: незакрытый хвостовой блок восстанавливается, битые — молча
пропускаются.
"""

from __future__ import annotations

import hashlib
import json
import re
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

# Consent must be based on a complete, terminal-safe rendering of the action,
# never on an ellipsized summary.  This bounds an approval request to a size a
# human can reasonably inspect while refusing a payload that cannot fit.
MAX_APPROVAL_PREVIEW_BYTES = 4096


@dataclass(frozen=True)
class ApprovalPreview:
    """A terminal-safe, complete action rendering for interactive consent.

    ``complete`` is false when the full rendering would exceed the explicit
    display bound.  Such a request is informational only and must be denied;
    callers must never turn its digest into an approval shortcut.
    """

    text: str
    complete: bool
    action_label: str
    rendered_bytes: int
    fingerprint: str


def _terminal_quote(value: object) -> str:
    """Return one JSON string literal without terminal control characters."""

    # ``ensure_ascii`` escapes C0/C1 controls, ESC/OSC delimiters, CR/LF and
    # bidirectional/non-ASCII controls.  DEL is not required to be escaped by
    # JSON, so replace it explicitly as well.
    encoded = json.dumps(value, ensure_ascii=True)
    return encoded.replace("\x7f", "\\u007f")


def _approval_fingerprint(name: object, fields: list[tuple[str, object]]) -> str:
    """Hash the exact structured action represented by an approval preview."""

    canonical = json.dumps(
        {"name": name, "fields": fields},
        ensure_ascii=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8", "surrogatepass")).hexdigest()


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

    def _approval_fields(self) -> list[tuple[str, object]]:
        """Return every value relevant to the current tool invocation.

        The known write/edit forms come first in execution order.  Extra
        kwargs and an otherwise ignored body remain visible too, so a model
        cannot hide text in the structured action it asked the user to allow.
        """

        def extras(excluded: set[str]) -> list[tuple[str, object]]:
            return [
                (f"kwargs.{key}", value)
                for key, value in sorted(self.kwargs.items())
                if key not in excluded
            ]

        if self.name == "bash":
            return [("command", self.body), *extras(set())]
        if self.name == "write":
            return [
                ("path", self.kwargs.get("path", "")),
                ("content", self.body),
                *extras({"path"}),
            ]
        if self.name == "edit":
            return [
                ("path", self.kwargs.get("path", "")),
                ("old", self.kwargs.get("old", "")),
                ("new", self.kwargs.get("new", "")),
                ("body_ignored_by_edit", self.body),
                *extras({"path", "old", "new"}),
            ]
        return [("body", self.body), *extras(set())]

    def approval_preview(
        self, *, max_bytes: int = MAX_APPROVAL_PREVIEW_BYTES
    ) -> ApprovalPreview:
        """Render the whole action safely, or return a fail-closed notice.

        This is deliberately separate from :meth:`summary`, whose abbreviated
        representation is useful for memory bookkeeping but unsafe as a basis
        for authorizing a process or filesystem mutation.
        """

        fields = self._approval_fields()
        action_label = _terminal_quote(self.name)
        lines = [f"action = {action_label}"]
        lines.extend(
            f"field[{_terminal_quote(label)}] = {_terminal_quote(value)}"
            for label, value in fields
        )
        rendered = "\n".join(lines)
        rendered_bytes = len(rendered.encode("utf-8", "surrogatepass"))
        fingerprint = _approval_fingerprint(self.name, fields)
        if rendered_bytes <= max_bytes:
            return ApprovalPreview(
                text=rendered,
                complete=True,
                action_label=action_label,
                rendered_bytes=rendered_bytes,
                fingerprint=fingerprint,
            )
        return ApprovalPreview(
            text=(
                "payload cannot be displayed completely within the safe "
                f"approval limit ({rendered_bytes} > {max_bytes} UTF-8 bytes); "
                f"sha256={fingerprint}; request denied"
            ),
            complete=False,
            action_label=action_label,
            rendered_bytes=rendered_bytes,
            fingerprint=fingerprint,
        )

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
