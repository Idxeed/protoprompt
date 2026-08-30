"""Regression coverage for the frozen v0.2 Ledger composition benchmark."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

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


async def test_v02_ledger_composition_suite_is_frozen_deterministic_and_private():
    suite = load_suite("v0.2")
    expected = load_expected("v0.2")

    first = await run_suite(suite)
    second = await run_suite(suite)

    assert suite["suite_kind"] == "ledger_context_composition"
    assert expected["fixture_sha256"] == fixture_sha256(suite)
    assert canonical_json(first) == canonical_json(second)
    verify_report(first, expected)
    assert first["summary"] == {
        "case_count": 5,
        "passing_case_count": 5,
        "all_pass": True,
    }
    assert all(item["result"]["status"] == "ok" for item in first["cases"])

    report_text = canonical_json(first)
    assert "trace_id" not in report_text
    for case in suite["cases"]:
        assert case["admitted"]["content"] not in report_text
        assert case["admitted"]["record_id"] not in report_text
        for marker in case["private_markers"]:
            assert marker not in report_text
        if "raw" in case:
            assert case["raw"]["content"] not in report_text
            assert case["raw"]["record_id"] not in report_text


async def test_v02_verification_rejects_a_semantically_corrupted_report():
    report = await run_suite(load_suite("v0.2"))
    corrupted = deepcopy(load_expected("v0.2"))
    corrupted["report"]["summary"]["all_pass"] = False

    with pytest.raises(BenchmarkVerificationError, match="semantic report"):
        verify_report(report, corrupted)


def test_v02_fixture_rejects_missing_required_contract_case():
    suite = deepcopy(load_suite("v0.2"))
    suite["cases"] = suite["cases"][:-1]

    with pytest.raises(ValueError, match="exactly one case"):
        # `run_suite` re-validates a caller-supplied suite rather than trusting
        # the fixture loader, which is useful for replay/test harnesses.
        import asyncio

        asyncio.run(run_suite(suite))


def test_v02_fixture_rejects_a_duplicate_contract_case():
    suite = deepcopy(load_suite("v0.2"))
    duplicate = deepcopy(suite["cases"][0])
    duplicate["id"] = "strict_raw_exclusion_duplicate"
    suite["cases"].append(duplicate)

    with pytest.raises(ValueError, match="exactly one case"):
        import asyncio

        asyncio.run(run_suite(suite))


def test_v02_cli_verifies_and_renders_a_content_free_report(tmp_path: Path):
    script = ROOT / "scripts" / "run_memory_benchmark.py"
    report_path = tmp_path / "ledger-composition-report.json"
    markdown_path = tmp_path / "ledger-composition-report.md"
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--suite",
            "v0.2",
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
    assert "v0.2 verified" in completed.stdout
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["summary"]["all_pass"] is True
    markdown = markdown_path.read_text(encoding="utf-8")
    assert "Ledger Composition Benchmark" in markdown
    assert "ADMITTED_STRICT_SENTINEL" not in markdown
