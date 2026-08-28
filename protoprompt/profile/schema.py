"""Loader and enum helpers for the profile JSON schema.

The canonical schema lives in :file:`schema.json` next to this module; it
is the single source of truth for closed-world enum values. The codec
uses :data:`ENUM_VALUES` to normalize LLM output, and consumers may use
:data:`PROFILE_SCHEMA` for optional ``jsonschema`` validation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_SCHEMA_PATH = Path(__file__).with_name("schema.json")

PROFILE_SCHEMA: dict[str, Any] = json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))

_ENUM_CONTAINERS = ("traits", "preferences")


def _enum_values() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    props = PROFILE_SCHEMA.get("properties", {})
    for container in _ENUM_CONTAINERS:
        for name, spec in props.get(container, {}).get("properties", {}).items():
            enum = spec.get("enum")
            if enum:
                out[name] = [str(v) for v in enum]
    return out


ENUM_VALUES: dict[str, list[str]] = _enum_values()
