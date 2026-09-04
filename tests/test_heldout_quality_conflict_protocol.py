"""Contract tests for the local-only held-out quality/conflict scaffold."""

from __future__ import annotations

import copy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from benchmarks.heldout_quality_conflict_protocol import (
    CONFLICT_FACT_RATE_TARGET_PERCENT,
    DELAYED_RECALL_IMPROVEMENT_TARGET_PERCENTAGE_POINTS,
    PROTOCOL_ID,
    FixtureValidationError,
    SubmittedRunError,
    build_report,
    canonical_json,
    canonical_sha256,
    describe_protocol,
    load_protocol,
    render_markdown_report,
    run_template,
    validate_submitted_run,
)


ROOT = Path(__file__).resolve().parents[1]


def _completed_run(
    *,
    role: str,
    delayed_target_case_count: int,
    contradictory_conflict_case_count: int,
) -> dict[str, object]:
    material = load_protocol()
    run = run_template(material, role=role)
    run["run_id"] = f"{role}-operator-run-v1"
    policy = {
        "policy_id": f"{role}-bounded-selection-policy",
        "policy_version": "1",
        "configuration": {"ranking": role, "budget_contract": "fixture-v1"},
    }
    run["system"] = {
        "system_id": f"{role}-system-v1",
        "policy": policy,
        "policy_sha256": canonical_sha256(policy),
    }
    run["attestation"] = {
        "fixture_frozen_before_policy": True,
        "fixture_held_out_from_policy_author": True,
        "independent_evaluator": True,
        "no_model_calls": True,
        "no_remote_io": True,
    }
    results: list[dict[str, object]] = []
    delayed_ordinal = 0
    conflict_ordinal = 0
    for case in material.cases:
        if case.category == "delayed_recall":
            delayed_ordinal += 1
            if delayed_ordinal <= delayed_target_case_count:
                selected_fact_id = case.target_fact_id
            else:
                selected_fact_id = next(
                    fact_id for fact_id in case.available_fact_ids if fact_id != case.target_fact_id
                )
        else:
            conflict_ordinal += 1
            selected_fact_id = (
                case.contradictory_fact_ids[0]
                if conflict_ordinal <= contradictory_conflict_case_count
                else case.authoritative_fact_id
            )
        assert selected_fact_id is not None
        results.append(
            {
                "case_id": case.case_id,
                "selected_fact_ids": [selected_fact_id],
                "answer_fact_id": selected_fact_id,
            }
        )
    run["results"] = results
    return run


def test_fixture_is_versioned_hashed_and_has_resolution_for_both_roadmap_goals():
    material = load_protocol()
    description = describe_protocol(material)

    assert material.fixture["protocol_id"] == PROTOCOL_ID
    assert material.manifest["fixture_sha256"] == material.fixture_sha256
    assert material.manifest["scoring_sha256"] == material.scoring_sha256
    assert material.manifest["fixture_corpus_sha256"] == material.corpus_sha256
    assert description["status"] == "protocol_scaffold_not_run"
    assert description["empirical_result"] == "not_available"
    assert description["fixture"]["delayed_recall_case_count"] == 20
    assert description["fixture"]["conflict_case_count"] == 50
    assert description["roadmap_goals_only"] == {
        "delayed_recall_improvement_percentage_points": 15.0,
        "conflict_fact_rate_percent_maximum": 2.0,
    }


def test_manifest_rejects_fixture_or_generator_drift(tmp_path: Path):
    source = ROOT / "benchmarks" / "fixtures" / "quality-conflict-heldout-v1"
    copied = tmp_path / "quality-conflict-heldout-v1"
    copied.mkdir()
    for name in ("suite.json", "scoring.json", "manifest.json"):
        (copied / name).write_text((source / name).read_text(encoding="utf-8"), encoding="utf-8")
    suite_path = copied / "suite.json"
    suite = json.loads(suite_path.read_text(encoding="utf-8"))
    suite["generator_seed"] = "drifted-seed"
    suite_path.write_text(json.dumps(suite), encoding="utf-8")

    with pytest.raises(FixtureValidationError, match="fixture_sha256"):
        load_protocol(copied)


def test_template_cannot_be_scored_without_completed_policy_results_and_attestation():
    material = load_protocol()

    with pytest.raises(SubmittedRunError, match="template placeholder"):
        validate_submitted_run(run_template(material, role="baseline"), material, expected_role="baseline")


def test_report_scores_explicit_operator_runs_and_treats_targets_as_protocol_goals_only():
    material = load_protocol()
    baseline = _completed_run(
        role="baseline",
        delayed_target_case_count=10,
        contradictory_conflict_case_count=10,
    )
    candidate = _completed_run(
        role="candidate",
        delayed_target_case_count=13,
        contradictory_conflict_case_count=1,
    )

    report = build_report(baseline_run=baseline, candidate_run=candidate, material=material)

    assert report["status"] == "operator_attested_observed_pass"
    goals = report["roadmap_goals"]
    assert goals["delayed_recall_improvement"] == {
        "target_percentage_points": DELAYED_RECALL_IMPROVEMENT_TARGET_PERCENTAGE_POINTS,
        "observed_percentage_points": 15.0,
        "status": "observed_pass",
        "baseline_metric": "target_selection_rate_percent",
        "candidate_metric": "target_selection_rate_percent",
    }
    assert goals["conflict_fact_rate"] == {
        "target_percent_maximum": CONFLICT_FACT_RATE_TARGET_PERCENT,
        "observed_percent": 2.0,
        "status": "observed_pass",
        "metric": "conflict-case has one or more contradictory facts in bounded selection",
    }
    assert report["methodology"]["model_calls_by_protocol"] is False
    assert report["interpretation"]["model_quality_claim"] == "not_made"
    rendered = render_markdown_report(report)
    assert "does not run a model" in rendered
    assert "reproducible, not inherently secret" in rendered
    assert "selected_fact_ids" not in canonical_json(report)


def test_invalid_or_over_budget_submitted_selection_is_rejected_before_scoring():
    material = load_protocol()
    baseline = _completed_run(
        role="baseline",
        delayed_target_case_count=10,
        contradictory_conflict_case_count=10,
    )
    malformed = copy.deepcopy(baseline)
    first = malformed["results"][0]
    assert isinstance(first, dict)
    delayed_case = material.cases[0]
    first["selected_fact_ids"] = list(delayed_case.available_fact_ids[:4])
    first["answer_fact_id"] = delayed_case.available_fact_ids[0]

    with pytest.raises(SubmittedRunError, match="exceeds the fixture token budget"):
        validate_submitted_run(malformed, material, expected_role="baseline")


def test_cli_describe_and_template_are_non_empirical_and_write_only_explicit_path(tmp_path: Path):
    description = subprocess.run(
        [sys.executable, str(ROOT / "benchmarks" / "heldout_quality_conflict_protocol.py"), "--describe"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert description.returncode == 0, description.stderr
    assert "protocol_scaffold_not_run" in description.stdout
    assert "no empirical baseline/candidate result" in description.stdout

    target = tmp_path / "baseline.run.json"
    template = subprocess.run(
        [
            sys.executable,
            str(ROOT / "benchmarks" / "heldout_quality_conflict_protocol.py"),
            "--init-run",
            str(target),
            "--role",
            "baseline",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert template.returncode == 0, template.stderr
    assert "non-runnable run template" in template.stdout
    saved = json.loads(target.read_text(encoding="utf-8"))
    assert saved["protocol_id"] == PROTOCOL_ID
    assert saved["attestation"]["independent_evaluator"] is False


def test_cli_scores_only_explicit_completed_baseline_and_candidate_files(tmp_path: Path):
    baseline_path = tmp_path / "baseline.run.json"
    candidate_path = tmp_path / "candidate.run.json"
    report_json = tmp_path / "report.json"
    report_markdown = tmp_path / "report.md"
    baseline_path.write_text(
        json.dumps(
            _completed_run(
                role="baseline",
                delayed_target_case_count=10,
                contradictory_conflict_case_count=10,
            )
        ),
        encoding="utf-8",
    )
    candidate_path.write_text(
        json.dumps(
            _completed_run(
                role="candidate",
                delayed_target_case_count=13,
                contradictory_conflict_case_count=1,
            )
        ),
        encoding="utf-8",
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "benchmarks" / "heldout_quality_conflict_protocol.py"),
            "--baseline-run",
            str(baseline_path),
            "--candidate-run",
            str(candidate_path),
            "--json",
            str(report_json),
            "--markdown",
            str(report_markdown),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "makes no model-quality or universal claim" in completed.stdout
    report = json.loads(report_json.read_text(encoding="utf-8"))
    assert report["status"] == "operator_attested_observed_pass"
    assert "selected_fact_ids" not in report_markdown.read_text(encoding="utf-8")
