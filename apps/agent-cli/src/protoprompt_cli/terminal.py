"""Safe rendering for terminal-facing, potentially untrusted text.

Model replies, repository files and child-process output are data, not ANSI
markup.  Rendering their control sequences literally prevents them from
changing terminal state before a later permission prompt is shown.
"""

from __future__ import annotations

import unicodedata


def escape_terminal_text(value: object) -> str:
    """Make one terminal payload inert while keeping ordinary text readable.

    Newlines are retained for normal multi-line output. Every other C0/C1
    control (including CR, TAB, ESC, BEL, CSI and OSC terminators), DEL, and
    Unicode format character is rendered as a visible escape. Unicode format
    includes bidi overrides and zero-width controls, which can otherwise
    alter how an approval preview is perceived. The function intentionally
    accepts ``object`` so exception messages and arbitrary tool values cannot
    bypass the terminal boundary.
    """

    text = value if isinstance(value, str) else str(value)
    rendered: list[str] = []
    for character in text:
        codepoint = ord(character)
        if character == "\n":
            rendered.append(character)
            continue
        if (
            codepoint <= 0x1F
            or 0x7F <= codepoint <= 0x9F
            or unicodedata.category(character) in {"Cf", "Cs"}
        ):
            if codepoint <= 0xFFFF:
                rendered.append(f"\\u{codepoint:04x}")
            else:
                rendered.append(f"\\U{codepoint:08x}")
            continue
        rendered.append(character)
    return "".join(rendered)
