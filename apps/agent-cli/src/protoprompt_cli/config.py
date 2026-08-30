"""Конфигурация pp-agent: дефолты + config.toml + env-переменные.

Приоритет источников (возрастающий): ``DEFAULT_CONFIG`` → файл
``.protoprompt/config.toml`` (tomllib) → env ``PP_*``.
"""

from __future__ import annotations

import copy
import os
import tomllib
from pathlib import Path
from typing import Any

DEFAULT_CONFIG: dict[str, Any] = {
    "llm": {
        "backend": "ollama",
        "chat_model": "",
        "embed_model": "nomic-embed-text",
        "ollama": {"host": "http://localhost:11434"},
        "openai": {
            "model": "gpt-4o-mini",
            "embed_model": "text-embedding-3-small",
        },
        "httpx": {
            "base_url": "http://localhost:11434/v1",
            "api_key_env": "PP_OPENAI_API_KEY",
        },
    },
    "memory": {
        "max_tokens": 2048,
        "recall_cooldown_steps": 10,
        "dedup_threshold": 0.92,
        "max_pinned_ratio": 0.33,
    },
    "agent": {
        "max_iterations": 8,
        "tail": 8,
        # This is the whole provider request ceiling, independent from the
        # WorkingMemory budget above.  The reserve is passed through as the
        # model completion cap, so every request is bounded end-to-end.
        "request_max_tokens": 8192,
        "output_reserve_tokens": 1024,
        "system_prompt": (
            "Ты — кодер-агент. Работаешь в проекте пользователя. Отвечай "
            "кратко и по делу. Чтобы прочитать файл, изменить его или "
            "запустить команду, оборачивай действие в тег вида "
            '<action name="bash">ls</action> или '
            '<action name="write" path="x.py">…код…</action>. '
            "Используй инструменты ТОЛЬКО когда это действительно нужно для "
            "задачи. Для простых вопросов отвечай сразу, без тегов. Когда "
            "задача решена, дай финальный ответ без тегов. Никогда не используй "
            "sudo, su, chmod на системных путях, sudoers или команды для "
            "эскалации привилегий. Не работай за пределами корня проекта."
        ),
    },
}

ENV_OVERRIDES: dict[str, tuple[str, ...]] = {
    "PP_LLM_BACKEND": ("llm", "backend"),
    "PP_CHAT_MODEL": ("llm", "chat_model"),
    "PP_EMBED_MODEL": ("llm", "embed_model"),
    "PP_OLLAMA_HOST": ("llm", "ollama", "host"),
    "PP_OPENAI_API_KEY": ("llm", "openai", "api_key"),
    "PP_OPENAI_BASE_URL": ("llm", "openai", "base_url"),
    "PP_HTTPX_BASE_URL": ("llm", "httpx", "base_url"),
    "PP_MAX_TOKENS": ("memory", "max_tokens"),
    "PP_REQUEST_MAX_TOKENS": ("agent", "request_max_tokens"),
    "PP_OUTPUT_RESERVE_TOKENS": ("agent", "output_reserve_tokens"),
}


def _deep_merge(base: dict, override: dict) -> dict:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _coerce(value: str) -> Any:
    if value.isdigit():
        return int(value)
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    return value


def _set_dotted(target: dict, keys: tuple[str, ...], value: Any) -> None:
    node = target
    for key in keys[:-1]:
        node = node.setdefault(key, {})
    node[keys[-1]] = value


def load_config(path: str | Path | None = None) -> dict:
    """Собрать конфигурацию из дефолтов, файла и env."""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if path is not None:
        p = Path(path)
        if p.is_file():
            with p.open("rb") as fh:
                cfg = _deep_merge(cfg, tomllib.load(fh))
    for env_name, keys in ENV_OVERRIDES.items():
        raw = os.environ.get(env_name)
        if raw is not None and raw != "":
            _set_dotted(cfg, keys, _coerce(raw))
    return cfg


def project_config_path(root: str | Path) -> Path:
    return Path(root) / ".protoprompt" / "config.toml"
