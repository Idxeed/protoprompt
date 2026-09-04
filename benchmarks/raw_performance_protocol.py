"""Raw, local-only performance evidence protocol for Ledger planning.

The versioned benchmark suites in this repository deliberately verify semantic
contracts and do not report timings.  This module is a separate measurement
protocol for the future 1.0 runtime gate.  It uses a deterministic synthetic
10,000-record corpus, a fresh local SQLite Ledger, explicit warm-up runs, and
raw monotonic-clock samples.  It never calls a model, embedding service,
provider, or network service.

Timing values are inherently host-specific.  A report produced here is
evidence about exactly the declared machine, software manifest, source
revision, storage configuration, and run; it is not a universal latency,
throughput, model-quality, or framework-comparison claim.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import sqlite3
import sys
import tempfile
import time
from typing import Any, Mapping, Sequence

# Keep ``python benchmarks/raw_performance_protocol.py`` usable from a fresh
# checkout as well as ``python -m benchmarks.raw_performance_protocol``.
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from protoprompt import MemoryScope, RegexTokenCounter, __version__
from protoprompt.ledger import (
    MemoryAdmissionPolicy,
    MemoryKind,
    MemoryOrigin,
    MemoryReviewGate,
    MemoryWriter,
    SqliteMemoryLedger,
)
from protoprompt.ledger.recall import LedgerRecallPlanner, LedgerRecallPolicy


PROTOCOL_ID = "protoprompt-local-ledger-raw-performance-v1"
REPORT_SCHEMA_VERSION = 1
REFERENCE_CORPUS_RECORDS = 10_000
DEFAULT_WARMUP_ITERATIONS = 5
DEFAULT_MEASUREMENT_REPETITIONS = 30
DEFAULT_TOKEN_BUDGET = 2_048
DEFAULT_BYTE_BUDGET = 32_768
REFERENCE_P95_PLANNING_TARGET_MS = 50.0

# These are intentionally explicit rather than inferred from an implementation
# detail.  The public policy permits this opt-in 10k ceiling, while its normal
# defaults remain conservative (1,000 active records / 100 candidates).  A
# result can evaluate the roadmap gate only when every coverage check below
# shows that this explicit full-corpus policy was actually used.
PLANNER_ACTIVE_READ_LIMIT = REFERENCE_CORPUS_RECORDS
PLANNER_CANDIDATE_LIMIT = REFERENCE_CORPUS_RECORDS
PLANNER_CANDIDATE_SCAN_BYTE_BUDGET = 1_048_576

_CORPUS_GENERATOR_ID = "protoprompt-ledger-raw-performance-corpus-v1"
_CORPUS_START = datetime(2048, 1, 1, tzinfo=timezone.utc)
_PLANNING_TIME = _CORPUS_START + timedelta(days=30)
_SCOPE = MemoryScope(
    tenant="raw-performance-reference",
    user="local-operator",
    thread="ledger-planning-10k",
)
_TASK = "reference performance target invariant planning query"
_TARGET_MARKER = "RAW_PERFORMANCE_TARGET_INVARIANT"
_REFERENCE_MANIFEST_CLASS = "operator_verified_reference"
_VERIFICATION_MANIFEST_CLASS = "verification_only"
_GENERIC_MANIFEST_VALUE = re.compile(
    r"(?:\bexample\b|\bgeneric\b|\blocal\s+verification\b|"
    r"\bverification[-\s]?only\b|\btemporary\b|\bplaceholder\b|"
    r"\bdummy\b|\bsample\b|\btest(?:ing)?\b|\breplace\b)",
    re.IGNORECASE,
)
_SOURCE_REVISION = re.compile(r"[0-9a-f]{40}", re.IGNORECASE)


class PerformanceProtocolError(RuntimeError):
    """Raised when a local performance run cannot produce valid evidence."""


class ReferenceManifestError(ValueError):
    """Raised when the required operator-owned reference manifest is invalid."""


def canonical_json(value: object) -> str:
    """Return the stable JSON representation used for fingerprints and reports."""

    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def canonical_sha256(value: object) -> str:
    """Return a SHA-256 fingerprint of canonical JSON-safe data."""

    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _require_nonempty_string(
    value: object,
    *,
    field: str,
    allow_generic: bool = False,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ReferenceManifestError(f"{field} must be a non-empty string")
    normalized = value.strip()
    if "REPLACE" in normalized.upper() or "<" in normalized or ">" in normalized:
        raise ReferenceManifestError(f"{field} still contains a template placeholder")
    if not allow_generic and _GENERIC_MANIFEST_VALUE.search(normalized):
        raise ReferenceManifestError(
            f"{field} is a generic or verification-only label, not reference evidence"
        )
    return normalized


def _require_positive_number(value: object, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReferenceManifestError(f"{field} must be a positive number")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0:
        raise ReferenceManifestError(f"{field} must be a finite positive number")
    return normalized


def _require_exact_keys(
    value: Mapping[str, object],
    *,
    expected: set[str],
    field: str,
) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        unexpected = sorted(actual - expected)
        detail: list[str] = []
        if missing:
            detail.append(f"missing {', '.join(missing)}")
        if unexpected:
            detail.append(f"unexpected {', '.join(unexpected)}")
        raise ReferenceManifestError(f"{field} must contain exact fields ({'; '.join(detail)})")


def reference_manifest_template() -> dict[str, object]:
    """Return a deliberately non-runnable template for one reference machine.

    Runtime APIs cannot reliably infer physical memory, storage, power mode,
    or the exact source revision used by an operator.  The operator must fill
    and explicitly verify those fields before the benchmark may run.
    """

    return {
        "schema_version": 2,
        "protocol_id": PROTOCOL_ID,
        "manifest_class": _REFERENCE_MANIFEST_CLASS,
        "operator_verified": False,
        "hardware": {
            "cpu_model": "REPLACE_WITH_EXACT_CPU_MODEL",
            "physical_memory_gib": 0,
            "storage": "REPLACE_WITH_LOCAL_STORAGE_DESCRIPTION",
            "power_profile": "REPLACE_WITH_POWER_OR_PERFORMANCE_PROFILE",
        },
        "software": {
            "operating_system": platform.platform(),
            "python": f"{platform.python_implementation()} {platform.python_version()}",
            "sqlite": sqlite3.sqlite_version,
            "protoprompt_version": __version__,
            "source_revision": "REPLACE_WITH_EXACT_COMMIT_OR_REVIEWED_WORKTREE_ID",
        },
    }


def validate_reference_manifest(value: object) -> dict[str, object]:
    """Validate and normalize an operator-owned hardware/software manifest.

    A manifest is intentionally required rather than silently synthesized from
    the executing machine.  It is part of the evidence boundary: a timing is
    only comparable to another timing when both report their own reviewed
    configuration.
    """

    if not isinstance(value, Mapping):
        raise ReferenceManifestError("reference manifest must be a JSON object")
    raw = dict(value)
    _require_exact_keys(
        raw,
        expected={
            "schema_version",
            "protocol_id",
            "manifest_class",
            "operator_verified",
            "hardware",
            "software",
        },
        field="reference manifest",
    )
    if type(raw["schema_version"]) is not int or raw["schema_version"] != 2:
        raise ReferenceManifestError("unsupported reference manifest schema version")
    if raw["protocol_id"] != PROTOCOL_ID:
        raise ReferenceManifestError("reference manifest protocol_id does not match")
    manifest_class = raw["manifest_class"]
    if manifest_class not in {
        _REFERENCE_MANIFEST_CLASS,
        _VERIFICATION_MANIFEST_CLASS,
    }:
        raise ReferenceManifestError("reference manifest has an unsupported manifest_class")
    is_reference = manifest_class == _REFERENCE_MANIFEST_CLASS
    if is_reference and raw["operator_verified"] is not True:
        raise ReferenceManifestError("reference manifest requires operator_verified=true")
    if not is_reference and raw["operator_verified"] is not False:
        raise ReferenceManifestError(
            "verification_only manifest requires operator_verified=false"
        )

    hardware = raw["hardware"]
    if not isinstance(hardware, Mapping):
        raise ReferenceManifestError("hardware must be an object")
    normalized_hardware = dict(hardware)
    _require_exact_keys(
        normalized_hardware,
        expected={"cpu_model", "physical_memory_gib", "storage", "power_profile"},
        field="hardware",
    )
    cpu_model = _require_nonempty_string(
        normalized_hardware["cpu_model"],
        field="hardware.cpu_model",
        allow_generic=not is_reference,
    )
    memory_gib = _require_positive_number(
        normalized_hardware["physical_memory_gib"],
        field="hardware.physical_memory_gib",
    )
    storage = _require_nonempty_string(
        normalized_hardware["storage"],
        field="hardware.storage",
        allow_generic=not is_reference,
    )
    power_profile = _require_nonempty_string(
        normalized_hardware["power_profile"],
        field="hardware.power_profile",
        allow_generic=not is_reference,
    )

    software = raw["software"]
    if not isinstance(software, Mapping):
        raise ReferenceManifestError("software must be an object")
    normalized_software = dict(software)
    _require_exact_keys(
        normalized_software,
        expected={
            "operating_system",
            "python",
            "sqlite",
            "protoprompt_version",
            "source_revision",
        },
        field="software",
    )
    source_revision = _require_nonempty_string(
        normalized_software["source_revision"],
        field="software.source_revision",
        allow_generic=not is_reference,
    )
    if is_reference and _SOURCE_REVISION.fullmatch(source_revision) is None:
        raise ReferenceManifestError(
            "software.source_revision must be an exact 40-character Git commit"
        )
    return {
        "schema_version": 2,
        "protocol_id": PROTOCOL_ID,
        "manifest_class": manifest_class,
        "operator_verified": is_reference,
        "hardware": {
            "cpu_model": cpu_model,
            "physical_memory_gib": memory_gib,
            "storage": storage,
            "power_profile": power_profile,
        },
        "software": {
            "operating_system": _require_nonempty_string(
                normalized_software["operating_system"],
                field="software.operating_system",
                allow_generic=not is_reference,
            ),
            "python": _require_nonempty_string(
                normalized_software["python"],
                field="software.python",
                allow_generic=not is_reference,
            ),
            "sqlite": _require_nonempty_string(
                normalized_software["sqlite"],
                field="software.sqlite",
                allow_generic=not is_reference,
            ),
            "protoprompt_version": _require_nonempty_string(
                normalized_software["protoprompt_version"],
                field="software.protoprompt_version",
                allow_generic=not is_reference,
            ),
            "source_revision": source_revision,
        },
    }


def _read_reference_manifest(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReferenceManifestError(f"reference manifest does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ReferenceManifestError(
            f"reference manifest is not valid JSON: {exc.msg}"
        ) from exc
    return validate_reference_manifest(value)


def validate_storage_directory(value: Path | str) -> Path:
    """Require an explicit existing operator-selected benchmark directory.

    The protocol must not silently fall back to the process/system temporary
    directory: it may be RAM-backed, redirected, or otherwise unsuitable for
    a local-storage claim.  This validation cannot prove the physical volume
    is local, so an operator-verified manifest remains an attestation rather
    than a magic filesystem detector.  The resolved directory itself is never
    written to a report.
    """

    if not isinstance(value, (str, Path)):
        raise PerformanceProtocolError("storage_directory must be a path string or Path")
    candidate = Path(value)
    if not candidate.exists():
        raise PerformanceProtocolError("storage_directory does not exist")
    if not candidate.is_dir():
        raise PerformanceProtocolError("storage_directory must be a directory")
    if candidate.is_symlink():
        raise PerformanceProtocolError("storage_directory must not be a symlink")
    resolved = candidate.resolve(strict=True)
    # UNC locations are never an admissible local SQLite benchmark directory.
    if os.name == "nt" and str(resolved).startswith("\\\\"):
        raise PerformanceProtocolError("storage_directory must not be a UNC path")
    return resolved


def _paths_alias(left: Path, right: Path) -> bool:
    """Recognize lexical, symlink, and existing-file aliases without writing."""

    if left.resolve(strict=False) == right.resolve(strict=False):
        return True
    try:
        return left.exists() and right.exists() and os.path.samefile(left, right)
    except OSError:
        return False


def validate_output_destinations(
    *,
    reference_manifest: Path,
    json_output: Path | None,
    markdown_output: Path | None,
) -> None:
    """Reject output aliases before an explicit report write can overwrite input."""

    outputs = tuple(path for path in (json_output, markdown_output) if path is not None)
    for index, output in enumerate(outputs):
        if _paths_alias(output, reference_manifest):
            raise PerformanceProtocolError(
                "report output must not alias the reference manifest"
            )
        for other in outputs[index + 1 :]:
            if _paths_alias(output, other):
                raise PerformanceProtocolError(
                    "--json and --markdown must name distinct output files"
                )


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")


@dataclass(frozen=True, slots=True)
class ProtocolConfig:
    """Fixed methodology for one raw local planning measurement."""

    corpus_records: int = REFERENCE_CORPUS_RECORDS
    warmup_iterations: int = DEFAULT_WARMUP_ITERATIONS
    measurement_repetitions: int = DEFAULT_MEASUREMENT_REPETITIONS
    token_budget: int = DEFAULT_TOKEN_BUDGET
    byte_budget: int = DEFAULT_BYTE_BUDGET

    def __post_init__(self) -> None:
        for field in (
            "corpus_records",
            "warmup_iterations",
            "measurement_repetitions",
            "token_budget",
            "byte_budget",
        ):
            value = getattr(self, field)
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{field} must be an integer")
        if self.corpus_records != REFERENCE_CORPUS_RECORDS:
            raise ValueError(
                f"raw performance protocol requires exactly {REFERENCE_CORPUS_RECORDS} records"
            )
        if self.warmup_iterations < DEFAULT_WARMUP_ITERATIONS:
            raise ValueError(
                f"warmup_iterations must be at least {DEFAULT_WARMUP_ITERATIONS}"
            )
        if self.measurement_repetitions < DEFAULT_MEASUREMENT_REPETITIONS:
            raise ValueError(
                "measurement_repetitions must be at least "
                f"{DEFAULT_MEASUREMENT_REPETITIONS} for p95"
            )
        if self.token_budget != DEFAULT_TOKEN_BUDGET:
            raise ValueError(
                f"raw performance protocol requires token_budget={DEFAULT_TOKEN_BUDGET}"
            )
        if self.byte_budget != DEFAULT_BYTE_BUDGET:
            raise ValueError(
                f"raw performance protocol requires byte_budget={DEFAULT_BYTE_BUDGET}"
            )


class _AdvancingClock:
    """Deterministic record ordering for the generated corpus."""

    def __init__(self, start: datetime) -> None:
        self._instant = start

    def __call__(self) -> datetime:
        value = self._instant
        self._instant += timedelta(seconds=1)
        return value


@dataclass(frozen=True, slots=True)
class _CorpusPopulation:
    """Counts observed after the fresh synthetic corpus has been persisted."""

    persisted_record_count: int
    active_slice_count: int


def _record_content(index: int, *, target: bool) -> str:
    """Generate fixed-width, lexical synthetic records without external data."""

    if target:
        return (
            f"{_TARGET_MARKER} reference performance target invariant "
            f"planning query record {index:05d}"
        )
    return (
        f"raw performance filler record {index:05d} "
        "bounded local ledger context selection"
    )


def corpus_fingerprint(config: ProtocolConfig) -> str:
    """Fingerprint corpus shape/content generator, not local record identifiers."""

    return canonical_sha256(
        {
            "generator_id": _CORPUS_GENERATOR_ID,
            "record_count": config.corpus_records,
            "target_index": config.corpus_records - 1,
            "filler_example": _record_content(0, target=False),
            "target_example": _record_content(config.corpus_records - 1, target=True),
            "task": _TASK,
            "scope_shape": {"tenant": "fixed", "user": "fixed", "thread": "fixed"},
        }
    )


def _recall_policy() -> LedgerRecallPolicy:
    """Return the explicit current public planner configuration under test."""

    return LedgerRecallPolicy(
        policy_id="raw-performance-reference-v1",
        allowed_kinds=(MemoryKind.FACT,),
        allowed_origins=(MemoryOrigin.HOST_ASSERTION,),
        minimum_confidence=0.75,
        active_read_limit=PLANNER_ACTIVE_READ_LIMIT,
        candidate_limit=PLANNER_CANDIDATE_LIMIT,
        candidate_scan_byte_budget=PLANNER_CANDIDATE_SCAN_BYTE_BUDGET,
        relevance_weight=100.0,
        confidence_weight=10.0,
        recency_weight=1.0,
        require_admission_audit=True,
    )


def _populate_corpus(database_path: Path, config: ProtocolConfig) -> _CorpusPopulation:
    """Populate a fresh on-disk SQLite Ledger outside all timed operations."""

    ledger = SqliteMemoryLedger(str(database_path))
    ledger.setup()
    try:
        writer = MemoryWriter(
            ledger,
            scope=_SCOPE,
            actor="raw-performance-host",
            clock=_AdvancingClock(_CORPUS_START),
        )
        gate = MemoryReviewGate(
            writer,
            origin=MemoryOrigin.HOST_ASSERTION,
            policy=MemoryAdmissionPolicy.safe_default(),
        )
        for index in range(config.corpus_records):
            marker = f"{index:05d}"
            candidate = gate.ingress(
                kind=MemoryKind.FACT,
                source_ref=f"raw-performance-source-{marker}",
                evidence_refs=(f"raw-performance-evidence-{marker}",),
                confidence=0.95,
                asserted=True,
            ).submit(_record_content(index, target=index == config.corpus_records - 1))
            gate.confirm(
                gate.review(candidate.record_id),
                event_id=f"raw-performance-confirm-{marker}",
            )
        # The public reader limit is intentionally part of the observed setup.
        active = writer.list_active(
            now=_PLANNING_TIME,
            limit=PLANNER_ACTIVE_READ_LIMIT,
        )
        if len(active) != PLANNER_ACTIVE_READ_LIMIT:
            raise PerformanceProtocolError(
                "generated reference corpus did not expose the expected local active slice"
            )
        active_slice_count = len(active)
    finally:
        ledger.close()
    # The fresh disposable database contains only protocol records.  Verify
    # the actual persisted corpus rather than trusting the loop count before
    # allowing a report to call itself a 10k-corpus run.
    verification_connection = sqlite3.connect(str(database_path))
    try:
        row = verification_connection.execute(
            "SELECT COUNT(*) FROM memory_records"
        ).fetchone()
    finally:
        verification_connection.close()
    persisted_record_count = int(row[0]) if row is not None else 0
    if persisted_record_count != config.corpus_records:
        raise PerformanceProtocolError(
            "generated reference corpus did not persist the required record count"
        )
    return _CorpusPopulation(
        persisted_record_count=persisted_record_count,
        active_slice_count=active_slice_count,
    )


def _open_planner(database_path: Path) -> tuple[SqliteMemoryLedger, LedgerRecallPlanner]:
    """Reopen the persisted corpus before warming to avoid timing setup work."""

    ledger = SqliteMemoryLedger(str(database_path))
    ledger.setup()
    writer = MemoryWriter(
        ledger,
        scope=_SCOPE,
        actor="raw-performance-host",
        clock=lambda: _PLANNING_TIME,
    )
    planner = LedgerRecallPlanner(
        writer,
        policy=_recall_policy(),
        counter=RegexTokenCounter(),
        clock=lambda: _PLANNING_TIME,
    )
    return ledger, planner


def _measure_once(
    planner: LedgerRecallPlanner,
    config: ProtocolConfig,
) -> tuple[int, int, dict[str, int | bool]]:
    """Measure one plan and resolve pair with the monotonic high-resolution clock."""

    started_ns = time.perf_counter_ns()
    plan = planner.plan(
        task=_TASK,
        token_budget=config.token_budget,
        byte_budget=config.byte_budget,
    )
    planned_ns = time.perf_counter_ns()
    context = planner.resolve(plan)
    finished_ns = time.perf_counter_ns()

    rendered = context.render_data()
    if _TARGET_MARKER not in rendered:
        raise PerformanceProtocolError("reference target was not selected by the measured plan")
    if plan.active_record_count != PLANNER_ACTIVE_READ_LIMIT:
        raise PerformanceProtocolError("planner did not observe the documented active read cap")
    if not plan.active_read_limit_reached:
        raise PerformanceProtocolError("planner unexpectedly did not report its active read cap")
    if plan.candidate_count != PLANNER_CANDIDATE_LIMIT:
        raise PerformanceProtocolError("planner did not observe the documented candidate limit")
    if plan.candidate_limit_reached:
        raise PerformanceProtocolError(
            "planner candidate limit truncated the required full-corpus observation"
        )
    if plan.scanned_count != PLANNER_CANDIDATE_LIMIT:
        raise PerformanceProtocolError(
            "planner scan byte budget truncated the required full-corpus observation"
        )

    return (
        planned_ns - started_ns,
        finished_ns - planned_ns,
        {
            "planner_active_record_count": plan.active_record_count,
            "active_read_limit_reached": plan.active_read_limit_reached,
            "eligible_record_count": plan.eligible_record_count,
            "candidate_count": plan.candidate_count,
            "candidate_limit_reached": plan.candidate_limit_reached,
            "scanned_count": plan.scanned_count,
            "selected_record_count": context.record_count,
        },
    )


def nearest_rank(samples: Sequence[int], percentile: float) -> int:
    """Return the documented nearest-rank percentile over nanosecond samples."""

    if not samples:
        raise ValueError("samples must not be empty")
    if not isinstance(percentile, (int, float)) or isinstance(percentile, bool):
        raise TypeError("percentile must be a number")
    normalized_percentile = float(percentile)
    if not 0.0 < normalized_percentile <= 1.0:
        raise ValueError("percentile must be greater than 0 and at most 1")
    normalized_samples: list[int] = []
    for sample in samples:
        if isinstance(sample, bool) or not isinstance(sample, int) or sample < 0:
            raise ValueError("samples must be non-negative integer nanoseconds")
        normalized_samples.append(sample)
    ordered = sorted(normalized_samples)
    index = math.ceil(normalized_percentile * len(ordered)) - 1
    return ordered[index]


def summarize_samples(samples_ns: Sequence[int]) -> dict[str, object]:
    """Return raw timing samples and reproducible p50/p95 order statistics."""

    normalized = list(samples_ns)
    if not normalized:
        raise ValueError("samples_ns must not be empty")
    p50_ns = nearest_rank(normalized, 0.50)
    p95_ns = nearest_rank(normalized, 0.95)
    return {
        "unit": "nanoseconds",
        "sample_count": len(normalized),
        "samples_ns": normalized,
        "min_ns": min(normalized),
        "p50_ns": p50_ns,
        "p95_ns": p95_ns,
        "max_ns": max(normalized),
        "p50_ms": round(p50_ns / 1_000_000, 6),
        "p95_ms": round(p95_ns / 1_000_000, 6),
        "percentile_method": "nearest_rank",
    }


def observed_runtime_manifest() -> dict[str, object]:
    """Capture non-secret runtime facts that help reproduce a local report."""

    return {
        "python_implementation": platform.python_implementation(),
        "python_version": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine() or "not-reported",
        "processor": platform.processor() or "not-reported",
        "logical_cpu_count": os.cpu_count(),
        "sqlite_version": sqlite3.sqlite_version,
        "protoprompt_version": __version__,
        "clock": "time.perf_counter_ns",
    }


def _planning_gate(
    *,
    config: ProtocolConfig,
    persisted_record_count: int,
    active_slice_count: int,
    observation: Mapping[str, int | bool],
    planning_summary: Mapping[str, object],
    reference_eligible: bool,
) -> dict[str, object]:
    """Describe the Roadmap target without claiming it when the input is capped."""

    full_corpus_visible = (
        persisted_record_count == config.corpus_records
        and active_slice_count == config.corpus_records
        and observation["planner_active_record_count"] == config.corpus_records
        and observation["active_read_limit_reached"] is True
        and observation["eligible_record_count"] == config.corpus_records
        and observation["candidate_count"] == config.corpus_records
        and observation["candidate_limit_reached"] is False
        and observation["scanned_count"] == config.corpus_records
    )
    p95_ms = planning_summary["p95_ms"]
    assert isinstance(p95_ms, (int, float))
    target = (
        f"p95 planning <= {REFERENCE_P95_PLANNING_TARGET_MS:g} ms on "
        f"{config.corpus_records} local records without remote I/O"
    )
    if not reference_eligible:
        return {
            "roadmap_target": target,
            "status": "not_evaluated",
            "reason": (
                "the verification-only manifest is not eligible to evaluate a roadmap or "
                "reference baseline; its timing remains an implementation diagnostic, even "
                "when full coverage is observed"
            ),
            "diagnostic_full_coverage_observed": full_corpus_visible,
            "diagnostic_target_comparison": (
                "at_or_below_target" if p95_ms <= REFERENCE_P95_PLANNING_TARGET_MS else "above_target"
            ),
            "observed_p95_planning_ms": p95_ms,
        }
    if not full_corpus_visible:
        return {
            "roadmap_target": target,
            "status": "not_evaluated",
            "reason": (
                "full coverage requires persisted, active, eligible, candidate, and scanned "
                f"counts of {config.corpus_records} with no candidate truncation; observed "
                f"persisted={persisted_record_count}, active_slice={active_slice_count}, "
                f"planner_active={observation['planner_active_record_count']}, "
                f"eligible={observation['eligible_record_count']}, "
                f"candidates={observation['candidate_count']}, "
                f"scanned={observation['scanned_count']}, "
                f"candidate_limit_reached={observation['candidate_limit_reached']}; "
                "this report is not exhaustive 10k planning evidence"
            ),
            "observed_p95_planning_ms": planning_summary["p95_ms"],
        }
    return {
        "roadmap_target": target,
        "status": (
            "observed_pass"
            if p95_ms <= REFERENCE_P95_PLANNING_TARGET_MS
            else "observed_fail"
        ),
        "reason": (
            "full active/candidate/scan coverage observed; active_read_limit_reached "
            "is expected at the explicit 10k limit, and persisted count bounds the corpus "
            "to that exact size; single-run observation on the supplied manifest only"
        ),
        "observed_p95_planning_ms": p95_ms,
    }


def build_report(
    *,
    config: ProtocolConfig,
    reference_manifest: Mapping[str, object],
    corpus_population_ns: int,
    persisted_record_count: int,
    active_slice_count: int,
    observation: Mapping[str, int | bool],
    planning_samples_ns: Sequence[int],
    resolution_samples_ns: Sequence[int],
) -> dict[str, object]:
    """Build a content-safe raw measurement report with explicit boundaries."""

    manifest = validate_reference_manifest(reference_manifest)
    planning = summarize_samples(planning_samples_ns)
    resolution = summarize_samples(resolution_samples_ns)
    if planning["sample_count"] != config.measurement_repetitions:
        raise PerformanceProtocolError(
            "planning sample count does not match configured repetitions"
        )
    if resolution["sample_count"] != config.measurement_repetitions:
        raise PerformanceProtocolError(
            "resolution sample count does not match configured repetitions"
        )
    if persisted_record_count != config.corpus_records:
        raise PerformanceProtocolError("persisted corpus count does not match the 10k protocol")
    policy = _recall_policy()
    if not 1 <= active_slice_count <= policy.active_read_limit:
        raise PerformanceProtocolError(
            "active slice count must be within the configured planner limit"
        )
    required_observation = {
        "planner_active_record_count",
        "active_read_limit_reached",
        "eligible_record_count",
        "candidate_count",
        "candidate_limit_reached",
        "scanned_count",
        "selected_record_count",
    }
    if set(observation) != required_observation:
        raise PerformanceProtocolError("measurement observation has an unexpected shape")
    observed = dict(observation)
    integer_observation_fields = {
        "planner_active_record_count",
        "eligible_record_count",
        "candidate_count",
        "scanned_count",
        "selected_record_count",
    }
    boolean_observation_fields = {
        "active_read_limit_reached",
        "candidate_limit_reached",
    }
    if any(
        isinstance(observed[field], bool)
        or not isinstance(observed[field], int)
        or observed[field] < 0
        for field in integer_observation_fields
    ) or any(not isinstance(observed[field], bool) for field in boolean_observation_fields):
        raise PerformanceProtocolError("measurement observation contains invalid values")
    if observed["planner_active_record_count"] != active_slice_count:
        raise PerformanceProtocolError(
            "measured planner active count does not match the populated active slice"
        )
    if (
        observed["eligible_record_count"] > observed["planner_active_record_count"]
        or observed["candidate_count"] > observed["eligible_record_count"]
        or observed["scanned_count"] > observed["candidate_count"]
        or observed["selected_record_count"] > observed["scanned_count"]
    ):
        raise PerformanceProtocolError("measurement observation has impossible count ordering")
    end_to_end_samples = [
        planning_value + resolution_value
        for planning_value, resolution_value in zip(
            planning_samples_ns,
            resolution_samples_ns,
            strict=True,
        )
    ]
    reference_eligible = manifest["manifest_class"] == _REFERENCE_MANIFEST_CLASS
    return {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "protocol_id": PROTOCOL_ID,
        "reference_manifest": manifest,
        "reference_manifest_sha256": canonical_sha256(manifest),
        "reference_evidence": {
            "manifest_class": manifest["manifest_class"],
            "operator_verified": manifest["operator_verified"],
            "reference_baseline_eligible": reference_eligible,
            "publication_or_universal_claim": "not_made",
        },
        "observed_runtime": observed_runtime_manifest(),
        "methodology": {
            **asdict(config),
            "corpus_generator_id": _CORPUS_GENERATOR_ID,
            "corpus_fingerprint": corpus_fingerprint(config),
            "storage": "fresh_disposable_sqlite_v6_file",
            "storage_directory": "explicit_operator_selected_not_reported",
            "storage_locality": (
                "operator_attested_local"
                if reference_eligible
                else "verification_only_unverified"
            ),
            "cold_reopen_before_warmup": True,
            "provider_embedding_or_remote_database_io": False,
            "embedding_calls": False,
            "model_calls": False,
            "warmup_samples_retained": False,
            "timed_operations": ["ledger_recall_plan", "ledger_recall_resolve"],
        },
        "corpus": {
            "persisted_record_count": persisted_record_count,
            "scope_count": 1,
            "active_slice_count": active_slice_count,
            "synthetic": True,
            "plaintext_in_report": False,
        },
        "effective_policy": policy.explain(),
        "measurement_observation": observed,
        "setup": {
            "corpus_population_ns": corpus_population_ns,
            "corpus_population_ms": round(corpus_population_ns / 1_000_000, 6),
            "included_in_planning_percentiles": False,
        },
        "measurements": {
            "ledger_recall_plan": planning,
            "ledger_recall_resolve": resolution,
            "end_to_end_plan_and_resolve": summarize_samples(end_to_end_samples),
        },
        "roadmap_planning_gate": _planning_gate(
            config=config,
            persisted_record_count=persisted_record_count,
            active_slice_count=active_slice_count,
            observation=observed,
            planning_summary=planning,
            reference_eligible=reference_eligible,
        ),
        "interpretation": {
            "runtime_claim": "not_made",
            "universal_claim": "not_made",
            "reference_baseline_claim": (
                "operator_verified_configuration_only"
                if reference_eligible
                else "not_eligible_verification_only"
            ),
            "not_claimed": [
                "model answer quality",
                "general semantic recall",
                "throughput",
                "provider or embedding latency",
                "cross-hardware performance",
                "comparison with another framework",
                "full-10k exhaustive planner coverage unless roadmap_planning_gate is observed",
            ],
        },
    }


def run_raw_performance_protocol(
    *,
    config: ProtocolConfig,
    reference_manifest: Mapping[str, object],
    storage_directory: Path | str,
) -> dict[str, object]:
    """Run one complete local-only raw measurement protocol.

    Corpus creation is deliberately outside measurement.  The database is
    closed and reopened once before the warm-up phase; five warm-up iterations
    are discarded, then 30 or more plan/resolve pairs are measured.
    """

    manifest = validate_reference_manifest(reference_manifest)
    benchmark_directory = validate_storage_directory(storage_directory)
    with tempfile.TemporaryDirectory(
        prefix="protoprompt-raw-performance-",
        dir=str(benchmark_directory),
    ) as directory:
        database_path = Path(directory) / "ledger.sqlite3"
        population_started_ns = time.perf_counter_ns()
        corpus_population = _populate_corpus(database_path, config)
        population_finished_ns = time.perf_counter_ns()

        ledger, planner = _open_planner(database_path)
        try:
            for _ in range(config.warmup_iterations):
                _measure_once(planner, config)

            planning_samples_ns: list[int] = []
            resolution_samples_ns: list[int] = []
            observed: dict[str, int | bool] | None = None
            for _ in range(config.measurement_repetitions):
                planning_ns, resolution_ns, current_observation = _measure_once(planner, config)
                if observed is None:
                    observed = current_observation
                elif observed != current_observation:
                    raise PerformanceProtocolError(
                        "local planning observation changed between repetitions"
                    )
                planning_samples_ns.append(planning_ns)
                resolution_samples_ns.append(resolution_ns)
        finally:
            ledger.close()

    if observed is None:  # pragma: no cover - config validation requires repetitions
        raise PerformanceProtocolError("measurement produced no observations")
    return build_report(
        config=config,
        reference_manifest=manifest,
        corpus_population_ns=population_finished_ns - population_started_ns,
        persisted_record_count=corpus_population.persisted_record_count,
        active_slice_count=corpus_population.active_slice_count,
        observation=observed,
        planning_samples_ns=planning_samples_ns,
        resolution_samples_ns=resolution_samples_ns,
    )


def render_markdown_report(report: Mapping[str, object]) -> str:
    """Render a compact report that keeps the no-universal-claim boundary visible."""

    if report.get("protocol_id") != PROTOCOL_ID:
        raise PerformanceProtocolError("cannot render a report from another protocol")
    methodology = report.get("methodology")
    measurements = report.get("measurements")
    gate = report.get("roadmap_planning_gate")
    interpretation = report.get("interpretation")
    reference_evidence = report.get("reference_evidence")
    if not all(
        isinstance(value, Mapping)
        for value in (methodology, measurements, gate, interpretation, reference_evidence)
    ):
        raise PerformanceProtocolError("raw performance report has an invalid shape")
    assert isinstance(methodology, Mapping)
    assert isinstance(measurements, Mapping)
    assert isinstance(gate, Mapping)
    assert isinstance(interpretation, Mapping)
    assert isinstance(reference_evidence, Mapping)
    reference_eligible = reference_evidence.get("reference_baseline_eligible") is True
    manifest_status = (
        "Operator-verified reference configuration; publication and universal claims remain unmade."
        if reference_eligible
        else "**Verification-only result — not eligible as a reference baseline or comparison.**"
    )
    lines = [
        "# ProtoPrompt raw local performance protocol",
        "",
        f"Protocol: `{PROTOCOL_ID}`  ",
        f"Reference manifest SHA-256: `{report['reference_manifest_sha256']}`  ",
        "This is one local raw measurement, not a universal performance claim.",
        manifest_status,
        "",
        "## Methodology",
        "",
        (
            f"- Corpus: `{methodology['corpus_records']}` deterministic synthetic "
            "local Ledger records."
        ),
        f"- Warm-up: `{methodology['warmup_iterations']}` discarded plan/resolve pairs.",
        f"- Measurement: `{methodology['measurement_repetitions']}` retained repetitions.",
        "- Clock: `time.perf_counter_ns`; percentiles use nearest-rank over raw nanoseconds.",
        "- No model, embedding, provider, or remote I/O is called by this protocol.",
        "",
        "## Raw timings",
        "",
        "| Operation | Samples | p50 (ms) | p95 (ms) |",
        "|---|---:|---:|---:|",
    ]
    for operation in (
        "ledger_recall_plan",
        "ledger_recall_resolve",
        "end_to_end_plan_and_resolve",
    ):
        summary = measurements.get(operation)
        if not isinstance(summary, Mapping):
            raise PerformanceProtocolError(f"missing timing summary {operation}")
        lines.append(
            "| "
            f"`{operation}` | {summary['sample_count']} | "
            f"{summary['p50_ms']:.6f} | {summary['p95_ms']:.6f} |"
        )
    lines.extend(
        [
            "",
            "## Roadmap gate interpretation",
            "",
            f"- Status: `{gate['status']}`.",
            f"- Target: {gate['roadmap_target']}.",
            f"- Reason: {gate['reason']}.",
            "",
            "## Boundary",
            "",
            (
                "The report does not claim model quality, general recall, throughput, provider "
                "latency, cross-hardware performance, or superiority over another framework."
            ),
            (
                "Compare only runs with the exact reviewed reference-manifest and source revision, "
                "and retain the raw JSON samples."
            ),
        ]
    )
    return "\n".join(lines) + "\n"


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the local-only 10k Ledger raw performance evidence protocol.",
    )
    parser.add_argument(
        "--reference-manifest",
        type=Path,
        help="operator-reviewed JSON hardware/software manifest required to measure",
    )
    parser.add_argument(
        "--init-reference-manifest",
        type=Path,
        help="write a non-runnable manifest template and exit",
    )
    parser.add_argument("--json", type=Path, help="explicit path for the raw JSON report")
    parser.add_argument("--markdown", type=Path, help="explicit path for the Markdown report")
    parser.add_argument(
        "--storage-directory",
        type=Path,
        help="existing operator-selected local directory for the disposable SQLite file",
    )
    parser.add_argument(
        "--warmups",
        type=int,
        default=DEFAULT_WARMUP_ITERATIONS,
        help=f"discarded warm-up iterations (minimum {DEFAULT_WARMUP_ITERATIONS})",
    )
    parser.add_argument(
        "--repetitions",
        type=int,
        default=DEFAULT_MEASUREMENT_REPETITIONS,
        help=f"retained repetitions (minimum {DEFAULT_MEASUREMENT_REPETITIONS})",
    )
    arguments = parser.parse_args(argv)
    if arguments.init_reference_manifest is not None:
        if (
            arguments.reference_manifest is not None
            or arguments.json is not None
            or arguments.markdown is not None
            or arguments.storage_directory is not None
        ):
            parser.error("--init-reference-manifest cannot be combined with a measurement output")
        return arguments
    if arguments.reference_manifest is None:
        parser.error("--reference-manifest is required for a measurement")
    if arguments.json is None and arguments.markdown is None:
        parser.error("choose at least one explicit output path: --json or --markdown")
    if arguments.storage_directory is None:
        parser.error("--storage-directory is required for a measurement")
    try:
        validate_output_destinations(
            reference_manifest=arguments.reference_manifest,
            json_output=arguments.json,
            markdown_output=arguments.markdown,
        )
        validate_storage_directory(arguments.storage_directory)
    except PerformanceProtocolError as exc:
        parser.error(str(exc))
    return arguments


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point; writes only explicitly requested local report paths."""

    arguments = _parse_args(argv)
    if arguments.init_reference_manifest is not None:
        _write_json(arguments.init_reference_manifest, reference_manifest_template())
        print(
            "wrote non-runnable reference manifest template; fill all placeholders "
            "and set operator_verified=true before measuring"
        )
        return 0
    try:
        config = ProtocolConfig(
            warmup_iterations=arguments.warmups,
            measurement_repetitions=arguments.repetitions,
        )
        manifest = _read_reference_manifest(arguments.reference_manifest)
        report = run_raw_performance_protocol(
            config=config,
            reference_manifest=manifest,
            storage_directory=arguments.storage_directory,
        )
        if arguments.json is not None:
            _write_json(arguments.json, report)
        if arguments.markdown is not None:
            _write_text(arguments.markdown, render_markdown_report(report))
    except (PerformanceProtocolError, ReferenceManifestError, ValueError) as exc:
        print(f"raw performance protocol failed: {exc}", file=sys.stderr)
        return 1
    print(
        "raw local performance protocol completed; report is hardware/source specific "
        "and makes no universal runtime claim"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
