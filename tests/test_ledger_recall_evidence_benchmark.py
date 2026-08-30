"""Fixture and verifier coverage for frozen v1 Ledger recall evidence."""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from benchmarks.ledger_recall_evidence_benchmark import (
    validate_ledger_recall_evidence_manifest,
)
from benchmarks.memory_benchmark import (
    BenchmarkVerificationError,
    canonical_json,
    fixture_sha256,
    load_expected,
    load_suite,
    verify_report,
)


ROOT = Path(__file__).resolve().parents[1]


def _report_from_expected(expected: dict, backend_ids: tuple[str, ...]) -> dict:
    baseline = expected["report"]
    report = {
        field: baseline[field]
        for field in (
            "report_schema_version",
            "suite_version",
            "benchmark_kind",
            "candidate_id",
            "baseline_id",
            "fixture_sha256",
        )
    }
    normalized = baseline["normalized_backend_report"]
    report["backend_reports"] = {}
    for backend_id in backend_ids:
        backend = deepcopy(normalized)
        backend["backend_id"] = backend_id
        report["backend_reports"][backend_id] = backend
    return report


def test_v1_evidence_fixture_is_manifest_bound_and_content_free():
    suite = load_suite("v1.0")
    expected = load_expected("v1.0")
    manifest = json.loads(
        (ROOT / "benchmarks" / "fixtures" / "v1.0" / "manifest.json").read_text(
            encoding="utf-8"
        )
    )

    assert suite["suite_kind"] == "ledger_dual_backend_recall"
    assert suite["foreign_scope_axes"] == {
        "100": "tenant",
        "500": "user",
        "1000": "thread",
    }
    assert expected["fixture_sha256"] == fixture_sha256(suite)
    validate_ledger_recall_evidence_manifest(
        manifest,
        suite=suite,
        expected=expected,
    )

    expected_text = canonical_json(expected)
    for private_value in (
        "EVIDENCE_TARGET_",
        "EVIDENCE_REPLACEMENT_",
        "EVIDENCE_REVOKED_",
        "ledger-evidence-primary",
        "ledger-evidence-user",
    ):
        assert private_value not in expected_text


def test_v1_verifier_requires_both_backends_and_normalized_parity():
    expected = load_expected("v1.0")
    one_backend = _report_from_expected(expected, ("sqlite_v6",))

    with pytest.raises(BenchmarkVerificationError, match="requires both SQLite"):
        verify_report(one_backend, expected)
    verify_report(one_backend, expected, require_all_evidence_backends=False)

    dual_backend = _report_from_expected(expected, ("sqlite_v6", "postgres_v6"))
    verify_report(dual_backend, expected)

    dual_backend["backend_reports"]["postgres_v6"]["cases"][0]["result"]["checks"][
        "source_revocation_enforced"
    ] = False
    with pytest.raises(BenchmarkVerificationError, match="frozen postgres_v6 output"):
        verify_report(dual_backend, expected)


def test_v1_cli_refuses_to_verify_only_one_backend():
    script = ROOT / "scripts" / "run_memory_benchmark.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--suite",
            "v1.0",
            "--verify",
            "--ledger-backend",
            "sqlite",
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 1
    assert "v1.0 verification requires --ledger-backend all" in completed.stderr
