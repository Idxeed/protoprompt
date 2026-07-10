"""Shared test fixtures for protoprompt tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))

from _mocks import MockLLM  # noqa: E402


@pytest.fixture
def mock_llm() -> MockLLM:
    return MockLLM()


@pytest.fixture
def mock_llm_factory():
    def _make(dim: int = 16) -> MockLLM:
        return MockLLM(embed_dim=dim)

    return _make
