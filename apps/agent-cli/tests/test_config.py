"""Тесты конфигурации: дефолты, файл, env, приоритеты."""

from __future__ import annotations

from pathlib import Path

import pytest

from protoprompt_cli.config import (
    DEFAULT_CONFIG,
    ENV_OVERRIDES,
    load_config,
    project_config_path,
)


def test_defaults_without_file_or_env(monkeypatch):
    for var in ENV_OVERRIDES:
        monkeypatch.delenv(var, raising=False)
    cfg = load_config()
    assert cfg["llm"]["backend"] == "ollama"
    assert cfg["memory"]["max_tokens"] == 2048
    assert cfg["agent"]["max_iterations"] == 8
    assert cfg["llm"]["ollama"]["host"] == "http://localhost:11434"


def test_file_overrides_backend(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[llm]\nbackend = "httpx"\n', encoding="utf-8")
    cfg = load_config(config)
    assert cfg["llm"]["backend"] == "httpx"
    assert cfg["memory"]["max_tokens"] == 2048


def test_file_merges_nested(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[llm.ollama]\nhost = "http://127.0.0.1:9999"\n',
                      encoding="utf-8")
    cfg = load_config(config)
    assert cfg["llm"]["ollama"]["host"] == "http://127.0.0.1:9999"
    assert cfg["llm"]["backend"] == "ollama"
    assert cfg["memory"]["max_tokens"] == 2048


def test_missing_file_is_ignored(tmp_path):
    cfg = load_config(tmp_path / "nope.toml")
    assert cfg["llm"]["backend"] == "ollama"


def test_env_overrides_file_and_default(monkeypatch, tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[llm]\nbackend = "httpx"\n', encoding="utf-8")
    monkeypatch.setenv("PP_LLM_BACKEND", "openai")
    monkeypatch.setenv("PP_MAX_TOKENS", "512")
    cfg = load_config(config)
    assert cfg["llm"]["backend"] == "openai"
    assert cfg["memory"]["max_tokens"] == 512


def test_env_coerces_int_and_keeps_string(monkeypatch):
    monkeypatch.setenv("PP_MAX_TOKENS", "900")
    monkeypatch.setenv("PP_CHAT_MODEL", "llama3.1:8b")
    cfg = load_config()
    assert cfg["memory"]["max_tokens"] == 900
    assert isinstance(cfg["memory"]["max_tokens"], int)
    assert cfg["llm"]["chat_model"] == "llama3.1:8b"


def test_empty_env_var_is_ignored(monkeypatch):
    monkeypatch.setenv("PP_LLM_BACKEND", "")
    cfg = load_config()
    assert cfg["llm"]["backend"] == "ollama"


def test_project_config_path():
    assert project_config_path("/x/y") == Path("/x/y/.protoprompt/config.toml")


def test_default_config_has_required_sections():
    assert set(DEFAULT_CONFIG) == {"llm", "memory", "agent"}
    assert set(DEFAULT_CONFIG["memory"]) >= {
        "max_tokens", "recall_cooldown_steps", "dedup_threshold", "max_pinned_ratio",
    }