"""Regression tests for the CLI's terminal-output boundary."""

from __future__ import annotations

import pytest

from protoprompt_cli.terminal import escape_terminal_text


@pytest.mark.parametrize(
    ("raw", "visible"),
    [
        ("\x1b[8m", r"\u001b[8m"),
        ("\x1b]52;c;clipboard\x07", r"\u001b]52;c;clipboard\u0007"),
        ("\r\t", r"\u000d\u0009"),
        ("\x9b31m", r"\u009b31m"),
        ("\x7f", r"\u007f"),
        ("\u202ereversed", r"\u202ereversed"),
        ("\u200b", r"\u200b"),
        ("\ud800", r"\ud800"),
    ],
)
def test_terminal_renderer_makes_control_and_format_characters_visible(raw, visible):
    assert escape_terminal_text(raw) == visible


def test_terminal_renderer_preserves_safe_text_and_line_structure():
    assert escape_terminal_text("first line\nsecond line") == "first line\nsecond line"


def test_terminal_renderer_coerces_non_string_values_at_the_boundary():
    assert escape_terminal_text(42) == "42"
