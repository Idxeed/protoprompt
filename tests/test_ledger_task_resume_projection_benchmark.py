"""Regression coverage for the offline task-resume projection benchmark."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from benchmarks.ledger_task_resume_projection_benchmark import (
    _CHECKPOINT_SECRET,
    _LIVE_QUERY,
    _PARENT_SCOPE,
    _TASK_DESCRIPTOR,
    _TASK_REF,
    _episode,
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


async def test_v05_projection_suite_is_frozen_deterministic_and_content_free():
    suite = load_suite("v0.5")
    expected = load_expected("v0.5")
    first = await run_suite(suite)
    second = await run_suite(suite)

    assert suite["suite_kind"] == "ledger_task_episode_projection"
    assert expected["fixture_sha256"] == fixture_sha256(suite)
    assert canonical_json(first) == canonical_json(second)
    verify_report(first, expected)
    assert first["benchmark_kind"] == "ledger_task_episode_projection"
    assert first["summary"] == {
        "case_count": 3,
        "passing_case_count": 3,
        "all_pass": True,
    }
    assert all(case["result"]["status"] == "ok" for case in first["cases"])

    episode = _episode(_TASK_REF)
    private_markers = (
        _CHECKPOINT_SECRET.decode("ascii"),
        _TASK_REF,
        _TASK_DESCRIPTOR,
        _LIVE_QUERY,
        _PARENT_SCOPE.correlation_id(),
        *episode.completed_action_refs,
        episode.goal,
        episode.next_action or "",
        episode.lesson or "",
    )
    for value in (first, suite, expected):
        rendered = canonical_json(value)
        assert not any(marker in rendered for marker in private_markers)


async def test_v05_verification_rejects_a_semantically_corrupted_report():
    report = await run_suite(load_suite("v0.5"))
    corrupted = deepcopy(load_expected("v0.5"))
    corrupted["report"]["summary"]["all_pass"] = False

    with pytest.raises(BenchmarkVerificationError, match="semantic report"):
        verify_report(report, corrupted)


def test_v05_fixture_rejects_missing_or_non_metadata_contracts():
    missing = deepcopy(load_suite("v0.5"))
    missing["contracts"] = missing["contracts"][:-1]
    expanded = deepcopy(load_suite("v0.5"))
    expanded["contracts"][0]["payload"] = "must-not-enter-fixture"

    import asyncio

    with pytest.raises(ValueError, match="exactly one case"):
        asyncio.run(run_suite(missing))
    with pytest.raises(ValueError, match="must not carry payload"):
        asyncio.run(run_suite(expanded))


def test_v05_cli_verifies_and_renders_a_content_free_report(tmp_path: Path):
    script = ROOT / "scripts" / "run_memory_benchmark.py"
    report_path = tmp_path / "task-projection-report.json"
    markdown_path = tmp_path / "task-projection-report.md"
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--suite",
            "v0.5",
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
    assert "v0.5 verified" in completed.stdout
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["summary"]["all_pass"] is True
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "Ledger Task-Episode Projection Benchmark" in markdown
    for marker in (_CHECKPOINT_SECRET.decode("ascii"), _TASK_REF, _TASK_DESCRIPTOR):
        assert marker not in markdown


def test_v05_cli_returns_a_verification_error_for_a_corrupted_baseline(tmp_path: Path):
    script = ROOT / "scripts" / "run_memory_benchmark.py"
    corrupted = deepcopy(load_expected("v0.5"))
    corrupted["report"]["summary"]["all_pass"] = False
    expected_path = tmp_path / "corrupted-expected.json"
    expected_path.write_text(json.dumps(corrupted), encoding="utf-8")

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--suite",
            "v0.5",
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
