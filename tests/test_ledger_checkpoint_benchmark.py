"""Regression coverage for the offline sealed-checkpoint semantic benchmark."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from benchmarks.ledger_checkpoint_benchmark import (
    _CHECKPOINT_SECRET,
    _PAYLOAD,
    _SCOPE,
)
from benchmarks.memory_benchmark import (
    BenchmarkVerificationError,
    canonical_json,
    fixture_sha256,
    load_expected,
    load_suite,
    run_suite,
    verify_report,
)


ROOT = Path(__file__).resolve().parents[1]


async def test_v03_checkpoint_suite_is_frozen_deterministic_and_content_free():
    suite = load_suite("v0.3")
    expected = load_expected("v0.3")
    first = await run_suite(suite)
    second = await run_suite(suite)

    assert suite["suite_kind"] == "ledger_sealed_checkpoint"
    assert expected["fixture_sha256"] == fixture_sha256(suite)
    assert canonical_json(first) == canonical_json(second)
    verify_report(first, expected)
    assert first["benchmark_kind"] == "ledger_sealed_checkpoint"
    assert first["summary"] == {
        "case_count": 4,
        "passing_case_count": 4,
        "all_pass": True,
    }
    assert all(case["result"]["status"] == "ok" for case in first["cases"])

    report_text = canonical_json(first)
    assert _PAYLOAD not in report_text
    assert _CHECKPOINT_SECRET.decode("ascii") not in report_text
    assert _SCOPE.correlation_id() not in report_text
    assert _PAYLOAD not in canonical_json(suite)
    assert _CHECKPOINT_SECRET.decode("ascii") not in canonical_json(suite)
    assert _SCOPE.correlation_id() not in canonical_json(suite)
    expected_text = canonical_json(expected)
    assert _PAYLOAD not in expected_text
    assert _CHECKPOINT_SECRET.decode("ascii") not in expected_text
    assert _SCOPE.correlation_id() not in expected_text


async def test_v03_verification_rejects_a_semantically_corrupted_report():
    report = await run_suite(load_suite("v0.3"))
    corrupted = deepcopy(load_expected("v0.3"))
    corrupted["report"]["summary"]["all_pass"] = False

    with pytest.raises(BenchmarkVerificationError, match="semantic report"):
        verify_report(report, corrupted)


def test_v03_fixture_rejects_missing_required_contract_case():
    suite = deepcopy(load_suite("v0.3"))
    suite["contracts"] = suite["contracts"][:-1]

    with pytest.raises(ValueError, match="exactly one case"):
        import asyncio

        asyncio.run(run_suite(suite))


def test_v03_cli_verifies_and_renders_a_content_free_report(tmp_path: Path):
    script = ROOT / "scripts" / "run_memory_benchmark.py"
    report_path = tmp_path / "ledger-checkpoint-report.json"
    markdown_path = tmp_path / "ledger-checkpoint-report.md"
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--suite",
            "v0.3",
            "--verify",
            "--json",
            str(report_path),
            "--markdown",
            str(markdown_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "v0.3 verified" in completed.stdout
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["summary"]["all_pass"] is True
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "Ledger Sealed Checkpoint Benchmark" in markdown
    assert _PAYLOAD not in markdown
    assert _CHECKPOINT_SECRET.decode("ascii") not in markdown


def test_v03_cli_returns_a_verification_error_for_a_corrupted_baseline(tmp_path: Path):
    script = ROOT / "scripts" / "run_memory_benchmark.py"
    corrupted = deepcopy(load_expected("v0.3"))
    corrupted["report"]["summary"]["all_pass"] = False
    expected_path = tmp_path / "corrupted-expected.json"
    expected_path.write_text(json.dumps(corrupted), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--suite",
            "v0.3",
            "--verify",
            "--expected",
            str(expected_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert "memory benchmark failed: semantic report differs from frozen expected output" in (
        completed.stderr
    )
