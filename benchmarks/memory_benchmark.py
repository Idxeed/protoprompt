"""Deterministic offline regression suite for memory and context planning.

This module deliberately measures semantic outcomes rather than wall-clock
performance.  It has no network, model, or optional-package dependency, so it
can be run in CI and used as a stable regression gate.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import tempfile
from typing import Any, Mapping, Sequence

from protoprompt import (
    ContextInput,
    MemoryScope,
    RegexTokenCounter,
    SqliteStore,
    TokenBudgetedContextBuilder,
)
from protoprompt.connectivity import MemoryService
from protoprompt.rag.retriever import Retriever
from protoprompt.scope import scoped_doc_id
from benchmarks.ledger_composition_benchmark import (
    render_ledger_composition_markdown,
    run_ledger_composition_suite,
    validate_ledger_composition_suite,
)
from benchmarks.ledger_checkpoint_benchmark import (
    render_ledger_checkpoint_markdown,
    run_ledger_checkpoint_suite,
    validate_ledger_checkpoint_suite,
)


REPORT_SCHEMA_VERSION = 1
DEFAULT_SUITE = "v0.1"
EMBEDDING_ALGORITHM = "seeded-blake2b-feature-hash-v1"
LEGACY_REFERENCE_VERSION = "0.6.1"
LEGACY_REFERENCE_COMMIT = "fee6272856ba52f2cb157acd5820749678ec95c0"
IMPLEMENTATIONS = (
    ("tail_window_v1", "baseline", "runtime"),
    ("rolling_summary_v1", "baseline", "runtime"),
    ("vector_recall_v1", "baseline", "runtime"),
    ("protoprompt_0_6_1", "baseline", "frozen_reference"),
    ("protoprompt_context_plan_v0_7", "candidate", "runtime"),
)

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_-]+", re.ASCII)
_SUITE_RE = re.compile(r"v[0-9]+\.[0-9]+")
_FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures"


class BenchmarkFixtureError(ValueError):
    """Raised when a benchmark fixture is malformed or unsupported."""


class BenchmarkVerificationError(AssertionError):
    """Raised when a semantic benchmark result deviates from its baseline."""


class SeededFeatureHashEmbeddings:
    """Small stable embedding client used only by this offline suite.

    The representation is intentionally transparent: case-folded word tokens
    map through keyed BLAKE2b into signed feature buckets and are L2
    normalized.  It is a deterministic retrieval contour, not an embedding
    quality claim about an LLM or an external model.
    """

    def __init__(self, *, seed: str, dimensions: int) -> None:
        if not seed:
            raise ValueError("embedding seed must not be empty")
        if dimensions < 2:
            raise ValueError("embedding dimensions must be at least 2")
        self._seed = seed.encode("utf-8")
        self.dimensions = dimensions

    async def embed(self, texts: list[str], model: str = "") -> list[list[float]]:
        return [self.embed_one(text) for text in texts]

    def bucket_for_token(self, token: str) -> tuple[int, float]:
        digest = hashlib.blake2b(
            token.casefold().encode("utf-8"),
            key=self._seed,
            digest_size=16,
            person=b"pp-bench-v1",
        ).digest()
        value = int.from_bytes(digest[:8], "little")
        return value % self.dimensions, -1.0 if value & 1 else 1.0

    def embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimensions
        for token in _TOKEN_RE.findall(text.casefold()):
            bucket, sign = self.bucket_for_token(token)
            vector[bucket] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0.0:
            return vector
        return [value / norm for value in vector]


def canonical_json(value: object) -> str:
    """Serialize JSON-safe data into the fixture's canonical representation."""
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def canonical_sha256(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def fixture_sha256(suite: Mapping[str, Any]) -> str:
    """Return the immutable suite fingerprint embedded in every report."""
    return canonical_sha256(suite)


def _fixture_directory(
    suite_name: str,
    fixture_root: Path | None = None,
) -> Path:
    if not _SUITE_RE.fullmatch(suite_name):
        raise BenchmarkFixtureError("suite must look like v0.1")
    root = (fixture_root or _FIXTURE_ROOT).resolve()
    target = (root / suite_name).resolve()
    if root != target and root not in target.parents:
        raise BenchmarkFixtureError("suite path escapes the fixture root")
    return target


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BenchmarkFixtureError(f"missing fixture: {path}") from exc
    except json.JSONDecodeError as exc:
        raise BenchmarkFixtureError(f"invalid JSON fixture {path}: {exc.msg}") from exc
    if not isinstance(value, dict):
        raise BenchmarkFixtureError(f"fixture {path} must contain a JSON object")
    return value


def _require_string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise BenchmarkFixtureError(f"{label} must be a non-empty string")
    return value


def _validate_suite(suite: Mapping[str, Any]) -> None:
    if suite.get("suite_kind") == "ledger_context_composition":
        validate_ledger_composition_suite(suite)
        return
    if suite.get("suite_kind") == "ledger_sealed_checkpoint":
        validate_ledger_checkpoint_suite(suite)
        return
    if suite.get("schema_version") != 1:
        raise BenchmarkFixtureError("unsupported suite schema version")
    if not _SUITE_RE.fullmatch(str(suite.get("suite_version", ""))):
        raise BenchmarkFixtureError("suite_version must look like v0.1")
    embedding = suite.get("embedding")
    if not isinstance(embedding, Mapping):
        raise BenchmarkFixtureError("embedding must be an object")
    if embedding.get("algorithm") != EMBEDDING_ALGORITHM:
        raise BenchmarkFixtureError("unsupported embedding algorithm")
    _require_string(embedding.get("seed"), "embedding.seed")
    dimensions = embedding.get("dimensions")
    if not isinstance(dimensions, int) or dimensions < 2:
        raise BenchmarkFixtureError("embedding.dimensions must be an integer >= 2")

    plan = suite.get("context_plan")
    if not isinstance(plan, Mapping):
        raise BenchmarkFixtureError("context_plan must be an object")
    for name in ("max_tokens", "output_reserve_tokens", "top_k_rag"):
        value = plan.get(name)
        if not isinstance(value, int) or value < 0:
            raise BenchmarkFixtureError(f"context_plan.{name} must be non-negative")
    if int(plan["output_reserve_tokens"]) > int(plan["max_tokens"]):
        raise BenchmarkFixtureError("output reserve exceeds max tokens")

    sqlite = suite.get("sqlite")
    if not isinstance(sqlite, Mapping) or not isinstance(
        sqlite.get("cold_reopen_after_ingest"), bool
    ):
        raise BenchmarkFixtureError("sqlite.cold_reopen_after_ingest must be boolean")
    baselines = suite.get("baselines")
    if not isinstance(baselines, Mapping):
        raise BenchmarkFixtureError("baselines must be an object")
    tail = baselines.get("tail_window_v1")
    summary = baselines.get("rolling_summary_v1")
    vector = baselines.get("vector_recall_v1")
    if not isinstance(tail, Mapping) or not isinstance(tail.get("window_records"), int):
        raise BenchmarkFixtureError("tail_window_v1.window_records must be an integer")
    if int(tail["window_records"]) < 1:
        raise BenchmarkFixtureError("tail_window_v1.window_records must be positive")
    if not isinstance(summary, Mapping) or not isinstance(summary.get("max_keys"), int):
        raise BenchmarkFixtureError("rolling_summary_v1.max_keys must be an integer")
    if int(summary["max_keys"]) < 1:
        raise BenchmarkFixtureError("rolling_summary_v1.max_keys must be positive")
    if not isinstance(vector, Mapping) or not isinstance(vector.get("top_k"), int):
        raise BenchmarkFixtureError("vector_recall_v1.top_k must be an integer")
    if int(vector["top_k"]) < 1 or not isinstance(vector.get("score_threshold"), (int, float)):
        raise BenchmarkFixtureError("vector_recall_v1 has an invalid retrieval setting")
    packer = baselines.get("request_packer")
    if not isinstance(packer, Mapping) or packer.get("algorithm") != "greedy-final-request-packer-v1":
        raise BenchmarkFixtureError("unsupported baseline request packer")
    guard = suite.get("collision_guard")
    if not isinstance(guard, Mapping):
        raise BenchmarkFixtureError("collision_guard must be an object")
    for name in ("min_target_cosine", "max_non_target_cosine", "min_target_margin"):
        if not isinstance(guard.get(name), (int, float)):
            raise BenchmarkFixtureError(f"collision_guard.{name} must be numeric")

    cases = suite.get("cases")
    if not isinstance(cases, list) or not cases:
        raise BenchmarkFixtureError("cases must be a non-empty list")
    seen: set[str] = set()
    for index, case in enumerate(cases):
        if not isinstance(case, Mapping):
            raise BenchmarkFixtureError(f"case {index} must be an object")
        case_id = _require_string(case.get("id"), f"cases[{index}].id")
        if case_id in seen:
            raise BenchmarkFixtureError(f"duplicate case id {case_id!r}")
        seen.add(case_id)
        if case.get("category") not in {"recall", "scope", "final_request"}:
            raise BenchmarkFixtureError(f"case {case_id!r} has an unknown category")
        _require_string(case.get("query"), f"case {case_id}.query")
        _require_string(case.get("target_memory_id"), f"case {case_id}.target_memory_id")
        _require_string(case.get("sentinel"), f"case {case_id}.sentinel")
        if not isinstance(case.get("scope"), Mapping):
            raise BenchmarkFixtureError(f"case {case_id}.scope must be an object")
        records = case.get("records")
        if not isinstance(records, list) or not records:
            raise BenchmarkFixtureError(f"case {case_id}.records must be a non-empty list")
        for record_index, record in enumerate(records):
            if not isinstance(record, Mapping):
                raise BenchmarkFixtureError(
                    f"case {case_id}.records[{record_index}] must be an object"
                )
            _require_string(
                record.get("memory_id"),
                f"case {case_id}.records[{record_index}].memory_id",
            )
            _require_string(
                record.get("text"),
                f"case {case_id}.records[{record_index}].text",
            )
        foreign_records = case.get("foreign_records", [])
        if not isinstance(foreign_records, list):
            raise BenchmarkFixtureError(f"case {case_id}.foreign_records must be a list")
        for foreign_index, record in enumerate(foreign_records):
            if not isinstance(record, Mapping) or not isinstance(record.get("scope"), Mapping):
                raise BenchmarkFixtureError(
                    f"case {case_id}.foreign_records[{foreign_index}] must provide a scope"
                )
            _require_string(
                record.get("memory_id"),
                f"case {case_id}.foreign_records[{foreign_index}].memory_id",
            )
            _require_string(
                record.get("text"),
                f"case {case_id}.foreign_records[{foreign_index}].text",
            )
        if case["category"] == "scope":
            primary_scope = case["scope"]
            changed_dimensions = {
                tuple(
                    name
                    for name in ("tenant", "user", "thread")
                    if record["scope"].get(name, "") != primary_scope.get(name, "")
                )
                for record in foreign_records
            }
            if changed_dimensions != {("tenant",), ("user",), ("thread",)}:
                raise BenchmarkFixtureError(
                    "scope case must independently probe tenant, user, and thread"
                )
            foreign_sentinels = case.get("foreign_sentinels")
            if (
                not isinstance(foreign_sentinels, list)
                or len(foreign_sentinels) != len(foreign_records)
                or not all(isinstance(item, str) and item for item in foreign_sentinels)
            ):
                raise BenchmarkFixtureError(
                    "scope case must declare one non-empty foreign sentinel per probe"
                )
        request = case.get("plan_request")
        if not isinstance(request, Mapping):
            raise BenchmarkFixtureError(f"case {case_id}.plan_request must be an object")
        _require_string(request.get("system_prompt"), f"case {case_id}.system_prompt")
        if not isinstance(request.get("history"), list):
            raise BenchmarkFixtureError(f"case {case_id}.history must be a list")
        if not isinstance(request.get("final_messages"), list):
            raise BenchmarkFixtureError(f"case {case_id}.final_messages must be a list")
        if not all(isinstance(item, Mapping) for item in request["history"]):
            raise BenchmarkFixtureError(f"case {case_id}.history must contain objects")
        if not all(isinstance(item, Mapping) for item in request["final_messages"]):
            raise BenchmarkFixtureError(
                f"case {case_id}.final_messages must contain objects"
            )


def load_suite(
    suite_name: str = DEFAULT_SUITE,
    *,
    fixture_root: Path | None = None,
) -> dict[str, Any]:
    """Load and validate an immutable versioned benchmark suite."""
    suite = _read_json(_fixture_directory(suite_name, fixture_root) / "suite.json")
    _validate_suite(suite)
    return suite


def load_expected(
    suite_name: str = DEFAULT_SUITE,
    *,
    fixture_root: Path | None = None,
    path: Path | None = None,
) -> dict[str, Any]:
    """Load the frozen semantic outcome for a versioned suite."""
    expected = _read_json(
        path
        if path is not None
        else _fixture_directory(suite_name, fixture_root) / "expected.json"
    )
    if expected.get("report_schema_version") != REPORT_SCHEMA_VERSION:
        raise BenchmarkFixtureError("unsupported expected-report schema version")
    if not isinstance(expected.get("report"), Mapping):
        raise BenchmarkFixtureError("expected fixture must contain a report object")
    return expected


def _scope(value: Mapping[str, Any]) -> MemoryScope:
    fields = {name: str(value.get(name, "")) for name in ("tenant", "user", "thread", "kind")}
    scope = MemoryScope(**fields)
    if scope.is_empty:
        raise BenchmarkFixtureError("benchmark scope must not be empty")
    return scope


def _result(
    *,
    status: str,
    evidence_available: bool | None = None,
    target_rank: int | None = None,
    scope_leak_count: int | None = None,
    budget_violation_count: int | None = None,
    receipt_reconciles: bool | None = None,
    legacy_budget_report_reconciles: bool | None = None,
    decision_contract_coverage: float | None = None,
    explain_content_leak: bool | None = None,
    source_ref_present: bool | None = None,
    tool_dependency_preserved: bool | None = None,
    channel_evidence: Mapping[str, bool] | None = None,
    cold_reopen: bool | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "evidence_available": evidence_available,
        "target_rank": target_rank,
        "scope_leak_count": scope_leak_count,
        "budget_violation_count": budget_violation_count,
        "receipt_reconciles": receipt_reconciles,
        "legacy_budget_report_reconciles": legacy_budget_report_reconciles,
        "decision_contract_coverage": decision_contract_coverage,
        "explain_content_leak": explain_content_leak,
        "source_ref_present": source_ref_present,
        "tool_dependency_preserved": tool_dependency_preserved,
        "channel_evidence": dict(channel_evidence) if channel_evidence else None,
        "cold_reopen": cold_reopen,
    }


def _not_supported() -> dict[str, Any]:
    return _result(status="not_supported")


def _primary_records(case: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return list(case["records"])


def _select_tail(records: Sequence[Mapping[str, Any]], count: int) -> list[str]:
    return [str(record["text"]) for record in records[-count:]]


def _pack_baseline_request(
    context_blocks: Sequence[str],
    case: Mapping[str, Any],
    suite: Mapping[str, Any],
) -> tuple[str, int]:
    """Pack a baseline payload under the same final-request ceiling.

    This intentionally small, deterministic packer is not an implementation
    of ContextPlan. It makes the comparison fairer by accounting for the
    mandatory final input, provider framing, system text, optional evidence,
    optional history, and output reserve before a baseline can report a zero
    budget violation. Optional evidence is kept in supplied priority order;
    optional history is then admitted newest-first one message at a time.
    """
    counter = RegexTokenCounter()
    config = suite["context_plan"]
    request = case["plan_request"]
    max_tokens = int(config["max_tokens"])
    reserve = int(config["output_reserve_tokens"])
    final_messages = [dict(item) for item in request["final_messages"]]
    history = [dict(item) for item in request["history"]]
    system_prompt = str(request["system_prompt"])

    def messages_for(
        blocks: Sequence[str], history_items: Sequence[Mapping[str, Any]],
    ) -> list[dict[str, Any]]:
        system = system_prompt
        if blocks:
            system = system + "\n\n" + "\n\n---\n\n".join(blocks)
        return [
            {"role": "system", "content": system},
            *[dict(item) for item in history_items],
            *final_messages,
        ]

    selected_blocks: list[str] = []
    # A final input that cannot fit is a true violation rather than something
    # this baseline may truncate or hide.
    if counter.count_messages(messages_for([], [])) + reserve > max_tokens:
        return "", 1
    for block in context_blocks:
        candidate = [*selected_blocks, block]
        if counter.count_messages(messages_for(candidate, [])) + reserve <= max_tokens:
            selected_blocks.append(block)

    selected_history: list[dict[str, Any]] = []
    for item in reversed(history):
        candidate = [dict(item), *selected_history]
        if (
            counter.count_messages(messages_for(selected_blocks, candidate)) + reserve
            <= max_tokens
        ):
            selected_history = candidate
    total = counter.count_messages(messages_for(selected_blocks, selected_history)) + reserve
    return "\n".join(selected_blocks), int(total > max_tokens)


def _tail_window_result(case: Mapping[str, Any], suite: Mapping[str, Any]) -> dict[str, Any]:
    if case["category"] != "recall":
        return _not_supported()
    count = int(suite["baselines"]["tail_window_v1"]["window_records"])
    selected, budget_violation_count = _pack_baseline_request(
        _select_tail(_primary_records(case), count), case, suite
    )
    return _result(
        status="ok",
        evidence_available=str(case["sentinel"]) in selected,
        budget_violation_count=budget_violation_count,
    )


def _rolling_summary_result(
    case: Mapping[str, Any],
    suite: Mapping[str, Any],
) -> dict[str, Any]:
    if case["category"] != "recall":
        return _not_supported()
    ledger: dict[str, str] = {}
    for record in _primary_records(case):
        summary = record.get("summary")
        if isinstance(summary, Mapping):
            key = _require_string(summary.get("key"), "record.summary.key")
            value = _require_string(summary.get("value"), "record.summary.value")
            ledger[key] = value
    # This is intentionally a transparent baseline, not an LLM-generated
    # summary: one fixture-supplied key/value per record, latest write wins.
    # Its fixed key ceiling makes the policy auditable and bounded.
    max_keys = int(suite["baselines"]["rolling_summary_v1"]["max_keys"])
    rendered = "\n".join(
        f"{key}={ledger[key]}" for key in sorted(ledger)[:max_keys]
    )
    packed, budget_violation_count = _pack_baseline_request([rendered], case, suite)
    return _result(
        status="ok",
        evidence_available=str(case["sentinel"]) in packed,
        budget_violation_count=budget_violation_count,
    )


async def _remember_records(
    store: SqliteStore,
    embeddings: SeededFeatureHashEmbeddings,
    case: Mapping[str, Any],
) -> None:
    primary_scope = _scope(case["scope"])
    primary = MemoryService(store, embeddings, primary_scope)
    for record in _primary_records(case):
        await primary.remember(
            str(record["text"]),
            memory_id=str(record["memory_id"]),
            metadata={"benchmark_record_id": str(record["memory_id"])},
        )
    for record in case.get("foreign_records", []):
        if not isinstance(record, Mapping) or not isinstance(record.get("scope"), Mapping):
            raise BenchmarkFixtureError("foreign record must provide a scope")
        foreign = MemoryService(store, embeddings, _scope(record["scope"]))
        await foreign.remember(
            _require_string(record.get("text"), "foreign record text"),
            memory_id=_require_string(record.get("memory_id"), "foreign memory id"),
            metadata={"benchmark_record_id": str(record["memory_id"])},
        )
    session = case.get("session")
    if isinstance(session, Mapping):
        chat_id = _require_string(session.get("chat_id"), "session.chat_id")
        text = _require_string(session.get("text"), "session.text")
        vector = (await embeddings.embed([text]))[0]
        store.add(
            scoped_doc_id(f"session_{chat_id}", primary_scope),
            [text],
            [vector],
            primary_scope.merge_metadata({"kind": "session"}),
        )


def _rank(hits: Sequence[Mapping[str, Any]], target_memory_id: str) -> int | None:
    for index, hit in enumerate(hits, start=1):
        if str(hit.get("memory_id", "")) == target_memory_id:
            return index
    return None


def _foreign_leaks(
    case: Mapping[str, Any],
    texts: Sequence[str],
) -> int:
    raw_sentinels = case.get("foreign_sentinels", [])
    if not isinstance(raw_sentinels, list):
        raise BenchmarkFixtureError("foreign_sentinels must be a list")
    sentinels = [sentinel for sentinel in raw_sentinels if isinstance(sentinel, str) and sentinel]
    if not sentinels:
        return 0
    return sum(any(sentinel in text for sentinel in sentinels) for text in texts)


async def _with_reopened_store(
    case: Mapping[str, Any],
    suite: Mapping[str, Any],
    embeddings: SeededFeatureHashEmbeddings,
    evaluator,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="protoprompt-memory-benchmark-") as directory:
        database = Path(directory) / "memory.sqlite3"
        store = SqliteStore(str(database))
        try:
            await _remember_records(store, embeddings, case)
            if bool(suite["sqlite"]["cold_reopen_after_ingest"]):
                store.close()
                store = SqliteStore(str(database))
            return await evaluator(store)
        finally:
            store.close()


async def _vector_recall_result(
    case: Mapping[str, Any],
    suite: Mapping[str, Any],
    embeddings: SeededFeatureHashEmbeddings,
) -> dict[str, Any]:
    if case["category"] == "final_request":
        return _not_supported()

    async def evaluate(store: SqliteStore) -> dict[str, Any]:
        service = MemoryService(store, embeddings, _scope(case["scope"]))
        top_k = int(suite["baselines"]["vector_recall_v1"]["top_k"])
        hits = await service.search(
            str(case["query"]),
            top_k=top_k,
            score_threshold=float(suite["baselines"]["vector_recall_v1"]["score_threshold"]),
        )
        texts = [str(hit["text"]) for hit in hits]
        packed, budget_violation_count = _pack_baseline_request(texts, case, suite)
        return _result(
            status="ok",
            evidence_available=str(case["sentinel"]) in packed,
            target_rank=_rank(hits, str(case["target_memory_id"])),
            scope_leak_count=_foreign_leaks(case, texts) if case["category"] == "scope" else None,
            budget_violation_count=budget_violation_count,
            cold_reopen=bool(suite["sqlite"]["cold_reopen_after_ingest"]),
        )

    return await _with_reopened_store(case, suite, embeddings, evaluate)


def _explain_private_markers(case: Mapping[str, Any]) -> list[str]:
    markers = [str(value) for value in case.get("explain_private_markers", [])]
    markers.append(str(case["target_memory_id"]))
    scope = case["scope"]
    if isinstance(scope, Mapping):
        markers.extend(str(scope.get(name, "")) for name in ("tenant", "user", "thread"))
    session = case.get("session")
    if isinstance(session, Mapping):
        markers.append(str(session.get("chat_id", "")))
    for record in case.get("foreign_records", []):
        if isinstance(record, Mapping) and isinstance(record.get("scope"), Mapping):
            markers.extend(
                str(record["scope"].get(name, ""))
                for name in ("tenant", "user", "thread")
            )
    return [marker for marker in markers if marker]


def _tool_dependency_preserved(
    rendered: Sequence[Mapping[str, Any]],
    history: Sequence[Mapping[str, Any]],
) -> bool | None:
    tool_history = [
        item
        for item in history
        if item.get("role") == "tool" or item.get("tool_calls")
    ]
    if not tool_history:
        return None
    expected = [dict(item) for item in tool_history]
    return any(
        list(rendered[index:index + len(expected)]) == expected
        for index in range(len(rendered) - len(expected) + 1)
    )


async def _context_plan_result(
    case: Mapping[str, Any],
    suite: Mapping[str, Any],
    embeddings: SeededFeatureHashEmbeddings,
) -> dict[str, Any]:
    counter = RegexTokenCounter()
    config = suite["context_plan"]
    request = case["plan_request"]

    async def evaluate(store: SqliteStore) -> dict[str, Any]:
        primary_scope = _scope(case["scope"])
        retriever = Retriever(
            store,
            embeddings,
            document_kind="memory",
            scope=primary_scope,
        )
        builder = TokenBudgetedContextBuilder(
            store,
            embeddings,
            counter=counter,
            max_tokens=int(config["max_tokens"]),
            output_reserve=int(config["output_reserve_tokens"]),
            scope=primary_scope,
            retriever=retriever,
        )
        session = case.get("session")
        chat_id = str(session.get("chat_id", "")) if isinstance(session, Mapping) else ""
        plan = await builder.plan_messages(
            ContextInput(
                query=str(case["query"]),
                chat_id=chat_id,
                system_prompt=str(request["system_prompt"]),
                score_threshold=float(config["score_threshold"]),
                top_k_rag=int(config["top_k_rag"]),
                top_k_session=1,
                include_rag=True,
                include_session=bool(session),
            ),
            history=list(request["history"]),
            final_messages=list(request["final_messages"]),
        )
        rendered = plan.render_messages()
        evidence_text = "\n".join([*plan.rag_blocks, *plan.session_blocks])
        planned_target_rank = next(
            (
                index
                for index, block in enumerate(plan.rag_blocks, start=1)
                if str(case["sentinel"]) in block
            ),
            None,
        )
        receipt = plan.receipt
        assert receipt is not None
        receipt_reconciles = (
            receipt.input_tokens
            + receipt.output_reserve_tokens
            + receipt.remaining_tokens
            == receipt.max_tokens
            and receipt.context_tokens
            + receipt.history_tokens
            + receipt.final_input_tokens
            + receipt.output_reserve_tokens
            + receipt.remaining_tokens
            == receipt.max_tokens
        )
        decisions = plan.decisions
        covered = [
            bool(item.block_id and item.origin and item.decision and item.reason)
            and item.token_cost >= 0
            for item in decisions
        ]
        coverage = len(covered) and sum(covered) / len(covered)
        explanation = canonical_json(plan.explain())
        private_markers = _explain_private_markers(case)
        source_ref_present = any(
            item.origin == "rag"
            and isinstance(item.source_id, str)
            and item.source_id.startswith("source:")
            for item in decisions
        )
        texts = [*plan.rag_blocks, *plan.session_blocks]
        channels: dict[str, bool] = {"rag": str(case["sentinel"]) in "\n".join(plan.rag_blocks)}
        if isinstance(session, Mapping):
            channels["session"] = str(session["sentinel"]) in "\n".join(plan.session_blocks)
        return _result(
            status="ok",
            evidence_available=str(case["sentinel"]) in evidence_text,
            # This rank is derived from the rendered ContextPlan selection,
            # not from a second independent MemoryService query.
            target_rank=planned_target_rank,
            scope_leak_count=(
                _foreign_leaks(case, texts) if case["category"] == "scope" else None
            ),
            budget_violation_count=int(not receipt_reconciles),
            receipt_reconciles=receipt_reconciles,
            decision_contract_coverage=float(coverage),
            explain_content_leak=any(marker in explanation for marker in private_markers),
            source_ref_present=source_ref_present,
            tool_dependency_preserved=_tool_dependency_preserved(
                rendered,
                list(request["history"]),
            ),
            channel_evidence=channels,
            cold_reopen=bool(suite["sqlite"]["cold_reopen_after_ingest"]),
        )

    return await _with_reopened_store(case, suite, embeddings, evaluate)


def _load_frozen_reference(
    suite_name: str,
    suite: Mapping[str, Any],
    fixture_root: Path | None,
) -> dict[str, Any]:
    reference = _read_json(
        _fixture_directory(suite_name, fixture_root)
        / "references"
        / "protoprompt-0.6.1.json"
    )
    if reference.get("schema_version") != 1:
        raise BenchmarkFixtureError("unsupported frozen-reference schema version")
    if reference.get("baseline_id") != "protoprompt_0_6_1":
        raise BenchmarkFixtureError("unexpected frozen baseline id")
    reference_suite = reference.get("suite")
    if not isinstance(reference_suite, Mapping):
        raise BenchmarkFixtureError("frozen reference must declare its suite")
    if (
        reference_suite.get("id") != suite.get("suite_id")
        or reference_suite.get("canonical_sha256") != fixture_sha256(suite)
    ):
        raise BenchmarkFixtureError("frozen reference is bound to another suite")
    source = reference.get("source")
    if not isinstance(source, Mapping) or (
        source.get("package"),
        source.get("version"),
        source.get("git_tag"),
        source.get("git_commit"),
    ) != (
        "protoprompt",
        LEGACY_REFERENCE_VERSION,
        f"v{LEGACY_REFERENCE_VERSION}",
        LEGACY_REFERENCE_COMMIT,
    ):
        raise BenchmarkFixtureError("frozen reference source does not match v0.6.1")
    capabilities = reference.get("capabilities")
    expected_capabilities = {
        "sqlite_cold_reopen": True,
        "scoped_memory_service": True,
        "legacy_full_request_budget": True,
        "context_plan": False,
        "context_request_receipt": False,
        "context_decisions": False,
        "context_plan_content_free_explain": False,
    }
    if capabilities != expected_capabilities:
        raise BenchmarkFixtureError("frozen reference capabilities have changed")
    case_results = reference.get("case_results")
    if not isinstance(case_results, Mapping):
        raise BenchmarkFixtureError("frozen reference must contain case_results")
    expected_case_ids = {str(case["id"]) for case in suite["cases"]}
    if set(case_results) != expected_case_ids:
        raise BenchmarkFixtureError("frozen reference case ids do not match the suite")
    expected_result_keys = set(_not_supported())
    for case_id, result in case_results.items():
        if not isinstance(result, Mapping) or set(result) != expected_result_keys:
            raise BenchmarkFixtureError(
                f"frozen reference result has an invalid shape for {case_id}"
            )
        if result.get("status") not in {"ok", "not_supported"}:
            raise BenchmarkFixtureError(
                f"frozen reference result has an invalid status for {case_id}"
            )
    return reference


def _reference_result(reference: Mapping[str, Any], case_id: str) -> dict[str, Any]:
    result = reference["case_results"].get(case_id)
    if not isinstance(result, Mapping):
        raise BenchmarkFixtureError(f"frozen reference has no result for {case_id}")
    return dict(result)


def _all_fixture_tokens(suite: Mapping[str, Any]) -> set[str]:
    strings: list[str] = []
    for case in suite["cases"]:
        strings.append(str(case["query"]))
        request = case["plan_request"]
        strings.append(str(request["system_prompt"]))
        for record in case["records"]:
            strings.append(str(record["text"]))
        for record in case.get("foreign_records", []):
            strings.append(str(record["text"]))
        session = case.get("session")
        if isinstance(session, Mapping):
            strings.append(str(session["text"]))
        for item in [*request["history"], *request["final_messages"]]:
            if isinstance(item, Mapping):
                strings.append(canonical_json(item))
    return {token.casefold() for text in strings for token in _TOKEN_RE.findall(text)}


def validate_embedding_guard(
    suite: Mapping[str, Any],
    embeddings: SeededFeatureHashEmbeddings,
) -> None:
    """Fail rather than silently benchmark accidental feature-hash collisions."""
    guard = suite["collision_guard"]
    buckets: dict[int, str] = {}
    for token in sorted(_all_fixture_tokens(suite)):
        bucket, _ = embeddings.bucket_for_token(token)
        existing = buckets.setdefault(bucket, token)
        if existing != token:
            raise BenchmarkFixtureError(
                "fixture vocabulary has a feature-hash bucket collision; "
                "publish a new fixture version with an updated seed"
            )
    for case in suite["cases"]:
        query = embeddings.embed_one(str(case["query"]))
        records = _primary_records(case)
        similarities = {
            str(record["memory_id"]): sum(
                left * right
                for left, right in zip(query, embeddings.embed_one(str(record["text"])))
            )
            for record in records
        }
        target = similarities[str(case["target_memory_id"])]
        other = max(
            (score for memory_id, score in similarities.items() if memory_id != case["target_memory_id"]),
            default=-1.0,
        )
        if target < float(guard["min_target_cosine"]):
            raise BenchmarkFixtureError("fixture target similarity is below its guard")
        if other > float(guard["max_non_target_cosine"]):
            raise BenchmarkFixtureError("fixture distractor similarity exceeds its guard")
        if target - other < float(guard["min_target_margin"]):
            raise BenchmarkFixtureError("fixture target margin is below its guard")


async def run_suite(
    suite: Mapping[str, Any],
    *,
    fixture_root: Path | None = None,
) -> dict[str, Any]:
    """Run an already-loaded suite and return only deterministic semantics."""
    _validate_suite(suite)
    if suite.get("suite_kind") == "ledger_context_composition":
        return await run_ledger_composition_suite(
            suite,
            fixture_sha256=fixture_sha256(suite),
        )
    if suite.get("suite_kind") == "ledger_sealed_checkpoint":
        return await run_ledger_checkpoint_suite(
            suite,
            fixture_sha256=fixture_sha256(suite),
        )
    embedding_config = suite["embedding"]
    embeddings = SeededFeatureHashEmbeddings(
        seed=str(embedding_config["seed"]),
        dimensions=int(embedding_config["dimensions"]),
    )
    validate_embedding_guard(suite, embeddings)
    suite_name = str(suite["suite_version"])
    reference = _load_frozen_reference(suite_name, suite, fixture_root)
    cases: list[dict[str, Any]] = []
    for case in suite["cases"]:
        case_id = str(case["id"])
        results = {
            "tail_window_v1": _tail_window_result(case, suite),
            "rolling_summary_v1": _rolling_summary_result(case, suite),
            "vector_recall_v1": await _vector_recall_result(case, suite, embeddings),
            "protoprompt_0_6_1": _reference_result(reference, case_id),
            "protoprompt_context_plan_v0_7": await _context_plan_result(
                case, suite, embeddings
            ),
        }
        cases.append({"id": case_id, "results": results})
    return {
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "suite_version": suite_name,
        "fixture_sha256": fixture_sha256(suite),
        "frozen_reference_sha256": canonical_sha256(reference),
        "embedding_algorithm": str(embedding_config["algorithm"]),
        "implementations": [
            {"id": item_id, "role": role, "execution": execution}
            for item_id, role, execution in IMPLEMENTATIONS
        ],
        "cases": cases,
    }


async def run_suite_by_name(
    suite_name: str = DEFAULT_SUITE,
    *,
    fixture_root: Path | None = None,
) -> dict[str, Any]:
    return await run_suite(load_suite(suite_name, fixture_root=fixture_root), fixture_root=fixture_root)


def verify_report(report: Mapping[str, Any], expected: Mapping[str, Any]) -> None:
    """Verify a report against its frozen, content-free semantic baseline."""
    fixture_hash = expected.get("fixture_sha256")
    if fixture_hash != report.get("fixture_sha256"):
        raise BenchmarkVerificationError("fixture SHA-256 does not match expected baseline")
    expected_report = expected.get("report")
    if canonical_json(expected_report) != canonical_json(report):
        raise BenchmarkVerificationError("semantic report differs from frozen expected output")


def assert_candidate_not_worse_than_reference(report: Mapping[str, Any]) -> None:
    """Enforce the v0.1 comparison rule without interpreting unsupported APIs.

    The frozen 0.6.1 column is a compatibility floor for recall/scope behavior.
    New ContextPlan-only fields are deliberately excluded because their legacy
    counterpart did not exist yet.
    """
    for case in report["cases"]:
        results = case["results"]
        reference = results["protoprompt_0_6_1"]
        candidate = results["protoprompt_context_plan_v0_7"]
        if reference["status"] != "ok":
            continue
        if candidate["status"] != "ok":
            raise BenchmarkVerificationError(
                f"candidate is not available for {case['id']}"
            )
        if reference["evidence_available"] is True and candidate["evidence_available"] is not True:
            raise BenchmarkVerificationError(
                f"candidate lost required evidence for {case['id']}"
            )
        reference_rank = reference["target_rank"]
        candidate_rank = candidate["target_rank"]
        if (
            isinstance(reference_rank, int)
            and (not isinstance(candidate_rank, int) or candidate_rank > reference_rank)
        ):
            raise BenchmarkVerificationError(
                f"candidate retrieval rank regressed for {case['id']}"
            )
        if candidate["budget_violation_count"] != 0:
            raise BenchmarkVerificationError(
                f"candidate violates the request budget for {case['id']}"
            )
        if (
            reference["scope_leak_count"] == 0
            and candidate["scope_leak_count"] != 0
        ):
            raise BenchmarkVerificationError(
                f"candidate leaks scope data for {case['id']}"
            )
        if candidate["receipt_reconciles"] is not True:
            raise BenchmarkVerificationError(
                f"candidate has no reconciling request receipt for {case['id']}"
            )
        if candidate["explain_content_leak"] is not False:
            raise BenchmarkVerificationError(
                f"candidate explanation leaks content for {case['id']}"
            )


def render_markdown(report: Mapping[str, Any]) -> str:
    """Render a small deterministic human view; it contains no timing claims."""
    if report.get("benchmark_kind") == "ledger_context_composition":
        return render_ledger_composition_markdown(report)
    if report.get("benchmark_kind") == "ledger_sealed_checkpoint":
        return render_ledger_checkpoint_markdown(report)
    lines = [
        "# ProtoPrompt Memory Benchmark",
        "",
        f"Suite: `{report['suite_version']}`  ",
        f"Fixture SHA-256: `{report['fixture_sha256']}`",
        "",
        "| Case | Implementation | Status | Evidence | Rank | Scope leaks | Budget violations | Receipt | Explain leak |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for case in report["cases"]:
        for implementation, result in case["results"].items():
            def cell(value: object) -> str:
                if value is None:
                    return "—"
                if isinstance(value, bool):
                    return "yes" if value else "no"
                return str(value)

            lines.append(
                "| {case} | {implementation} | {status} | {evidence} | {rank} | "
                "{leaks} | {budget} | {receipt} | {explain} |".format(
                    case=case["id"],
                    implementation=implementation,
                    status=result["status"],
                    evidence=cell(result["evidence_available"]),
                    rank=cell(result["target_rank"]),
                    leaks=cell(result["scope_leak_count"]),
                    budget=cell(result["budget_violation_count"]),
                    receipt=cell(result["receipt_reconciles"]),
                    explain=cell(result["explain_content_leak"]),
                )
            )
    lines.extend([
        "",
        "`protoprompt_0_6_1` is a frozen semantic reference, not code executed "
        "through the current runtime. `not_supported` denotes an API that did "
        "not exist in that baseline; it is not a failed result.",
        "",
        "This suite is offline and deterministic. It deliberately excludes model "
        "quality, network latency, hardware throughput, and third-party rankings.",
        "",
    ])
    return "\n".join(lines)


def _write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8", newline="\n")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", default=DEFAULT_SUITE, help="versioned suite, e.g. v0.1")
    parser.add_argument("--verify", action="store_true", help="compare against frozen expected.json")
    parser.add_argument("--expected", type=Path, help="override expected.json (test/debug use)")
    parser.add_argument("--json", type=Path, help="write canonical JSON report to this path")
    parser.add_argument("--markdown", type=Path, help="write Markdown report to this path")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        suite = load_suite(args.suite)
        report = asyncio.run(run_suite(suite))
        if args.verify:
            verify_report(
                report,
                load_expected(args.suite, path=args.expected),
            )
            if suite.get("suite_kind") not in {
                "ledger_context_composition",
                "ledger_sealed_checkpoint",
            }:
                assert_candidate_not_worse_than_reference(report)
        if args.json:
            _write_text(args.json, canonical_json(report) + "\n")
        if args.markdown:
            _write_text(args.markdown, render_markdown(report))
    except (BenchmarkFixtureError, BenchmarkVerificationError, ValueError) as exc:
        print(f"memory benchmark failed: {exc}", file=sys.stderr)
        return 1
    if args.verify:
        print(f"memory benchmark {args.suite} verified")
    elif not args.json and not args.markdown:
        print(canonical_json(report))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised via script wrapper
    raise SystemExit(main())
