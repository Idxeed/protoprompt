"""Contract tests for the local-only raw performance evidence scaffold."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest

import benchmarks.raw_performance_protocol as protocol
from benchmarks.raw_performance_protocol import (
    DEFAULT_BYTE_BUDGET,
    DEFAULT_MEASUREMENT_REPETITIONS,
    DEFAULT_TOKEN_BUDGET,
    DEFAULT_WARMUP_ITERATIONS,
    PLANNER_ACTIVE_READ_LIMIT,
    PROTOCOL_ID,
    REFERENCE_CORPUS_RECORDS,
    PerformanceProtocolError,
    ProtocolConfig,
    ReferenceManifestError,
    _TARGET_MARKER,
    _TASK,
    build_report,
    canonical_json,
    nearest_rank,
    reference_manifest_template,
    render_markdown_report,
    run_raw_performance_protocol,
    summarize_samples,
    validate_output_destinations,
    validate_reference_manifest,
    validate_storage_directory,
)


ROOT = Path(__file__).resolve().parents[1]


def _reference_manifest() -> dict[str, object]:
    return {
        "schema_version": 2,
        "protocol_id": PROTOCOL_ID,
        "manifest_class": "operator_verified_reference",
        "operator_verified": True,
        "hardware": {
            "cpu_model": "AMD Ryzen 5 5600X",
            "physical_memory_gib": 32,
            "storage": "Samsung 980 PRO 1TB NVMe",
            "power_profile": "AC high performance",
        },
        "software": {
            "operating_system": "Windows 11 Pro 23H2 build 22631",
            "python": "CPython 3.11.9",
            "sqlite": "3.45.0",
            "protoprompt_version": "0.18.0",
            "source_revision": "0123456789abcdef0123456789abcdef01234567",
        },
    }


def _observation() -> dict[str, int | bool]:
    return {
        "planner_active_record_count": PLANNER_ACTIVE_READ_LIMIT,
        "active_read_limit_reached": True,
        "eligible_record_count": PLANNER_ACTIVE_READ_LIMIT,
        "candidate_count": PLANNER_ACTIVE_READ_LIMIT,
        "candidate_limit_reached": False,
        "scanned_count": PLANNER_ACTIVE_READ_LIMIT,
        "selected_record_count": 18,
    }


def _verification_manifest() -> dict[str, object]:
    return {
        "schema_version": 2,
        "protocol_id": PROTOCOL_ID,
        "manifest_class": "verification_only",
        "operator_verified": False,
        "hardware": {
            "cpu_model": "Local verification CPU",
            "physical_memory_gib": 32,
            "storage": "Local temporary storage",
            "power_profile": "Local verification profile",
        },
        "software": {
            "operating_system": "Local verification OS",
            "python": "CPython local verification",
            "sqlite": "local sqlite",
            "protoprompt_version": "0.18.0-local",
            "source_revision": "local-unpublished-verification",
        },
    }


def test_reference_protocol_requires_10k_corpus_and_meaningful_sampling():
    config = ProtocolConfig()

    assert config.corpus_records == REFERENCE_CORPUS_RECORDS == 10_000
    assert config.warmup_iterations == DEFAULT_WARMUP_ITERATIONS == 5
    assert config.measurement_repetitions == DEFAULT_MEASUREMENT_REPETITIONS == 30

    with pytest.raises(ValueError, match="exactly 10000 records"):
        ProtocolConfig(corpus_records=9_999)
    with pytest.raises(ValueError, match="warmup_iterations"):
        ProtocolConfig(warmup_iterations=4)
    with pytest.raises(ValueError, match="measurement_repetitions"):
        ProtocolConfig(measurement_repetitions=29)
    with pytest.raises(ValueError, match="token_budget=2048"):
        ProtocolConfig(token_budget=DEFAULT_TOKEN_BUDGET + 1)
    with pytest.raises(ValueError, match="byte_budget=32768"):
        ProtocolConfig(byte_budget=DEFAULT_BYTE_BUDGET + 1)


def test_nearest_rank_p50_p95_and_raw_samples_are_reproducible():
    samples = list(range(1, 31))

    assert nearest_rank(samples, 0.50) == 15
    assert nearest_rank(samples, 0.95) == 29
    summary = summarize_samples(samples)
    assert summary["sample_count"] == 30
    assert summary["samples_ns"] == samples
    assert summary["p50_ns"] == 15
    assert summary["p95_ns"] == 29
    assert summary["percentile_method"] == "nearest_rank"


def test_reference_manifest_template_must_be_completed_and_operator_verified():
    with pytest.raises(ReferenceManifestError, match="operator_verified"):
        validate_reference_manifest(reference_manifest_template())

    manifest = validate_reference_manifest(_reference_manifest())
    assert manifest["operator_verified"] is True
    assert manifest["hardware"] == {
        "cpu_model": "AMD Ryzen 5 5600X",
        "physical_memory_gib": 32.0,
        "storage": "Samsung 980 PRO 1TB NVMe",
        "power_profile": "AC high performance",
    }

    malformed = _reference_manifest()
    malformed["software"] = dict(malformed["software"], unknown="not-allowed")
    with pytest.raises(ReferenceManifestError, match="exact fields"):
        validate_reference_manifest(malformed)

    generic = _reference_manifest()
    generic["hardware"] = dict(generic["hardware"], cpu_model="Local verification CPU")
    with pytest.raises(ReferenceManifestError, match="generic or verification-only"):
        validate_reference_manifest(generic)

    unpinned = _reference_manifest()
    unpinned["software"] = dict(unpinned["software"], source_revision="local-worktree")
    with pytest.raises(ReferenceManifestError, match="exact 40-character Git commit"):
        validate_reference_manifest(unpinned)


@pytest.mark.parametrize("schema_version", [True, 2.0, "2"])
def test_reference_manifest_requires_an_exact_integer_schema_version(
    schema_version: object,
) -> None:
    manifest = _reference_manifest()
    manifest["schema_version"] = schema_version

    with pytest.raises(ReferenceManifestError, match="schema version"):
        validate_reference_manifest(manifest)


def test_report_evaluates_only_a_full_10k_observation_and_keeps_claims_bounded():
    config = ProtocolConfig()
    samples = list(range(1, config.measurement_repetitions + 1))
    report = build_report(
        config=config,
        reference_manifest=_reference_manifest(),
        corpus_population_ns=1_000_000,
        persisted_record_count=REFERENCE_CORPUS_RECORDS,
        active_slice_count=PLANNER_ACTIVE_READ_LIMIT,
        observation=_observation(),
        planning_samples_ns=samples,
        resolution_samples_ns=list(reversed(samples)),
    )

    assert report["corpus"]["persisted_record_count"] == 10_000
    assert report["methodology"]["warmup_samples_retained"] is False
    assert report["methodology"]["storage"] == "fresh_disposable_sqlite_v6_file"
    assert report["methodology"]["storage_directory"] == (
        "explicit_operator_selected_not_reported"
    )
    assert report["methodology"]["storage_locality"] == "operator_attested_local"
    assert report["methodology"]["provider_embedding_or_remote_database_io"] is False
    assert report["methodology"]["embedding_calls"] is False
    assert report["roadmap_planning_gate"]["status"] == "observed_pass"
    assert "full active/candidate/scan coverage observed" in report["roadmap_planning_gate"]["reason"]
    assert report["interpretation"]["runtime_claim"] == "not_made"
    assert report["interpretation"]["universal_claim"] == "not_made"
    assert report["reference_evidence"]["reference_baseline_eligible"] is True

    rendered = render_markdown_report(report)
    assert "not a universal performance claim" in rendered
    assert "`observed_pass`" in rendered
    assert "model quality" in rendered
    report_text = canonical_json(report)
    assert _TARGET_MARKER not in report_text
    assert _TASK not in report_text


def test_report_does_not_evaluate_the_gate_when_a_coverage_check_is_incomplete():
    config = ProtocolConfig()
    samples = list(range(1, config.measurement_repetitions + 1))
    observation = _observation()
    observation["scanned_count"] = REFERENCE_CORPUS_RECORDS - 1

    report = build_report(
        config=config,
        reference_manifest=_reference_manifest(),
        corpus_population_ns=1_000_000,
        persisted_record_count=REFERENCE_CORPUS_RECORDS,
        active_slice_count=PLANNER_ACTIVE_READ_LIMIT,
        observation=observation,
        planning_samples_ns=samples,
        resolution_samples_ns=samples,
    )

    assert report["roadmap_planning_gate"]["status"] == "not_evaluated"
    assert "scanned=9999" in report["roadmap_planning_gate"]["reason"]


def test_verification_only_manifest_is_explicitly_not_a_reference_baseline():
    config = ProtocolConfig()
    samples = list(range(1, config.measurement_repetitions + 1))
    manifest = validate_reference_manifest(_verification_manifest())

    assert manifest["operator_verified"] is False
    report = build_report(
        config=config,
        reference_manifest=manifest,
        corpus_population_ns=1_000_000,
        persisted_record_count=REFERENCE_CORPUS_RECORDS,
        active_slice_count=PLANNER_ACTIVE_READ_LIMIT,
        observation=_observation(),
        planning_samples_ns=samples,
        resolution_samples_ns=samples,
    )

    assert report["reference_evidence"]["reference_baseline_eligible"] is False
    assert report["interpretation"]["reference_baseline_claim"] == "not_eligible_verification_only"
    assert report["roadmap_planning_gate"]["status"] == "not_evaluated"
    assert report["roadmap_planning_gate"]["diagnostic_full_coverage_observed"] is True
    assert "Verification-only result" in render_markdown_report(report)


def test_report_rejects_a_loop_count_when_the_persisted_corpus_is_not_10k():
    config = ProtocolConfig()
    samples = list(range(1, config.measurement_repetitions + 1))

    with pytest.raises(PerformanceProtocolError, match="persisted corpus count"):
        build_report(
            config=config,
            reference_manifest=_reference_manifest(),
            corpus_population_ns=1_000_000,
            persisted_record_count=REFERENCE_CORPUS_RECORDS - 1,
            active_slice_count=PLANNER_ACTIVE_READ_LIMIT,
            observation=_observation(),
            planning_samples_ns=samples,
            resolution_samples_ns=samples,
        )


def test_init_manifest_cli_writes_only_an_explicit_template_path(tmp_path: Path):
    target = tmp_path / "reference-manifest.json"
    command = [
        sys.executable,
        str(ROOT / "benchmarks" / "raw_performance_protocol.py"),
        "--init-reference-manifest",
        str(target),
    ]

    completed = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)

    assert completed.returncode == 0, completed.stderr
    assert "non-runnable" in completed.stdout
    template = json.loads(target.read_text(encoding="utf-8"))
    assert template["protocol_id"] == PROTOCOL_ID
    assert template["operator_verified"] is False


def test_storage_directory_is_explicit_existing_and_not_a_system_temp_fallback(tmp_path: Path):
    assert validate_storage_directory(tmp_path) == tmp_path.resolve()

    with pytest.raises(PerformanceProtocolError, match="does not exist"):
        validate_storage_directory(tmp_path / "missing")
    file_path = tmp_path / "not-a-directory"
    file_path.write_text("x", encoding="utf-8")
    with pytest.raises(PerformanceProtocolError, match="must be a directory"):
        validate_storage_directory(file_path)


def test_cli_rejects_report_paths_that_alias_each_other_or_the_manifest(tmp_path: Path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(_verification_manifest()), encoding="utf-8")
    output = tmp_path / "report.json"

    with pytest.raises(PerformanceProtocolError, match="alias the reference manifest"):
        validate_output_destinations(
            reference_manifest=manifest,
            json_output=manifest,
            markdown_output=None,
        )
    with pytest.raises(PerformanceProtocolError, match="distinct output"):
        validate_output_destinations(
            reference_manifest=manifest,
            json_output=output,
            markdown_output=output,
        )
    with pytest.raises(SystemExit):
        protocol._parse_args(
            [
                "--reference-manifest",
                str(manifest),
                "--json",
                str(output),
            ]
        )


def test_compact_execution_path_populates_reopens_warms_and_measures_in_explicit_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Exercise the real protocol lifecycle at a test-only tiny corpus scale."""

    monkeypatch.setattr(protocol, "REFERENCE_CORPUS_RECORDS", 3)
    monkeypatch.setattr(protocol, "PLANNER_ACTIVE_READ_LIMIT", 3)
    monkeypatch.setattr(protocol, "PLANNER_CANDIDATE_LIMIT", 3)

    original_temporary_directory = protocol.tempfile.TemporaryDirectory
    directories: list[str | None] = []

    def tracked_temporary_directory(*args, **kwargs):
        directories.append(kwargs.get("dir"))
        return original_temporary_directory(*args, **kwargs)

    monkeypatch.setattr(protocol.tempfile, "TemporaryDirectory", tracked_temporary_directory)
    original_measure_once = protocol._measure_once
    calls: list[object] = []

    def tracked_measure_once(planner, config):
        calls.append(planner)
        return original_measure_once(planner, config)

    monkeypatch.setattr(protocol, "_measure_once", tracked_measure_once)
    config = protocol.ProtocolConfig(corpus_records=3)
    report = run_raw_performance_protocol(
        config=config,
        reference_manifest=_verification_manifest(),
        storage_directory=tmp_path,
    )

    assert directories == [str(tmp_path.resolve())]
    assert len(calls) == config.warmup_iterations + config.measurement_repetitions
    assert report["corpus"]["persisted_record_count"] == 3
    assert report["measurement_observation"]["selected_record_count"] >= 1
    assert report["reference_evidence"]["reference_baseline_eligible"] is False
