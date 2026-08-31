"""Regression coverage for the offline task-episode resume semantic benchmark."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from benchmarks.ledger_task_resume_benchmark import (
    _CHECKPOINT_SECRET,
    _EPISODE_GOAL,
    _LIVE_QUERY,
    _PARENT_SCOPE,
    _TASK_DESCRIPTOR,
    _TASK_REF,
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


async def test_v04_task_resume_suite_is_frozen_deterministic_and_content_free():
    suite = load_suite("v0.4")
    expected = load_expected("v0.4")
    first = await run_suite(suite)
    second = await run_suite(suite)

    assert suite["suite_kind"] == "ledger_task_episode_resume"
    assert expected["fixture_sha256"] == fixture_sha256(suite)
    assert canonical_json(first) == canonical_json(second)
    verify_report(first, expected)
    assert first["benchmark_kind"] == "ledger_task_episode_resume"
    assert first["summary"] == {
        "case_count": 5,
        "passing_case_count": 5,
        "all_pass": True,
    }
    assert all(case["result"]["status"] == "ok" for case in first["cases"])

    private_markers = (
        _CHECKPOINT_SECRET.decode("ascii"),
        _TASK_REF,
        _TASK_DESCRIPTOR,
        _LIVE_QUERY,
        _EPISODE_GOAL,
        _PARENT_SCOPE.correlation_id(),
    )
    for value in (first, suite, expected):
        rendered = canonical_json(value)
        assert not any(marker in rendered for marker in private_markers)


async def test_v04_verification_rejects_a_semantically_corrupted_report():
    report = await run_suite(load_suite("v0.4"))
    corrupted = deepcopy(load_expected("v0.4"))
    corrupted["report"]["summary"]["all_pass"] = False

    with pytest.raises(BenchmarkVerificationError, match="semantic report"):
        verify_report(report, corrupted)


def test_v04_fixture_rejects_missing_required_contract_case():
    suite = deepcopy(load_suite("v0.4"))
    suite["contracts"] = suite["contracts"][:-1]

    with pytest.raises(ValueError, match="exactly one case"):
        import asyncio

        asyncio.run(run_suite(suite))


def test_v04_cli_verifies_and_renders_a_content_free_report(tmp_path: Path):
    script = ROOT / "scripts" / "run_memory_benchmark.py"
    report_path = tmp_path / "task-resume-report.json"
    markdown_path = tmp_path / "task-resume-report.md"
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--suite",
            "v0.4",
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
    assert "v0.4 verified" in completed.stdout
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["summary"]["all_pass"] is True
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "Ledger Task-Episode Resume Benchmark" in markdown
    for marker in (_CHECKPOINT_SECRET.decode("ascii"), _TASK_REF, _EPISODE_GOAL):
        assert marker not in markdown


def test_v04_cli_returns_a_verification_error_for_a_corrupted_baseline(tmp_path: Path):
    script = ROOT / "scripts" / "run_memory_benchmark.py"
    corrupted = deepcopy(load_expected("v0.4"))
    corrupted["report"]["summary"]["all_pass"] = False
    expected_path = tmp_path / "corrupted-expected.json"
    expected_path.write_text(json.dumps(corrupted), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--suite",
            "v0.4",
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
