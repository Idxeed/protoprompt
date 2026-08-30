"""Regression coverage for the versioned offline Memory Benchmark."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from benchmarks.memory_benchmark import (
    BenchmarkVerificationError,
    SeededFeatureHashEmbeddings,
    _tail_window_result,
    _tool_dependency_preserved,
    assert_candidate_not_worse_than_reference,
    canonical_json,
    canonical_sha256,
    fixture_sha256,
    load_expected,
    load_suite,
    run_suite,
    validate_embedding_guard,
    verify_report,
)


ROOT = Path(__file__).resolve().parents[1]


def _cases(report: dict) -> dict[str, dict]:
    return {item["id"]: item["results"] for item in report["cases"]}


def test_fixture_is_versioned_and_reference_is_bound_to_its_exact_hash():
    suite = load_suite()
    expected = load_expected()
    reference_path = (
        ROOT
        / "benchmarks"
        / "fixtures"
        / "v0.1"
        / "references"
        / "protoprompt-0.6.1.json"
    )
    reference = json.loads(reference_path.read_text(encoding="utf-8"))

    assert suite["suite_version"] == "v0.1"
    assert expected["fixture_sha256"] == fixture_sha256(suite)
    assert reference["suite"]["canonical_sha256"] == fixture_sha256(suite)
    assert reference["source"]["git_tag"] == "v0.6.1"
    assert reference["capabilities"]["context_plan"] is False

    scope_case = next(case for case in suite["cases"] if case["id"] == "scope_isolation")
    primary = scope_case["scope"]
    changed = {
        tuple(
            field
            for field in ("tenant", "user", "thread")
            if record["scope"][field] != primary[field]
        )
        for record in scope_case["foreign_records"]
    }
    assert changed == {("tenant",), ("user",), ("thread",)}


async def test_suite_is_deterministic_matches_expected_and_has_no_dynamic_values():
    suite = load_suite()
    first = await run_suite(suite)
    second = await run_suite(suite)

    assert canonical_json(first) == canonical_json(second)
    verify_report(first, load_expected())
    assert_candidate_not_worse_than_reference(first)
    reference = json.loads(
        (
            ROOT
            / "benchmarks"
            / "fixtures"
            / "v0.1"
            / "references"
            / "protoprompt-0.6.1.json"
        ).read_text(encoding="utf-8")
    )
    assert first["frozen_reference_sha256"] == canonical_sha256(reference)

    report_text = canonical_json(first)
    assert "trace_id" not in report_text
    assert "source:" not in report_text
    for case in suite["cases"]:
        assert case["sentinel"] not in report_text


async def test_candidate_covers_cold_recall_scope_and_final_request_contracts():
    report = await run_suite(load_suite())
    cases = _cases(report)

    delayed = cases["delayed_cold_recall"]
    assert delayed["tail_window_v1"]["evidence_available"] is False
    assert delayed["vector_recall_v1"]["cold_reopen"] is True
    assert delayed["protoprompt_context_plan_v0_7"]["target_rank"] == 1

    collision = cases["near_collision_recall"]["protoprompt_context_plan_v0_7"]
    assert collision["evidence_available"] is True
    assert collision["target_rank"] == 1

    scope = cases["scope_isolation"]["protoprompt_context_plan_v0_7"]
    assert scope["scope_leak_count"] == 0
    assert scope["explain_content_leak"] is False

    final = cases["final_request_budget"]["protoprompt_context_plan_v0_7"]
    assert final["budget_violation_count"] == 0
    assert final["receipt_reconciles"] is True
    assert final["tool_dependency_preserved"] is True
    assert final["channel_evidence"] == {"rag": True, "session": True}
    assert final["decision_contract_coverage"] == 1.0


async def test_seeded_embedding_guard_is_stable_and_rejects_fixture_regressions():
    suite = load_suite()
    config = suite["embedding"]
    embeddings = SeededFeatureHashEmbeddings(
        seed=config["seed"], dimensions=config["dimensions"]
    )
    assert embeddings.embed_one("anchor aurora") == embeddings.embed_one("ANCHOR AURORA")
    validate_embedding_guard(suite, embeddings)

    broken = deepcopy(suite)
    broken["collision_guard"]["min_target_cosine"] = 1.1
    with pytest.raises(ValueError, match="target similarity"):
        validate_embedding_guard(broken, embeddings)


async def test_verification_rejects_an_artificially_corrupted_expected_report():
    report = await run_suite(load_suite())
    corrupted = deepcopy(load_expected())
    corrupted["report"]["cases"][0]["results"]["vector_recall_v1"]["target_rank"] = 2

    with pytest.raises(BenchmarkVerificationError, match="semantic report"):
        verify_report(report, corrupted)


def test_baseline_request_packer_reports_a_real_overflow_instead_of_assuming_zero():
    suite = deepcopy(load_suite())
    suite["context_plan"]["max_tokens"] = 25
    case = next(item for item in suite["cases"] if item["id"] == "delayed_cold_recall")

    result = _tail_window_result(case, suite)

    assert result["budget_violation_count"] == 1
    assert result["evidence_available"] is False


def test_tool_dependency_check_requires_the_original_contiguous_pair():
    history = [
        {"role": "assistant", "content": None, "tool_calls": [{"id": "call-1"}]},
        {"role": "tool", "tool_call_id": "call-1", "content": "result"},
    ]
    separated = [history[0], {"role": "user", "content": "interleaved"}, history[1]]

    assert _tool_dependency_preserved(history, history) is True
    assert _tool_dependency_preserved(separated, history) is False


def test_cli_verifies_and_writes_only_explicit_output_paths(tmp_path: Path):
    script = ROOT / "scripts" / "run_memory_benchmark.py"
    report_path = tmp_path / "memory-report.json"
    markdown_path = tmp_path / "memory-report.md"
    command = [
        sys.executable,
        str(script),
        "--suite",
        "v0.1",
        "--verify",
        "--json",
        str(report_path),
        "--markdown",
        str(markdown_path),
    ]
    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)

    assert completed.returncode == 0, completed.stderr
    assert "verified" in completed.stdout
    assert json.loads(report_path.read_text(encoding="utf-8"))["suite_version"] == "v0.1"
    assert "ProtoPrompt Memory Benchmark" in markdown_path.read_text(encoding="utf-8")

    corrupted = deepcopy(load_expected())
    corrupted["fixture_sha256"] = "0" * 64
    expected_path = tmp_path / "corrupted-expected.json"
    expected_path.write_text(json.dumps(corrupted), encoding="utf-8")
    failed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--suite",
            "v0.1",
            "--verify",
            "--expected",
            str(expected_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert failed.returncode == 1
    assert "fixture SHA-256" in failed.stderr
