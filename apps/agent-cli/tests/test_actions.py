"""Тесты парсера action-блоков."""

from __future__ import annotations

from protoprompt_cli.actions import (
    MAX_APPROVAL_PREVIEW_BYTES,
    Action,
    parse_actions,
    strip_actions,
)


def test_empty_and_plain_text_yield_no_actions():
    assert parse_actions("") == []
    assert parse_actions("просто текст без тегов") == []


def test_single_action_parses_name_and_body():
    actions = parse_actions('<action name="bash">ls -la</action>')
    assert len(actions) == 1
    assert actions[0].name == "bash"
    assert actions[0].body == "ls -la"


def test_multiple_actions_preserve_order():
    text = (
        '<action name="read" path="a.py">x</action> '
        'промежуточный текст '
        '<action name="bash">pytest</action>'
    )
    actions = parse_actions(text)
    assert [a.name for a in actions] == ["read", "bash"]


def test_kwargs_parsed_from_attributes():
    actions = parse_actions(
        '<action name="edit" path="a.py" old="x" new="y">ignored</action>'
    )
    action = actions[0]
    assert action.kwargs == {"path": "a.py", "old": "x", "new": "y"}


def test_bare_name_without_quotes():
    actions = parse_actions("<action bash>echo hi</action>")
    assert actions[0].name == "bash"


def test_name_is_lowercased():
    actions = parse_actions('<action name="BASH">ls</action>')
    assert actions[0].name == "bash"


def test_unclosed_trailing_action_is_recovered():
    actions = parse_actions(
        'сначала текст\n<action name="bash">echo unfinished'
    )
    assert len(actions) == 1
    assert actions[0].name == "bash"
    assert "unfinished" in actions[0].body


def test_unclosed_after_closed_block():
    text = (
        '<action name="read" path="a.py">done</action>\n'
        '<action name="bash">half'
    )
    actions = parse_actions(text)
    assert [a.name for a in actions] == ["read", "bash"]
    assert actions[1].body == "half"


def test_action_without_name_is_ignored():
    assert parse_actions('<action path="a.py">text</action>') == []


def test_action_with_empty_body():
    actions = parse_actions('<action name="bash"></action>')
    assert len(actions) == 1
    assert actions[0].is_empty


def test_body_preserves_whitespace_and_newlines():
    actions = parse_actions('<action name="write" path="f.py">\ncode here\n</action>')
    assert actions[0].body == "\ncode here\n"


def test_summary_truncates_long_body():
    action = Action(name="bash", body="x" * 200)
    assert action.summary().startswith("bash: ")
    assert len(action.summary()) <= 90


def test_summary_for_empty_body_is_just_name():
    action = Action(name="bash", body="   ")
    assert action.summary() == "bash"


def test_approval_preview_shows_the_complete_escaped_bash_command():
    command = "echo safe\r\x1b]0;spoof\x07\nthen-visible-tail"
    preview = Action(name="bash", body=command).approval_preview()

    assert preview.complete is True
    assert 'action = "bash"' in preview.text
    assert 'field["command"] = "echo safe\\r\\u001b]0;spoof\\u0007\\nthen-visible-tail"' in preview.text
    assert "\r" not in preview.text
    assert "\x1b" not in preview.text
    assert "\x07" not in preview.text


def test_approval_preview_covers_write_and_edit_structured_values():
    write_preview = Action(
        name="write",
        body="new\ncontent",
        kwargs={"path": "src/new.py"},
    ).approval_preview()
    edit_preview = Action(
        name="edit",
        body="ignored source body",
        kwargs={"path": "src/app.py", "old": "before", "new": "after"},
    ).approval_preview()

    assert write_preview.complete is True
    assert 'field["path"] = "src/new.py"' in write_preview.text
    assert 'field["content"] = "new\\ncontent"' in write_preview.text
    assert edit_preview.complete is True
    assert 'field["path"] = "src/app.py"' in edit_preview.text
    assert 'field["old"] = "before"' in edit_preview.text
    assert 'field["new"] = "after"' in edit_preview.text
    assert 'field["body_ignored_by_edit"] = "ignored source body"' in edit_preview.text


def test_approval_preview_fails_closed_instead_of_truncating_large_payload():
    tail = "UNSEEN_MALICIOUS_TAIL"
    action = Action(name="bash", body=("x" * MAX_APPROVAL_PREVIEW_BYTES) + tail)

    preview = action.approval_preview()

    assert preview.complete is False
    assert "safe approval limit" in preview.text
    assert "sha256=" in preview.text
    assert tail not in preview.text


def test_code_fence_does_not_hide_action():
    text = "```\n<action name=\"bash\">pytest -q</action>\n```"
    actions = parse_actions(text)
    assert len(actions) == 1
    assert actions[0].name == "bash"


def test_tool_call_json_format():
    actions = parse_actions(
        '<tool_call>{"name":"read","arguments":{"path":"src/a.py"}}</tool_call>'
    )
    assert len(actions) == 1
    assert actions[0].name == "read"
    assert actions[0].kwargs == {"path": "src/a.py"}


def test_tool_call_json_command_becomes_body():
    actions = parse_actions(
        '<tool_call>{"tool":"bash","args":{"command":"git status"}}'
        "</tool_call>"
    )
    assert actions[0].name == "bash"
    assert actions[0].body == "git status"


def test_invalid_tool_call_json_is_ignored():
    assert parse_actions("<tool_call>{broken}</tool_call>") == []


# ── strip_actions ────────────────────────────────────────────────


def test_strip_removes_closed_blocks_keeps_text():
    text = "сначала текст\n<action name=\"bash\">ls</action>\nпотом ответ"
    assert strip_actions(text) == "сначала текст\nпотом ответ"


def test_strip_removes_unclosed_tail():
    text = "текст\n<action name=\"bash\">незакрытый"
    assert strip_actions(text) == "текст"


def test_strip_plain_text_untouched():
    assert strip_actions("просто ответ") == "просто ответ"


def test_strip_empty_text():
    assert strip_actions("") == ""
