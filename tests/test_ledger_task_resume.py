"""Tests for the host-side typed task-resume reference-data contract."""

from __future__ import annotations

import json

import pytest

import protoprompt.ledger.task_resume as task_resume
from protoprompt.ledger.task_resume import (
    MAX_PROCEDURE_STEP_CHARS,
    MAX_TASK_GOAL_CHARS,
    TaskEpisode,
    TaskOutcome,
    TaskProcedure,
    TaskResumePayloadError,
    decode_task_resume_payload,
    encode_task_resume_payload,
)


def test_episode_round_trip_is_canonical_typed_and_non_executable_data():
    episode = TaskEpisode(
        task_ref="task:deploy-42",
        goal="Resume the checked deployment after the environment is available.",
        completed_action_refs=("action:prepare", "artifact:manifest"),
        outcome="interrupted",
        next_action="Check the host-owned deployment status before continuing.",
        lesson="Persist immutable artifact references before a restart.",
    )

    encoded = encode_task_resume_payload(episode)

    assert encoded == episode.to_json()
    assert encoded == (
        '{"completed_action_refs":["action:prepare","artifact:manifest"],'
        '"goal":"Resume the checked deployment after the environment is available.",'
        '"kind":"episode","lesson":"Persist immutable artifact references before a restart.",'
        '"next_action":"Check the host-owned deployment status before continuing.",'
        '"outcome":"interrupted","schema_version":1,"task_ref":"task:deploy-42"}'
    )
    restored = decode_task_resume_payload(encoded)
    assert restored == episode
    assert restored.outcome is TaskOutcome.INTERRUPTED
    assert "not tool instructions" in (task_resume.__doc__ or "")
    assert not hasattr(task_resume, "SqliteMemoryLedger")
    assert not hasattr(task_resume, "MemoryWriter")


def test_procedure_round_trip_preserves_ordered_steps_and_episode_provenance():
    procedure = TaskProcedure(
        task_ref="task:deploy-42",
        procedure_ref="procedure:checked-deploy-v1",
        steps=[
            "Verify the host-owned deployment status.",
            "Apply the approved continuation only after verification.",
        ],
        supporting_episode_refs=["episode:31", "episode:37"],
    )

    restored = decode_task_resume_payload(procedure.to_json())

    assert restored == procedure
    assert restored.steps == (
        "Verify the host-owned deployment status.",
        "Apply the approved continuation only after verification.",
    )
    assert restored.supporting_episode_refs == ("episode:31", "episode:37")
    assert restored.to_dict()["kind"] == "procedure"


@pytest.mark.parametrize(
    ("factory", "match"),
    [
        (
            lambda: TaskEpisode(
                task_ref="task ref with spaces",
                goal="Valid goal.",
                completed_action_refs=(),
                outcome=TaskOutcome.SUCCEEDED,
            ),
            "task_ref",
        ),
        (
            lambda: TaskEpisode(
                task_ref="task:one",
                goal="x" * (MAX_TASK_GOAL_CHARS + 1),
                completed_action_refs=(),
                outcome=TaskOutcome.SUCCEEDED,
            ),
            "goal",
        ),
        (
            lambda: TaskEpisode(
                task_ref="task:one",
                goal="Valid goal.",
                completed_action_refs=("action:one", "action:one"),
                outcome=TaskOutcome.SUCCEEDED,
            ),
            "duplicates",
        ),
        (
            lambda: TaskProcedure(
                task_ref="task:one",
                procedure_ref="procedure:one",
                steps=(),
                supporting_episode_refs=("episode:one",),
            ),
            "at least one step",
        ),
        (
            lambda: TaskProcedure(
                task_ref="task:one",
                procedure_ref="procedure:one",
                steps=("Valid step.",),
                supporting_episode_refs=(),
            ),
            "at least 1 reference",
        ),
        (
            lambda: TaskProcedure(
                task_ref="task:one",
                procedure_ref="procedure:one",
                steps=("x" * (MAX_PROCEDURE_STEP_CHARS + 1),),
                supporting_episode_refs=("episode:one",),
            ),
            r"steps\[0\]",
        ),
    ],
)
def test_direct_construction_rejects_invalid_or_unbounded_data(factory, match):
    with pytest.raises((TypeError, ValueError), match=match):
        factory()


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        ("not JSON", "invalid task-resume JSON"),
        ("[]", "JSON object"),
        (
            '{"schema_version":1,"kind":"episode","task_ref":"task:one",'
            '"goal":"one","goal":"two","completed_action_refs":[],'
            '"outcome":"succeeded","next_action":null,"lesson":null}',
            "duplicate JSON field",
        ),
        ("{\"schema_version\":NaN}", "non-finite JSON constant"),
        (
            json.dumps({
                "schema_version": 2,
                "kind": "episode",
                "task_ref": "task:one",
                "goal": "Valid goal.",
                "completed_action_refs": [],
                "outcome": "succeeded",
                "next_action": None,
                "lesson": None,
            }),
            "invalid task episode payload",
        ),
        (
            json.dumps({
                "schema_version": 1,
                "kind": "episode",
                "task_ref": "task:one",
                "goal": "Valid goal.",
                "completed_action_refs": [],
                "outcome": "succeeded",
                "next_action": None,
                "lesson": None,
                "unexpected": True,
            }),
            "unknown field",
        ),
        (
            json.dumps({
                "schema_version": 1,
                "kind": "procedure",
                "task_ref": "task:one",
                "goal": "An episode field cannot be accepted as a procedure.",
                "completed_action_refs": [],
                "outcome": "succeeded",
                "next_action": None,
                "lesson": None,
            }),
            "missing required field",
        ),
        (
            json.dumps({
                "schema_version": True,
                "kind": "episode",
                "task_ref": "task:one",
                "goal": "Valid goal.",
                "completed_action_refs": [],
                "outcome": "succeeded",
                "next_action": None,
                "lesson": None,
            }),
            "invalid task episode payload",
        ),
        (
            json.dumps({
                "schema_version": 1,
                "kind": "procedure",
                "task_ref": "task:one",
                "procedure_ref": "procedure:one",
                "steps": ["Valid step."],
                "supporting_episode_refs": [],
            }),
            "invalid task procedure payload",
        ),
    ],
)
def test_decoder_fails_closed_on_unknown_or_malformed_payloads(payload, match):
    with pytest.raises(TaskResumePayloadError, match=match):
        decode_task_resume_payload(payload)


def test_decoder_rejects_unknown_kind_and_non_string_transport():
    with pytest.raises(TaskResumePayloadError, match="unsupported task-resume payload kind"):
        decode_task_resume_payload('{"schema_version":1,"kind":"fact"}')
    with pytest.raises(TypeError, match="JSON string"):
        decode_task_resume_payload(b"{}")  # type: ignore[arg-type]


def test_equivalent_json_is_normalized_to_the_canonical_wire_shape():
    raw = (
        '{ "task_ref" : "task:one", "lesson" : null, "next_action" : null, '
        '"outcome" : "succeeded", "completed_action_refs" : [], '
        '"goal" : "  Keep this task scoped.  ", "kind" : "episode", '
        '"schema_version" : 1 }'
    )

    restored = decode_task_resume_payload(raw)

    assert isinstance(restored, TaskEpisode)
    assert restored.goal == "Keep this task scoped."
    assert encode_task_resume_payload(restored) == (
        '{"completed_action_refs":[],"goal":"Keep this task scoped.",'
        '"kind":"episode","lesson":null,"next_action":null,'
        '"outcome":"succeeded","schema_version":1,"task_ref":"task:one"}'
    )
