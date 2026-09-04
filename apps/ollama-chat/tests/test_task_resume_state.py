from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

from protoprompt_ollama_chat.task_resume_state import (
    TASK_RESUME_BINDING_CONTRACT_ID,
    TASK_RESUME_STATE_SCHEMA_VERSION,
    TaskResumeBindingState,
    TaskResumeStateConflictError,
    TaskResumeStateIntegrityError,
    TaskResumeStateLifecycleError,
    TaskResumeStateRepository,
)


_CHECKPOINT_SECRET = b"ollama-chat-task-resume-state-test-secret-0001"
_INSTALLATION_TABLE = "ollama_chat_task_resume_installation"
_BINDING_TABLE = "ollama_chat_task_resume_bindings"


def _repository(path: Path) -> TaskResumeStateRepository:
    return TaskResumeStateRepository(path, checkpoint_secret=_CHECKPOINT_SECRET)


def _create_binding(
    repository: TaskResumeStateRepository,
    conversation_id: str = "conversation-a",
):
    return repository.create(
        conversation_id=conversation_id,
        task_ref="demo-task",
        task_descriptor="Подготовить локальную демонстрацию PDF RAG.",
        checkpoint_id=f"checkpoint-{conversation_id}",
    )


def test_binding_survives_restart_with_stable_installation_scope(
    tmp_path: Path,
) -> None:
    path = tmp_path / "chat.db"
    first = _repository(path)
    binding = _create_binding(first)
    installation_id = first.installation_id

    assert binding.contract_id == TASK_RESUME_BINDING_CONTRACT_ID
    assert binding.schema_version == TASK_RESUME_STATE_SCHEMA_VERSION
    assert binding.parent_scope.tenant == installation_id
    assert binding.parent_scope.user == "local-owner"
    assert binding.parent_scope.thread == "ollama-chat:conversation-a"
    assert binding.parent_scope.kind == "ollama_chat"
    assert binding.parent_scope.correlation_id() == binding.parent_scope_correlation_id
    first.close()

    restarted = _repository(path)
    assert restarted.installation_id == installation_id
    assert restarted.load_active("conversation-a") == binding
    assert restarted.parent_scope_for("conversation-a") == binding.parent_scope
    restarted.close()

    # The raw host secret does not occur in the additive SQLite state.  The
    # repository stores only HMAC tags calculated with a derived key.
    assert _CHECKPOINT_SECRET not in path.read_bytes()


def test_additive_state_schema_preserves_an_existing_chat_database(tmp_path: Path) -> None:
    path = tmp_path / "chat.db"
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE conversations (id TEXT PRIMARY KEY, title TEXT NOT NULL)"
    )
    connection.execute("INSERT INTO conversations(id, title) VALUES (?, ?)", ("c1", "Demo"))
    connection.commit()
    connection.close()

    repository = _repository(path)
    _create_binding(repository, "c1")
    repository.close()

    connection = sqlite3.connect(path)
    conversation = connection.execute(
        "SELECT id, title FROM conversations WHERE id = 'c1'"
    ).fetchone()
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    connection.close()
    assert conversation == ("c1", "Demo")
    assert {_INSTALLATION_TABLE, _BINDING_TABLE}.issubset(tables)


def test_tampered_installation_and_binding_fail_closed(tmp_path: Path) -> None:
    installation_path = tmp_path / "installation-tamper.db"
    repository = _repository(installation_path)
    _create_binding(repository)
    repository.close()

    connection = sqlite3.connect(installation_path)
    connection.execute(
        f"UPDATE {_INSTALLATION_TABLE} SET installation_id = 'other-installation'"
    )
    connection.commit()
    connection.close()
    with pytest.raises(TaskResumeStateIntegrityError, match="installation integrity"):
        _repository(installation_path)

    binding_path = tmp_path / "binding-tamper.db"
    repository = _repository(binding_path)
    _create_binding(repository)
    repository.close()
    connection = sqlite3.connect(binding_path)
    connection.execute(
        f"UPDATE {_BINDING_TABLE} SET task_descriptor = 'attacker replacement'"
    )
    connection.commit()
    connection.close()

    restarted = _repository(binding_path)
    with pytest.raises(TaskResumeStateIntegrityError, match="binding integrity"):
        restarted.load_active("conversation-a")
    restarted.close()


def test_unknown_binding_schema_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "unknown-schema.db"
    repository = _repository(path)
    _create_binding(repository)
    repository.close()

    connection = sqlite3.connect(path)
    connection.execute(
        f"UPDATE {_BINDING_TABLE} SET schema_version = 99"
    )
    connection.commit()
    connection.close()

    restarted = _repository(path)
    with pytest.raises(TaskResumeStateIntegrityError, match="unsupported.*schema"):
        restarted.load_active("conversation-a")
    restarted.close()


def test_conversation_scope_isolation_rejects_even_a_signed_cross_binding(
    tmp_path: Path,
) -> None:
    path = tmp_path / "chat.db"
    repository = _repository(path)
    first = _create_binding(repository, "conversation-a")
    second = _create_binding(repository, "conversation-b")

    assert first.task_ref == second.task_ref
    assert first.parent_scope != second.parent_scope
    assert first.parent_scope_correlation_id != second.parent_scope_correlation_id
    assert repository.load_active("conversation-missing") is None

    # This simulates an operator/key-holder error, not an ordinary disk
    # attacker: the altered row has a valid state HMAC.  Scope validation must
    # still stop a task from becoming active for another conversation.
    cross_bound = replace(
        second,
        parent_scope=first.parent_scope,
        parent_scope_json=first.parent_scope_json,
        parent_scope_correlation_id=first.parent_scope_correlation_id,
    )
    connection = sqlite3.connect(path)
    connection.execute(
        f"""
        UPDATE {_BINDING_TABLE}
        SET parent_scope_json = ?, parent_scope_correlation_id = ?, integrity_tag = ?
        WHERE conversation_id = ?
        """,
        (
            cross_bound.parent_scope_json,
            cross_bound.parent_scope_correlation_id,
            repository._binding_integrity_tag(cross_bound),
            cross_bound.conversation_id,
        ),
    )
    connection.commit()
    connection.close()
    repository.close()

    restarted = _repository(path)
    with pytest.raises(TaskResumeStateIntegrityError, match="does not match its conversation"):
        restarted.load_active("conversation-b")
    restarted.close()


def test_close_lifecycle_blocks_resume_and_requires_the_closing_state(
    tmp_path: Path,
) -> None:
    path = tmp_path / "chat.db"
    repository = _repository(path)
    binding = _create_binding(repository)

    with pytest.raises(TaskResumeStateLifecycleError, match="must be closing"):
        repository.finish_close("conversation-a")

    closing = repository.begin_close(
        "conversation-a",
        expected_generation=binding.generation,
    )
    assert closing is not None
    assert closing.state is TaskResumeBindingState.CLOSING
    assert closing.generation == binding.generation + 1
    assert repository.load_active("conversation-a") is None
    assert repository.begin_close("conversation-a") == closing

    with pytest.raises(TaskResumeStateConflictError, match="already exists"):
        _create_binding(repository)
    with pytest.raises(TaskResumeStateConflictError, match="generation"):
        repository.finish_close(
            "conversation-a",
            expected_generation=binding.generation,
        )

    repository.close()
    restarted = _repository(path)
    assert restarted.finish_close(
        "conversation-a",
        expected_generation=closing.generation,
    )
    assert restarted.load_active("conversation-a") is None
    assert not restarted.finish_close("conversation-a")
    restarted.close()
