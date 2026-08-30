"""Тесты конфигурации: дефолты, файл, env, приоритеты."""

from __future__ import annotations

from pathlib import Path

import pytest

from protoprompt_cli import persistence
from protoprompt_cli.config import (
    DEFAULT_CONFIG,
    ENV_OVERRIDES,
    load_config,
    project_config_path,
    user_config_path,
)


def test_defaults_without_file_or_env(monkeypatch):
    for var in ENV_OVERRIDES:
        monkeypatch.delenv(var, raising=False)
    cfg = load_config()
    assert cfg["llm"]["backend"] == "ollama"
    assert cfg["memory"]["max_tokens"] == 2048
    assert cfg["agent"]["max_iterations"] == 8
    assert cfg["llm"]["ollama"]["host"] == "http://localhost:11434"
    assert cfg["llm"]["embed_model"] == ""


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


def test_config_drops_inline_credentials_and_environment_selectors(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(
        """[llm.openai]
api_key = "file-secret"
api_key_env = "AWS_SECRET_ACCESS_KEY"

[llm.httpx]
api_key = "another-file-secret"
api_key_env = "AWS_SECRET_ACCESS_KEY"
""",
        encoding="utf-8",
    )
    cfg = load_config(config)
    assert "api_key" not in cfg["llm"]["openai"]
    assert "api_key_env" not in cfg["llm"]["openai"]
    assert "api_key" not in cfg["llm"]["httpx"]
    assert "api_key_env" not in cfg["llm"]["httpx"]
    assert "file-secret" not in repr(cfg)


def test_openai_credential_environment_value_never_enters_config(monkeypatch):
    monkeypatch.setenv("PP_OPENAI_API_KEY", "environment-secret")
    cfg = load_config()
    assert "environment-secret" not in repr(cfg)
    assert "api_key" not in cfg["llm"]["openai"]


def test_env_coerces_int_and_keeps_string(monkeypatch):
    monkeypatch.setenv("PP_MAX_TOKENS", "900")
    monkeypatch.setenv("PP_REQUEST_MAX_TOKENS", "4096")
    monkeypatch.setenv("PP_OUTPUT_RESERVE_TOKENS", "512")
    monkeypatch.setenv("PP_CHAT_MODEL", "llama3.1:8b")
    cfg = load_config()
    assert cfg["memory"]["max_tokens"] == 900
    assert isinstance(cfg["memory"]["max_tokens"], int)
    assert cfg["agent"]["request_max_tokens"] == 4096
    assert cfg["agent"]["output_reserve_tokens"] == 512
    assert cfg["llm"]["chat_model"] == "llama3.1:8b"


def test_empty_env_var_is_ignored(monkeypatch):
    monkeypatch.setenv("PP_LLM_BACKEND", "")
    cfg = load_config()
    assert cfg["llm"]["backend"] == "ollama"


def test_config_paths_keep_project_path_explicit_and_default_path_user_owned(tmp_path, monkeypatch):
    state_home = tmp_path / "user-state"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("PROTOPROMPT_AGENT_STATE_DIR", str(state_home))
    assert project_config_path("/x/y") == Path("/x/y/.protoprompt/config.toml")
    assert user_config_path(project) == (
        state_home / persistence.namespace_for(project) / "config.toml"
    )


def test_default_config_has_required_sections():
    assert set(DEFAULT_CONFIG) == {"llm", "memory", "agent"}
    assert set(DEFAULT_CONFIG["memory"]) >= {
        "max_tokens", "recall_cooldown_steps", "dedup_threshold", "max_pinned_ratio",
    }
    assert DEFAULT_CONFIG["agent"]["request_max_tokens"] == 8192
    assert DEFAULT_CONFIG["agent"]["output_reserve_tokens"] == 1024
