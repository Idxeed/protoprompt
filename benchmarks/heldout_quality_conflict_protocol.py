"""Local-only held-out quality/conflict evidence protocol scaffold.

This module does not run a model, an embedding service, a provider, or a
network request.  It scores two *operator-supplied* bounded fact-selection
runs against a deterministic synthetic fixture.  The narrow result is useful
for a future memory-first release gate, but is not model-answer quality,
general retrieval quality, or a comparison with another framework.

The checked-in fixture is reproducible, not magically secret.  A report is
only called an operator-attested held-out observation when the submitted runs
attest that the fixture was frozen and withheld from the policy author before
the runs were produced.  This program can verify hashes and structure, not
human process claims.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import sys
from typing import Any


PROTOCOL_ID = "protoprompt-local-heldout-quality-conflict-v1"
REPORT_SCHEMA_VERSION = 1
RUN_SCHEMA_VERSION = 1
FIXTURE_SCHEMA_VERSION = 1
SCORING_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 1

DELAYED_RECALL_IMPROVEMENT_TARGET_PERCENTAGE_POINTS = 15.0
CONFLICT_FACT_RATE_TARGET_PERCENT = 2.0

_FIXTURE_DIRECTORY = Path(__file__).resolve().with_name("fixtures") / "quality-conflict-heldout-v1"
_FIXTURE_FILENAME = "suite.json"
_SCORING_FILENAME = "scoring.json"
_MANIFEST_FILENAME = "manifest.json"


class QualityConflictProtocolError(RuntimeError):
    """Raised when a protocol report cannot be built safely."""


class FixtureValidationError(ValueError):
    """Raised when a versioned protocol fixture is malformed or drifted."""


class SubmittedRunError(ValueError):
    """Raised when an operator-supplied baseline or candidate run is invalid."""


def canonical_json(value: object) -> str:
    """Return stable JSON used for every protocol fingerprint."""

    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def canonical_sha256(value: object) -> str:
    """Return the SHA-256 of canonical JSON-safe protocol data."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _read_json(path: Path, *, error_type: type[ValueError]) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise error_type(f"JSON file does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise error_type(f"JSON file is invalid: {path}: {exc.msg}") from exc


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


def _require_exact_keys(
    value: Mapping[str, object],
    *,
    expected: set[str],
    field: str,
    error_type: type[ValueError],
) -> None:
    actual = set(value)
    if actual == expected:
        return
    details: list[str] = []
    missing = sorted(expected - actual)
    unexpected = sorted(actual - expected)
    if missing:
        details.append(f"missing {', '.join(missing)}")
    if unexpected:
        details.append(f"unexpected {', '.join(unexpected)}")
    raise error_type(f"{field} must contain exact fields ({'; '.join(details)})")


def _require_mapping(value: object, *, field: str, error_type: type[ValueError]) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise error_type(f"{field} must be a JSON object")
    return dict(value)


def _require_string(
    value: object,
    *,
    field: str,
    error_type: type[ValueError],
    forbid_template: bool = False,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise error_type(f"{field} must be a non-empty string")
    normalized = value.strip()
    if forbid_template and ("REPLACE" in normalized.upper() or "<" in normalized or ">" in normalized):
        raise error_type(f"{field} still contains a template placeholder")
    return normalized


def _require_positive_int(value: object, *, field: str, error_type: type[ValueError]) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise error_type(f"{field} must be a positive integer")
    return value


def _require_exact_bool(value: object, *, field: str, expected: bool, error_type: type[ValueError]) -> bool:
    if value is not expected:
        raise error_type(f"{field} must be {str(expected).lower()}")
    return expected


def _require_sha256(value: object, *, field: str, error_type: type[ValueError]) -> str:
    normalized = _require_string(value, field=field, error_type=error_type, forbid_template=True)
    if len(normalized) != 64 or any(character not in "0123456789abcdef" for character in normalized):
        raise error_type(f"{field} must be a lowercase SHA-256 hex digest")
    return normalized


@dataclass(frozen=True, slots=True)
class SyntheticCase:
    """One materialized, bounded synthetic fact-selection case."""

    case_id: str
    category: str
    history_turns: int
    token_budget: int
    fact_token_cost: int
    available_fact_ids: tuple[str, ...]
    target_fact_id: str | None
    authoritative_fact_id: str | None
    contradictory_fact_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProtocolMaterial:
    """Validated fixture/scoring contract and its materialized corpus."""

    fixture: dict[str, object]
    scoring: dict[str, object]
    manifest: dict[str, object]
    fixture_sha256: str
    scoring_sha256: str
    corpus_sha256: str
    cases: tuple[SyntheticCase, ...]


@dataclass(frozen=True, slots=True)
class CaseSelection:
    """One submitted selection after strict fixture/budget validation."""

    case_id: str
    selected_fact_ids: tuple[str, ...]
    answer_fact_id: str


@dataclass(frozen=True, slots=True)
class SubmittedRun:
    """Normalized, operator-attested input for one side of a comparison."""

    run_id: str
    role: str
    system_id: str
    policy_id: str
    policy_version: str
    policy_sha256: str
    corpus_id: str
    corpus_sha256: str
    selections: dict[str, CaseSelection]
    run_sha256: str


def _seed_offset(seed: str) -> int:
    return int(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:8], 16)


def validate_fixture(value: object) -> dict[str, object]:
    """Validate the small, versioned declarative synthetic fixture."""

    fixture = _require_mapping(value, field="fixture", error_type=FixtureValidationError)
    _require_exact_keys(
        fixture,
        expected={
            "schema_version",
            "protocol_id",
            "fixture_id",
            "fixture_version",
            "generator_id",
            "generator_seed",
            "delayed_recall",
            "conflict",
        },
        field="fixture",
        error_type=FixtureValidationError,
    )
    if fixture["schema_version"] != FIXTURE_SCHEMA_VERSION:
        raise FixtureValidationError("unsupported fixture schema_version")
    if fixture["protocol_id"] != PROTOCOL_ID:
        raise FixtureValidationError("fixture protocol_id does not match")
    for field in ("fixture_id", "fixture_version", "generator_id", "generator_seed"):
        fixture[field] = _require_string(
            fixture[field], field=f"fixture.{field}", error_type=FixtureValidationError
        )

    delayed = _require_mapping(
        fixture["delayed_recall"], field="fixture.delayed_recall", error_type=FixtureValidationError
    )
    _require_exact_keys(
        delayed,
        expected={"case_count", "history_turns", "filler_fact_count", "fact_token_cost", "token_budget"},
        field="fixture.delayed_recall",
        error_type=FixtureValidationError,
    )
    conflict = _require_mapping(
        fixture["conflict"], field="fixture.conflict", error_type=FixtureValidationError
    )
    _require_exact_keys(
        conflict,
        expected={"case_count", "evidence_fact_count", "fact_token_cost", "token_budget"},
        field="fixture.conflict",
        error_type=FixtureValidationError,
    )
    for field in delayed:
        delayed[field] = _require_positive_int(
            delayed[field], field=f"fixture.delayed_recall.{field}", error_type=FixtureValidationError
        )
    for field in conflict:
        conflict[field] = _require_positive_int(
            conflict[field], field=f"fixture.conflict.{field}", error_type=FixtureValidationError
        )

    if delayed["case_count"] < 20:
        raise FixtureValidationError("fixture.delayed_recall.case_count must be at least 20")
    if delayed["history_turns"] < 32:
        raise FixtureValidationError("fixture.delayed_recall.history_turns must be at least 32")
    if delayed["filler_fact_count"] < 3:
        raise FixtureValidationError("fixture.delayed_recall.filler_fact_count must be at least 3")
    if delayed["token_budget"] < delayed["fact_token_cost"]:
        raise FixtureValidationError("fixture.delayed_recall.token_budget cannot exclude every fact")
    if delayed["token_budget"] >= delayed["fact_token_cost"] * (delayed["filler_fact_count"] + 1):
        raise FixtureValidationError("fixture.delayed_recall.token_budget must prevent selecting every fact")

    if conflict["case_count"] < 50:
        raise FixtureValidationError("fixture.conflict.case_count must be at least 50")
    if conflict["evidence_fact_count"] < 4:
        raise FixtureValidationError("fixture.conflict.evidence_fact_count must be at least 4")
    if conflict["token_budget"] < conflict["fact_token_cost"]:
        raise FixtureValidationError("fixture.conflict.token_budget cannot exclude every fact")
    if conflict["token_budget"] >= conflict["fact_token_cost"] * conflict["evidence_fact_count"]:
        raise FixtureValidationError("fixture.conflict.token_budget must prevent selecting every fact")

    fixture["delayed_recall"] = delayed
    fixture["conflict"] = conflict
    return fixture


def validate_scoring(value: object) -> dict[str, object]:
    """Validate the immutable metric definitions and roadmap goals."""

    scoring = _require_mapping(value, field="scoring", error_type=FixtureValidationError)
    _require_exact_keys(
        scoring,
        expected={
            "schema_version",
            "protocol_id",
            "scoring_id",
            "scoring_version",
            "delayed_recall",
            "conflict",
            "roadmap_goals",
        },
        field="scoring",
        error_type=FixtureValidationError,
    )
    if scoring["schema_version"] != SCORING_SCHEMA_VERSION:
        raise FixtureValidationError("unsupported scoring schema_version")
    if scoring["protocol_id"] != PROTOCOL_ID:
        raise FixtureValidationError("scoring protocol_id does not match")
    for field in ("scoring_id", "scoring_version"):
        scoring[field] = _require_string(
            scoring[field], field=f"scoring.{field}", error_type=FixtureValidationError
        )
    delayed = _require_mapping(
        scoring["delayed_recall"], field="scoring.delayed_recall", error_type=FixtureValidationError
    )
    conflict = _require_mapping(
        scoring["conflict"], field="scoring.conflict", error_type=FixtureValidationError
    )
    goals = _require_mapping(
        scoring["roadmap_goals"], field="scoring.roadmap_goals", error_type=FixtureValidationError
    )
    _require_exact_keys(
        delayed,
        expected={"metric_id", "success_rule", "denominator"},
        field="scoring.delayed_recall",
        error_type=FixtureValidationError,
    )
    _require_exact_keys(
        conflict,
        expected={"metric_id", "event_rule", "denominator"},
        field="scoring.conflict",
        error_type=FixtureValidationError,
    )
    _require_exact_keys(
        goals,
        expected={"delayed_recall_improvement_percentage_points", "conflict_fact_rate_percent_maximum"},
        field="scoring.roadmap_goals",
        error_type=FixtureValidationError,
    )
    for field in ("metric_id", "success_rule", "denominator"):
        delayed[field] = _require_string(
            delayed[field], field=f"scoring.delayed_recall.{field}", error_type=FixtureValidationError
        )
    for field in ("metric_id", "event_rule", "denominator"):
        conflict[field] = _require_string(
            conflict[field], field=f"scoring.conflict.{field}", error_type=FixtureValidationError
        )
    delayed_goal = goals["delayed_recall_improvement_percentage_points"]
    conflict_goal = goals["conflict_fact_rate_percent_maximum"]
    if isinstance(delayed_goal, bool) or not isinstance(delayed_goal, (int, float)):
        raise FixtureValidationError("delayed recall roadmap goal must be a number")
    if isinstance(conflict_goal, bool) or not isinstance(conflict_goal, (int, float)):
        raise FixtureValidationError("conflict roadmap goal must be a number")
    if float(delayed_goal) != DELAYED_RECALL_IMPROVEMENT_TARGET_PERCENTAGE_POINTS:
        raise FixtureValidationError("delayed recall roadmap goal does not match the v1 protocol")
    if float(conflict_goal) != CONFLICT_FACT_RATE_TARGET_PERCENT:
        raise FixtureValidationError("conflict roadmap goal does not match the v1 protocol")
    scoring["delayed_recall"] = delayed
    scoring["conflict"] = conflict
    scoring["roadmap_goals"] = {
        "delayed_recall_improvement_percentage_points": float(delayed_goal),
        "conflict_fact_rate_percent_maximum": float(conflict_goal),
    }
    return scoring


def materialize_cases(fixture: Mapping[str, object]) -> tuple[SyntheticCase, ...]:
    """Materialize deterministic record identifiers from the frozen fixture.

    The selection task is intentionally structural: no natural-language model
    answer is generated or judged.  Opaque synthetic fact identifiers and a
    fixed budget make changes to selection policy observable while keeping the
    repository fixture compact and reproducible.
    """

    normalized = validate_fixture(fixture)
    delayed = normalized["delayed_recall"]
    conflict = normalized["conflict"]
    assert isinstance(delayed, Mapping)
    assert isinstance(conflict, Mapping)
    seed_offset = _seed_offset(str(normalized["generator_seed"]))
    cases: list[SyntheticCase] = []

    delayed_case_count = int(delayed["case_count"])
    delayed_fact_count = int(delayed["filler_fact_count"]) + 1
    for ordinal in range(1, delayed_case_count + 1):
        case_id = f"delayed-{ordinal:03d}"
        available = tuple(f"{case_id}-fact-{slot:02d}" for slot in range(1, delayed_fact_count + 1))
        target_index = (seed_offset + ordinal * 7) % delayed_fact_count
        cases.append(
            SyntheticCase(
                case_id=case_id,
                category="delayed_recall",
                history_turns=int(delayed["history_turns"]),
                token_budget=int(delayed["token_budget"]),
                fact_token_cost=int(delayed["fact_token_cost"]),
                available_fact_ids=available,
                target_fact_id=available[target_index],
                authoritative_fact_id=None,
                contradictory_fact_ids=(),
            )
        )

    conflict_case_count = int(conflict["case_count"])
    conflict_fact_count = int(conflict["evidence_fact_count"])
    for ordinal in range(1, conflict_case_count + 1):
        case_id = f"conflict-{ordinal:03d}"
        available = tuple(f"{case_id}-evidence-{slot:02d}" for slot in range(1, conflict_fact_count + 1))
        authoritative_index = (seed_offset + ordinal * 5) % conflict_fact_count
        # The next two evidence items represent stale/revoked alternatives;
        # at least one remaining item is an unrelated but valid distractor.
        contradictory_indices = (
            (authoritative_index + 1) % conflict_fact_count,
            (authoritative_index + 2) % conflict_fact_count,
        )
        cases.append(
            SyntheticCase(
                case_id=case_id,
                category="conflict",
                history_turns=0,
                token_budget=int(conflict["token_budget"]),
                fact_token_cost=int(conflict["fact_token_cost"]),
                available_fact_ids=available,
                target_fact_id=None,
                authoritative_fact_id=available[authoritative_index],
                contradictory_fact_ids=tuple(available[index] for index in contradictory_indices),
            )
        )
    return tuple(cases)


def _corpus_shape(cases: Sequence[SyntheticCase]) -> list[dict[str, object]]:
    return [
        {
            "case_id": case.case_id,
            "category": case.category,
            "history_turns": case.history_turns,
            "token_budget": case.token_budget,
            "fact_token_cost": case.fact_token_cost,
            "available_fact_ids": list(case.available_fact_ids),
            "target_fact_id": case.target_fact_id,
            "authoritative_fact_id": case.authoritative_fact_id,
            "contradictory_fact_ids": list(case.contradictory_fact_ids),
        }
        for case in cases
    ]


def corpus_sha256(cases: Sequence[SyntheticCase]) -> str:
    """Fingerprint the materialized synthetic corpus and gold relationships."""

    return canonical_sha256(_corpus_shape(cases))


def _validate_manifest(
    value: object,
    *,
    fixture_sha256: str,
    scoring_sha256: str,
    materialized_corpus_sha256: str,
) -> dict[str, object]:
    manifest = _require_mapping(value, field="manifest", error_type=FixtureValidationError)
    _require_exact_keys(
        manifest,
        expected={
            "schema_version",
            "protocol_id",
            "suite_kind",
            "suite_version",
            "fixture_file",
            "fixture_sha256",
            "scoring_file",
            "scoring_sha256",
            "fixture_corpus_sha256",
        },
        field="manifest",
        error_type=FixtureValidationError,
    )
    if manifest["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise FixtureValidationError("unsupported manifest schema_version")
    if manifest["protocol_id"] != PROTOCOL_ID:
        raise FixtureValidationError("manifest protocol_id does not match")
    if manifest["fixture_file"] != _FIXTURE_FILENAME or manifest["scoring_file"] != _SCORING_FILENAME:
        raise FixtureValidationError("manifest must name the v1 fixture and scoring files")
    for field in ("suite_kind", "suite_version"):
        manifest[field] = _require_string(
            manifest[field], field=f"manifest.{field}", error_type=FixtureValidationError
        )
    expected_hashes = {
        "fixture_sha256": fixture_sha256,
        "scoring_sha256": scoring_sha256,
        "fixture_corpus_sha256": materialized_corpus_sha256,
    }
    for field, expected in expected_hashes.items():
        observed = _require_sha256(manifest[field], field=f"manifest.{field}", error_type=FixtureValidationError)
        if observed != expected:
            raise FixtureValidationError(f"manifest {field} does not match the materialized protocol")
        manifest[field] = observed
    return manifest


def load_protocol(fixture_directory: Path | None = None) -> ProtocolMaterial:
    """Load one frozen protocol version and reject file/generator drift."""

    directory = _FIXTURE_DIRECTORY if fixture_directory is None else fixture_directory
    fixture = validate_fixture(_read_json(directory / _FIXTURE_FILENAME, error_type=FixtureValidationError))
    scoring = validate_scoring(_read_json(directory / _SCORING_FILENAME, error_type=FixtureValidationError))
    cases = materialize_cases(fixture)
    fixture_hash = canonical_sha256(fixture)
    scoring_hash = canonical_sha256(scoring)
    materialized_corpus_hash = corpus_sha256(cases)
    manifest = _validate_manifest(
        _read_json(directory / _MANIFEST_FILENAME, error_type=FixtureValidationError),
        fixture_sha256=fixture_hash,
        scoring_sha256=scoring_hash,
        materialized_corpus_sha256=materialized_corpus_hash,
    )
    return ProtocolMaterial(
        fixture=fixture,
        scoring=scoring,
        manifest=manifest,
        fixture_sha256=fixture_hash,
        scoring_sha256=scoring_hash,
        corpus_sha256=materialized_corpus_hash,
        cases=cases,
    )


def describe_protocol(material: ProtocolMaterial) -> dict[str, object]:
    """Describe a fixture without pretending an empirical comparison exists."""

    delayed_cases = sum(case.category == "delayed_recall" for case in material.cases)
    conflict_cases = sum(case.category == "conflict" for case in material.cases)
    return {
        "protocol_id": PROTOCOL_ID,
        "status": "protocol_scaffold_not_run",
        "empirical_result": "not_available",
        "fixture": {
            "fixture_id": material.fixture["fixture_id"],
            "fixture_version": material.fixture["fixture_version"],
            "fixture_sha256": material.fixture_sha256,
            "fixture_corpus_sha256": material.corpus_sha256,
            "delayed_recall_case_count": delayed_cases,
            "conflict_case_count": conflict_cases,
        },
        "scoring": {
            "scoring_id": material.scoring["scoring_id"],
            "scoring_version": material.scoring["scoring_version"],
            "scoring_sha256": material.scoring_sha256,
        },
        "roadmap_goals_only": {
            "delayed_recall_improvement_percentage_points": DELAYED_RECALL_IMPROVEMENT_TARGET_PERCENTAGE_POINTS,
            "conflict_fact_rate_percent_maximum": CONFLICT_FACT_RATE_TARGET_PERCENT,
        },
        "requires_before_scoring": [
            "one completed operator-attested baseline run",
            "one completed operator-attested candidate run",
            "the same frozen fixture corpus hash and per-case budgets",
        ],
        "not_claimed": [
            "model answer quality",
            "general semantic recall",
            "universal conflict resolution",
            "framework comparison",
            "runtime or throughput",
        ],
    }


def run_template(material: ProtocolMaterial, *, role: str) -> dict[str, object]:
    """Return an intentionally non-runnable template for one submitted run."""

    if role not in {"baseline", "candidate"}:
        raise ValueError("role must be baseline or candidate")
    return {
        "schema_version": RUN_SCHEMA_VERSION,
        "protocol_id": PROTOCOL_ID,
        "run_id": f"REPLACE_WITH_{role.upper()}_RUN_ID",
        "role": role,
        "fixture_sha256": material.fixture_sha256,
        "fixture_corpus_sha256": material.corpus_sha256,
        "corpus": {
            "corpus_id": material.fixture["fixture_id"],
            "corpus_sha256": material.corpus_sha256,
        },
        "system": {
            "system_id": f"REPLACE_WITH_{role.upper()}_SYSTEM_ID",
            "policy": {
                "policy_id": "REPLACE_WITH_POLICY_ID",
                "policy_version": "REPLACE_WITH_POLICY_VERSION",
                "configuration": {},
            },
            "policy_sha256": "REPLACE_WITH_CANONICAL_POLICY_SHA256",
        },
        "attestation": {
            "fixture_frozen_before_policy": False,
            "fixture_held_out_from_policy_author": False,
            "independent_evaluator": False,
            "no_model_calls": False,
            "no_remote_io": False,
        },
        "results": [
            {
                "case_id": case.case_id,
                "selected_fact_ids": [],
                "answer_fact_id": "REPLACE_WITH_SELECTED_FACT_ID",
            }
            for case in material.cases
        ],
    }


def _validate_attestation(value: object) -> None:
    attestation = _require_mapping(value, field="run.attestation", error_type=SubmittedRunError)
    _require_exact_keys(
        attestation,
        expected={
            "fixture_frozen_before_policy",
            "fixture_held_out_from_policy_author",
            "independent_evaluator",
            "no_model_calls",
            "no_remote_io",
        },
        field="run.attestation",
        error_type=SubmittedRunError,
    )
    for field in sorted(attestation):
        _require_exact_bool(
            attestation[field], field=f"run.attestation.{field}", expected=True, error_type=SubmittedRunError
        )


def _validate_policy(value: object) -> tuple[str, str, str]:
    system = _require_mapping(value, field="run.system", error_type=SubmittedRunError)
    _require_exact_keys(
        system,
        expected={"system_id", "policy", "policy_sha256"},
        field="run.system",
        error_type=SubmittedRunError,
    )
    system_id = _require_string(
        system["system_id"], field="run.system.system_id", error_type=SubmittedRunError, forbid_template=True
    )
    policy = _require_mapping(system["policy"], field="run.system.policy", error_type=SubmittedRunError)
    _require_exact_keys(
        policy,
        expected={"policy_id", "policy_version", "configuration"},
        field="run.system.policy",
        error_type=SubmittedRunError,
    )
    policy_id = _require_string(
        policy["policy_id"], field="run.system.policy.policy_id", error_type=SubmittedRunError, forbid_template=True
    )
    policy_version = _require_string(
        policy["policy_version"],
        field="run.system.policy.policy_version",
        error_type=SubmittedRunError,
        forbid_template=True,
    )
    configuration = _require_mapping(
        policy["configuration"], field="run.system.policy.configuration", error_type=SubmittedRunError
    )
    policy = {
        "policy_id": policy_id,
        "policy_version": policy_version,
        "configuration": configuration,
    }
    policy_hash = _require_sha256(
        system["policy_sha256"], field="run.system.policy_sha256", error_type=SubmittedRunError
    )
    if policy_hash != canonical_sha256(policy):
        raise SubmittedRunError("run.system.policy_sha256 does not match canonical policy")
    return system_id, policy_id, policy_version


def validate_submitted_run(
    value: object,
    material: ProtocolMaterial,
    *,
    expected_role: str | None = None,
) -> SubmittedRun:
    """Validate one complete result set against exact fixture/budget contracts."""

    run = _require_mapping(value, field="run", error_type=SubmittedRunError)
    _require_exact_keys(
        run,
        expected={
            "schema_version",
            "protocol_id",
            "run_id",
            "role",
            "fixture_sha256",
            "fixture_corpus_sha256",
            "corpus",
            "system",
            "attestation",
            "results",
        },
        field="run",
        error_type=SubmittedRunError,
    )
    if run["schema_version"] != RUN_SCHEMA_VERSION:
        raise SubmittedRunError("unsupported run schema_version")
    if run["protocol_id"] != PROTOCOL_ID:
        raise SubmittedRunError("run protocol_id does not match")
    run_id = _require_string(run["run_id"], field="run.run_id", error_type=SubmittedRunError, forbid_template=True)
    role = _require_string(run["role"], field="run.role", error_type=SubmittedRunError, forbid_template=True)
    if role not in {"baseline", "candidate"}:
        raise SubmittedRunError("run.role must be baseline or candidate")
    if expected_role is not None and role != expected_role:
        raise SubmittedRunError(f"run.role must be {expected_role}")
    fixture_hash = _require_sha256(
        run["fixture_sha256"], field="run.fixture_sha256", error_type=SubmittedRunError
    )
    corpus_hash = _require_sha256(
        run["fixture_corpus_sha256"], field="run.fixture_corpus_sha256", error_type=SubmittedRunError
    )
    if fixture_hash != material.fixture_sha256:
        raise SubmittedRunError("run.fixture_sha256 does not match the frozen fixture")
    if corpus_hash != material.corpus_sha256:
        raise SubmittedRunError("run.fixture_corpus_sha256 does not match the materialized corpus")
    corpus = _require_mapping(run["corpus"], field="run.corpus", error_type=SubmittedRunError)
    _require_exact_keys(
        corpus,
        expected={"corpus_id", "corpus_sha256"},
        field="run.corpus",
        error_type=SubmittedRunError,
    )
    corpus_id = _require_string(
        corpus["corpus_id"], field="run.corpus.corpus_id", error_type=SubmittedRunError, forbid_template=True
    )
    submitted_corpus_hash = _require_sha256(
        corpus["corpus_sha256"], field="run.corpus.corpus_sha256", error_type=SubmittedRunError
    )
    if corpus_id != material.fixture["fixture_id"] or submitted_corpus_hash != material.corpus_sha256:
        raise SubmittedRunError("run.corpus does not identify the exact frozen synthetic corpus")
    _validate_attestation(run["attestation"])
    system_id, policy_id, policy_version = _validate_policy(run["system"])
    system = _require_mapping(run["system"], field="run.system", error_type=SubmittedRunError)
    policy_hash = _require_sha256(
        system["policy_sha256"], field="run.system.policy_sha256", error_type=SubmittedRunError
    )

    raw_results = run["results"]
    if not isinstance(raw_results, list):
        raise SubmittedRunError("run.results must be a JSON array")
    expected_cases = {case.case_id: case for case in material.cases}
    if len(raw_results) != len(expected_cases):
        raise SubmittedRunError("run.results must include exactly one result for every fixture case")
    selections: dict[str, CaseSelection] = {}
    for raw_result in raw_results:
        result = _require_mapping(raw_result, field="run.results[]", error_type=SubmittedRunError)
        _require_exact_keys(
            result,
            expected={"case_id", "selected_fact_ids", "answer_fact_id"},
            field="run.results[]",
            error_type=SubmittedRunError,
        )
        case_id = _require_string(
            result["case_id"], field="run.results[].case_id", error_type=SubmittedRunError, forbid_template=True
        )
        case = expected_cases.get(case_id)
        if case is None:
            raise SubmittedRunError(f"run.results contains unknown case_id {case_id!r}")
        if case_id in selections:
            raise SubmittedRunError(f"run.results repeats case_id {case_id!r}")
        raw_selected = result["selected_fact_ids"]
        if not isinstance(raw_selected, list):
            raise SubmittedRunError("run.results[].selected_fact_ids must be a JSON array")
        selected = tuple(
            _require_string(
                value,
                field="run.results[].selected_fact_ids[]",
                error_type=SubmittedRunError,
                forbid_template=True,
            )
            for value in raw_selected
        )
        if not selected:
            raise SubmittedRunError("run.results[].selected_fact_ids must not be empty")
        if len(set(selected)) != len(selected):
            raise SubmittedRunError("run.results[].selected_fact_ids must not contain duplicates")
        unknown_fact_ids = sorted(set(selected) - set(case.available_fact_ids))
        if unknown_fact_ids:
            raise SubmittedRunError(
                f"run.results[].selected_fact_ids contains unknown facts for {case_id}: "
                f"{', '.join(unknown_fact_ids)}"
            )
        if len(selected) * case.fact_token_cost > case.token_budget:
            raise SubmittedRunError(f"run.results for {case_id} exceeds the fixture token budget")
        answer_fact_id = _require_string(
            result["answer_fact_id"],
            field="run.results[].answer_fact_id",
            error_type=SubmittedRunError,
            forbid_template=True,
        )
        if answer_fact_id not in selected:
            raise SubmittedRunError("run.results[].answer_fact_id must be one selected fact")
        selections[case_id] = CaseSelection(
            case_id=case_id,
            selected_fact_ids=selected,
            answer_fact_id=answer_fact_id,
        )
    if set(selections) != set(expected_cases):  # defensive after exact length validation
        raise SubmittedRunError("run.results does not cover every fixture case")
    return SubmittedRun(
        run_id=run_id,
        role=role,
        system_id=system_id,
        policy_id=policy_id,
        policy_version=policy_version,
        policy_sha256=policy_hash,
        corpus_id=corpus_id,
        corpus_sha256=submitted_corpus_hash,
        selections=selections,
        run_sha256=canonical_sha256(run),
    )


def _percent(numerator: int, denominator: int) -> float:
    if denominator < 1:
        raise QualityConflictProtocolError("a protocol metric has no denominator")
    return round(numerator * 100.0 / denominator, 6)


def score_submitted_run(run: SubmittedRun, material: ProtocolMaterial) -> dict[str, object]:
    """Score structural evidence selection only; no natural-language answer is judged."""

    delayed_cases = [case for case in material.cases if case.category == "delayed_recall"]
    conflict_cases = [case for case in material.cases if case.category == "conflict"]
    delayed_successes = sum(
        case.target_fact_id in run.selections[case.case_id].selected_fact_ids for case in delayed_cases
    )
    conflict_events = sum(
        bool(set(run.selections[case.case_id].selected_fact_ids) & set(case.contradictory_fact_ids))
        for case in conflict_cases
    )
    authoritative_answers = sum(
        run.selections[case.case_id].answer_fact_id == case.authoritative_fact_id for case in conflict_cases
    )
    return {
        "run_id": run.run_id,
        "role": run.role,
        "system": {
            "system_id": run.system_id,
            "policy_id": run.policy_id,
            "policy_version": run.policy_version,
            "policy_sha256": run.policy_sha256,
            "run_sha256": run.run_sha256,
        },
        "delayed_recall": {
            "case_count": len(delayed_cases),
            "target_selected_case_count": delayed_successes,
            "target_selection_rate_percent": _percent(delayed_successes, len(delayed_cases)),
        },
        "conflict": {
            "case_count": len(conflict_cases),
            "contradictory_selection_case_count": conflict_events,
            "conflict_fact_rate_percent": _percent(conflict_events, len(conflict_cases)),
            "authoritative_answer_case_count": authoritative_answers,
            "authoritative_answer_rate_percent": _percent(authoritative_answers, len(conflict_cases)),
        },
    }


def build_report(
    *,
    baseline_run: object,
    candidate_run: object,
    material: ProtocolMaterial,
) -> dict[str, object]:
    """Compare one baseline/candidate pair after all reproducibility checks."""

    baseline = validate_submitted_run(baseline_run, material, expected_role="baseline")
    candidate = validate_submitted_run(candidate_run, material, expected_role="candidate")
    if baseline.corpus_id != candidate.corpus_id or baseline.corpus_sha256 != candidate.corpus_sha256:
        raise QualityConflictProtocolError("baseline and candidate must identify the same corpus")
    baseline_score = score_submitted_run(baseline, material)
    candidate_score = score_submitted_run(candidate, material)
    baseline_delayed = baseline_score["delayed_recall"]
    candidate_delayed = candidate_score["delayed_recall"]
    candidate_conflict = candidate_score["conflict"]
    assert isinstance(baseline_delayed, Mapping)
    assert isinstance(candidate_delayed, Mapping)
    assert isinstance(candidate_conflict, Mapping)
    improvement = round(
        float(candidate_delayed["target_selection_rate_percent"])
        - float(baseline_delayed["target_selection_rate_percent"]),
        6,
    )
    conflict_rate = float(candidate_conflict["conflict_fact_rate_percent"])
    delayed_goal_status = (
        "observed_pass"
        if improvement + 1e-9 >= DELAYED_RECALL_IMPROVEMENT_TARGET_PERCENTAGE_POINTS
        else "observed_fail"
    )
    conflict_goal_status = (
        "observed_pass"
        if conflict_rate <= CONFLICT_FACT_RATE_TARGET_PERCENT + 1e-9
        else "observed_fail"
    )
    overall_status = (
        "operator_attested_observed_pass"
        if delayed_goal_status == "observed_pass" and conflict_goal_status == "observed_pass"
        else "operator_attested_observed_fail"
    )
    return {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "protocol_id": PROTOCOL_ID,
        "status": overall_status,
        "fixture": {
            "fixture_id": material.fixture["fixture_id"],
            "fixture_version": material.fixture["fixture_version"],
            "fixture_sha256": material.fixture_sha256,
            "fixture_corpus_sha256": material.corpus_sha256,
            "manifest_sha256": canonical_sha256(material.manifest),
        },
        "scoring": {
            "scoring_id": material.scoring["scoring_id"],
            "scoring_version": material.scoring["scoring_version"],
            "scoring_sha256": material.scoring_sha256,
        },
        "methodology": {
            "synthetic": True,
            "model_calls_by_protocol": False,
            "embedding_calls_by_protocol": False,
            "remote_io_by_protocol": False,
            "scored_surface": "bounded structured fact selection",
            "same_corpus_required": True,
            "same_per_case_budget_required": True,
            "operator_attestation_required": True,
        },
        "submitted_runs": {
            "baseline": baseline_score,
            "candidate": candidate_score,
        },
        "roadmap_goals": {
            "delayed_recall_improvement": {
                "target_percentage_points": DELAYED_RECALL_IMPROVEMENT_TARGET_PERCENTAGE_POINTS,
                "observed_percentage_points": improvement,
                "status": delayed_goal_status,
                "baseline_metric": "target_selection_rate_percent",
                "candidate_metric": "target_selection_rate_percent",
            },
            "conflict_fact_rate": {
                "target_percent_maximum": CONFLICT_FACT_RATE_TARGET_PERCENT,
                "observed_percent": conflict_rate,
                "status": conflict_goal_status,
                "metric": "conflict-case has one or more contradictory facts in bounded selection",
            },
        },
        "interpretation": {
            "evidence_scope": "operator-attested observation over the submitted synthetic fact-selection runs",
            "model_quality_claim": "not_made",
            "universal_claim": "not_made",
            "heldout_caveat": (
                "the checked-in fixture is reproducible rather than secret; the program verifies "
                "hashes and self-attestation but cannot independently prove the fixture was withheld"
            ),
            "not_claimed": [
                "natural-language answer quality",
                "general semantic recall",
                "production conflict handling",
                "prompt-injection resistance",
                "latency or throughput",
                "comparison with another framework",
            ],
        },
    }


def render_markdown_report(report: Mapping[str, object]) -> str:
    """Render a compact report without raw fact selections or policy config."""

    if report.get("protocol_id") != PROTOCOL_ID:
        raise QualityConflictProtocolError("cannot render a report from another protocol")
    fixture = report.get("fixture")
    submitted_runs = report.get("submitted_runs")
    goals = report.get("roadmap_goals")
    interpretation = report.get("interpretation")
    if not all(isinstance(value, Mapping) for value in (fixture, submitted_runs, goals, interpretation)):
        raise QualityConflictProtocolError("quality/conflict report has an invalid shape")
    assert isinstance(fixture, Mapping)
    assert isinstance(submitted_runs, Mapping)
    assert isinstance(goals, Mapping)
    assert isinstance(interpretation, Mapping)
    baseline = submitted_runs.get("baseline")
    candidate = submitted_runs.get("candidate")
    delayed_goal = goals.get("delayed_recall_improvement")
    conflict_goal = goals.get("conflict_fact_rate")
    if not all(isinstance(value, Mapping) for value in (baseline, candidate, delayed_goal, conflict_goal)):
        raise QualityConflictProtocolError("quality/conflict report is missing a score")
    assert isinstance(baseline, Mapping)
    assert isinstance(candidate, Mapping)
    assert isinstance(delayed_goal, Mapping)
    assert isinstance(conflict_goal, Mapping)
    baseline_delayed = baseline.get("delayed_recall")
    candidate_delayed = candidate.get("delayed_recall")
    baseline_conflict = baseline.get("conflict")
    candidate_conflict = candidate.get("conflict")
    if not all(
        isinstance(value, Mapping)
        for value in (baseline_delayed, candidate_delayed, baseline_conflict, candidate_conflict)
    ):
        raise QualityConflictProtocolError("quality/conflict score sections are malformed")
    assert isinstance(baseline_delayed, Mapping)
    assert isinstance(candidate_delayed, Mapping)
    assert isinstance(baseline_conflict, Mapping)
    assert isinstance(candidate_conflict, Mapping)
    return "\n".join(
        [
            "# ProtoPrompt held-out quality/conflict protocol",
            "",
            f"Protocol: `{PROTOCOL_ID}`  ",
            f"Fixture SHA-256: `{fixture['fixture_sha256']}`  ",
            f"Materialized corpus SHA-256: `{fixture['fixture_corpus_sha256']}`  ",
            f"Status: `{report['status']}`",
            "",
            "This is an operator-attested observation over bounded synthetic fact selections. "
            "The protocol does not run a model or judge natural-language answers.",
            "",
            "## Submitted-run scores",
            "",
            "| Metric | Baseline | Candidate |",
            "|---|---:|---:|",
            "| Delayed target-selection rate | "
            f"{float(baseline_delayed['target_selection_rate_percent']):.6f}% | "
            f"{float(candidate_delayed['target_selection_rate_percent']):.6f}% |",
            "| Conflict-fact rate | "
            f"{float(baseline_conflict['conflict_fact_rate_percent']):.6f}% | "
            f"{float(candidate_conflict['conflict_fact_rate_percent']):.6f}% |",
            "| Authoritative-answer rate | "
            f"{float(baseline_conflict['authoritative_answer_rate_percent']):.6f}% | "
            f"{float(candidate_conflict['authoritative_answer_rate_percent']):.6f}% |",
            "",
            "## Roadmap goals (only for this frozen protocol)",
            "",
            "| Goal | Observation | Status |",
            "|---|---:|---|",
            "| Delayed recall improvement ≥ "
            f"{float(delayed_goal['target_percentage_points']):.6f} pp | "
            f"{float(delayed_goal['observed_percentage_points']):.6f} pp | "
            f"`{delayed_goal['status']}` |",
            "| Conflict-fact rate ≤ "
            f"{float(conflict_goal['target_percent_maximum']):.6f}% | "
            f"{float(conflict_goal['observed_percent']):.6f}% | "
            f"`{conflict_goal['status']}` |",
            "",
            "## Boundary",
            "",
            "The checked-in fixture is reproducible, not inherently secret. A genuinely held-out "
            "claim still depends on the submitted independent-evaluator attestations; this program "
            "can verify hashes and bounded selections, not the human process. It makes no model "
            "quality, universal recall, production conflict-handling, runtime, or framework-comparison claim.",
            "",
        ]
    )


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Score local-only held-out synthetic memory-selection evidence; no model is run.",
    )
    parser.add_argument(
        "--fixture-dir",
        type=Path,
        default=_FIXTURE_DIRECTORY,
        help="versioned fixture directory (default: checked-in quality-conflict v1)",
    )
    parser.add_argument("--describe", action="store_true", help="print scaffold metadata without scoring")
    parser.add_argument("--init-run", type=Path, help="write an explicit non-runnable run template and exit")
    parser.add_argument("--role", choices=("baseline", "candidate"), help="required with --init-run")
    parser.add_argument("--baseline-run", type=Path, help="completed operator-attested baseline JSON")
    parser.add_argument("--candidate-run", type=Path, help="completed operator-attested candidate JSON")
    parser.add_argument("--json", type=Path, help="explicit path for the aggregate JSON report")
    parser.add_argument("--markdown", type=Path, help="explicit path for the Markdown report")
    arguments = parser.parse_args(argv)
    if arguments.describe:
        if any(
            value is not None
            for value in (
                arguments.init_run,
                arguments.role,
                arguments.baseline_run,
                arguments.candidate_run,
                arguments.json,
                arguments.markdown,
            )
        ):
            parser.error("--describe cannot be combined with templates, runs, or output paths")
        return arguments
    if arguments.init_run is not None:
        if arguments.role is None:
            parser.error("--role is required with --init-run")
        if any(value is not None for value in (arguments.baseline_run, arguments.candidate_run, arguments.json, arguments.markdown)):
            parser.error("--init-run cannot be combined with scoring inputs or report outputs")
        return arguments
    if arguments.role is not None:
        parser.error("--role is only valid with --init-run")
    if arguments.baseline_run is None or arguments.candidate_run is None:
        parser.error("both --baseline-run and --candidate-run are required for scoring")
    if arguments.json is None and arguments.markdown is None:
        parser.error("choose at least one explicit output path: --json or --markdown")
    return arguments


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; it writes only requested local template/report files."""

    arguments = _parse_args(argv)
    try:
        material = load_protocol(arguments.fixture_dir)
        if arguments.describe:
            print(json.dumps(describe_protocol(material), ensure_ascii=False, indent=2))
            print("protocol scaffold only; no empirical baseline/candidate result has been supplied")
            return 0
        if arguments.init_run is not None:
            _write_json(arguments.init_run, run_template(material, role=arguments.role))
            print(
                "wrote non-runnable run template; complete its policy hash, all selections, and "
                "operator attestations before scoring"
            )
            return 0
        baseline_run = _read_json(arguments.baseline_run, error_type=SubmittedRunError)
        candidate_run = _read_json(arguments.candidate_run, error_type=SubmittedRunError)
        report = build_report(baseline_run=baseline_run, candidate_run=candidate_run, material=material)
        if arguments.json is not None:
            _write_json(arguments.json, report)
        if arguments.markdown is not None:
            _write_text(arguments.markdown, render_markdown_report(report))
    except (FixtureValidationError, SubmittedRunError, QualityConflictProtocolError, ValueError) as exc:
        print(f"held-out quality/conflict protocol failed: {exc}", file=sys.stderr)
        return 1
    print(
        "held-out quality/conflict protocol scored the supplied operator-attested runs; "
        "it makes no model-quality or universal claim"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
