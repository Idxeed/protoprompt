"""Tests for the provider-safe task episode reference contract."""

from __future__ import annotations

import json

import pytest

from protoprompt.ledger.task_resume import (
    MAX_TASK_COMPLETED_ACTION_COUNT,
    MAX_TASK_GOAL_CHARS,
    TASK_EPISODE_REFERENCE_TYPE,
    TaskEpisode,
    TaskEpisodeReference,
    TaskEpisodeReferenceError,
    TaskOutcome,
    decode_task_episode_reference,
    encode_task_episode_reference,
    project_task_episode_reference,
)


def _episode() -> TaskEpisode:
    return TaskEpisode(
        task_ref="task:host-only-deployment-42",
        goal="Resume the checked deployment after host verification.",
        completed_action_refs=(
            "action:host-only-prepare",
            "artifact:host-only-manifest",
        ),
        outcome=TaskOutcome.INTERRUPTED,
        next_action="Check the host-owned deployment status before continuing.",
        lesson="Persist immutable artifact references before a restart.",
    )


def test_projection_is_canonical_and_does_not_expose_raw_identifiers():
    episode = _episode()

    reference = project_task_episode_reference(episode)
    encoded = encode_task_episode_reference(reference)

    assert reference == TaskEpisodeReference(
        goal=episode.goal,
        completed_action_count=2,
        outcome=TaskOutcome.INTERRUPTED,
        next_action=episode.next_action,
        lesson=episode.lesson,
    )
    assert encoded == reference.to_json()
    assert encoded == (
        '{"completed_action_count":2,'
        '"goal":"Resume the checked deployment after host verification.",'
        '"kind":"episode",'
        '"lesson":"Persist immutable artifact references before a restart.",'
        '"next_action":"Check the host-owned deployment status before continuing.",'
        '"outcome":"interrupted",'
        '"schema_version":1,'
        '"type":"protoprompt.task-episode-reference"}'
    )
    assert decode_task_episode_reference(encoded) == reference
    assert episode.task_ref not in encoded
    for action_ref in episode.completed_action_refs:
        assert action_ref not in encoded
    assert "task_ref" not in reference.to_dict()
    assert "completed_action_refs" not in reference.to_dict()
    assert episode.completed_action_refs == (
        "action:host-only-prepare",
        "artifact:host-only-manifest",
    )


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"completed_action_count": True}, "completed_action_count must be an integer"),
        ({"completed_action_count": -1}, "completed_action_count must not be negative"),
        (
            {"completed_action_count": MAX_TASK_COMPLETED_ACTION_COUNT + 1},
            "completed_action_count must be at most",
        ),
        ({"goal": "x" * (MAX_TASK_GOAL_CHARS + 1)}, "goal must be at most"),
        ({"outcome": "pending"}, "'pending' is not a valid TaskOutcome"),
        ({"schema_version": True}, "schema_version must be an integer"),
        ({"schema_version": 2}, "unsupported task episode reference schema_version"),
    ],
)
def test_direct_reference_construction_is_strict_and_bounded(kwargs, match):
    values = {
        "goal": "Keep the provider data lane scoped.",
        "completed_action_count": 0,
        "outcome": TaskOutcome.SUCCEEDED,
    }
    values.update(kwargs)

    with pytest.raises((TypeError, ValueError), match=match):
        TaskEpisodeReference(**values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("task_ref", "task:must-never-cross-provider-boundary"),
        ("completed_action_refs", ["action:must-never-cross-provider-boundary"]),
        ("unexpected", True),
    ],
)
def test_decoder_fails_closed_on_raw_or_unknown_fields(field, value):
    data = project_task_episode_reference(_episode()).to_dict()
    data[field] = value

    with pytest.raises(TaskEpisodeReferenceError, match="unknown field"):
        decode_task_episode_reference(json.dumps(data))


def test_decoder_fails_closed_on_duplicate_type_or_kind_shape():
    payload = (
        '{"schema_version":1,'
        f'"type":"{TASK_EPISODE_REFERENCE_TYPE}",'
        '"kind":"episode",'
        '"goal":"One goal.",'
        '"goal":"Another goal.",'
        '"completed_action_count":0,'
        '"outcome":"succeeded",'
        '"next_action":null,'
        '"lesson":null}'
    )

    with pytest.raises(TaskEpisodeReferenceError, match="duplicate JSON field 'goal'"):
        decode_task_episode_reference(payload)

    data = project_task_episode_reference(_episode()).to_dict()
    data["type"] = "protoprompt.other-reference"
    with pytest.raises(TaskEpisodeReferenceError, match="unsupported task episode reference type"):
        decode_task_episode_reference(json.dumps(data))
    data["type"] = TASK_EPISODE_REFERENCE_TYPE
    data["kind"] = "procedure"
    with pytest.raises(TaskEpisodeReferenceError, match="unsupported task episode reference kind"):
        decode_task_episode_reference(json.dumps(data))


def test_projection_and_encoder_do_not_accept_raw_identifier_inputs():
    episode = _episode()

    with pytest.raises(TypeError, match="TaskEpisodeReference"):
        encode_task_episode_reference(episode)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="TaskEpisode"):
        project_task_episode_reference({  # type: ignore[arg-type]
            "task_ref": episode.task_ref,
            "completed_action_refs": episode.completed_action_refs,
        })
    with pytest.raises(TypeError, match="unexpected keyword argument 'task_ref'"):
        TaskEpisodeReference(  # type: ignore[call-arg]
            goal=episode.goal,
            completed_action_count=0,
            outcome=episode.outcome,
            task_ref=episode.task_ref,
        )
    with pytest.raises(
        TypeError,
        match="unexpected keyword argument 'completed_action_refs'",
    ):
        TaskEpisodeReference(  # type: ignore[call-arg]
            goal=episode.goal,
            completed_action_count=0,
            outcome=episode.outcome,
            completed_action_refs=episode.completed_action_refs,
        )
