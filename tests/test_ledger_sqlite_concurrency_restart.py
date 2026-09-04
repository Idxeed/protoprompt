"""Bounded multi-process/restart conformance for the SQLite Ledger.

This is deliberately a small correctness matrix rather than a throughput
benchmark.  Independent processes start their write commands from one local
file barrier, so SQLite must serialize the short ``BEGIN IMMEDIATE`` units
without leaving a partial candidate, a cross-scope event result, or a durable
writer lock after the processes have exited.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from protoprompt.ledger import MemoryKind, MemoryState, MemoryWriter, SqliteMemoryLedger
from protoprompt.scope import MemoryScope


ROOT = Path(__file__).resolve().parents[1]
_T0 = datetime(2040, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
_READY_TIMEOUT_SECONDS = 15
_COMMAND_TIMEOUT_SECONDS = 20


# Use a process boundary rather than threads: a single ``SqliteMemoryLedger``
# instance has a Python lock, while deployed hosts can open distinct processes
# against one database file.  The payload is intentionally fixed and passed as
# JSON rather than interpolated into the program text.
_CONCURRENT_PROPOSE_WORKER = r'''
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

from protoprompt.ledger import MemoryKind, MemoryWriter, SqliteMemoryLedger
from protoprompt.scope import MemoryScope


database_path, request_json, ready_path, start_path = sys.argv[1:]
request = json.loads(request_json)
scope = MemoryScope(**request["scope"])
ledger = SqliteMemoryLedger(database_path)
writer = MemoryWriter(
    ledger,
    scope=scope,
    actor="concurrency-host",
    clock=lambda: datetime(2040, 2, 3, 4, 5, 6, tzinfo=timezone.utc),
)

try:
    Path(ready_path).touch()
    deadline = time.monotonic() + 15
    while not Path(start_path).exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("parent did not release the local concurrency barrier")
        time.sleep(0.01)
    record = writer.propose(
        kind=MemoryKind.FACT,
        content=request["content"],
        source_ref=request["source_ref"],
        evidence_refs=(request["evidence_ref"],),
        confidence=0.9,
        record_id=request["record_id"],
        event_id=request["event_id"],
    )
    print(json.dumps({
        "record_id": record.record_id,
        "revision": record.revision,
        "state": record.state.value,
    }, sort_keys=True))
finally:
    ledger.close()
'''


def _scope(*, thread: str) -> MemoryScope:
    return MemoryScope(tenant="concurrency", user="operator", thread=thread)


def _writer(ledger: SqliteMemoryLedger, *, thread: str) -> MemoryWriter:
    return MemoryWriter(
        ledger,
        scope=_scope(thread=thread),
        actor="concurrency-host",
        clock=lambda: _T0,
    )


def _request(
    *,
    thread: str,
    record_id: str,
    event_id: str,
    content: str,
    source_ref: str,
) -> dict[str, object]:
    return {
        "scope": {
            "tenant": "concurrency",
            "user": "operator",
            "thread": thread,
            "kind": "",
        },
        "record_id": record_id,
        "event_id": event_id,
        "content": content,
        "source_ref": source_ref,
        "evidence_ref": source_ref + "-evidence",
    }


def _run_concurrent_proposals(path: Path, requests: list[dict[str, object]], tmp_path: Path) -> list[dict[str, object]]:
    """Run a bounded local write wave and return each worker's public result."""

    start_path = tmp_path / "release-workers"
    processes: list[subprocess.Popen[str]] = []
    ready_paths: list[Path] = []
    environment = os.environ.copy()
    original_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(ROOT)
        if not original_pythonpath
        else str(ROOT) + os.pathsep + original_pythonpath
    )
    try:
        for index, request in enumerate(requests):
            ready_path = tmp_path / f"worker-{index}.ready"
            ready_paths.append(ready_path)
            processes.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        _CONCURRENT_PROPOSE_WORKER,
                        str(path),
                        json.dumps(request, sort_keys=True),
                        str(ready_path),
                        str(start_path),
                    ],
                    cwd=ROOT,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )

        deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
        while not all(ready_path.exists() for ready_path in ready_paths):
            if time.monotonic() >= deadline:
                raise AssertionError("not all local SQLite workers reached the start barrier")
            time.sleep(0.01)
        start_path.touch()

        results: list[dict[str, object]] = []
        for index, process in enumerate(processes):
            stdout, stderr = process.communicate(timeout=_COMMAND_TIMEOUT_SECONDS)
            assert process.returncode == 0, (
                f"SQLite concurrency worker {index} failed\n"
                f"stdout:\n{stdout}\nstderr:\n{stderr}"
            )
            results.append(json.loads(stdout))
        return results
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)


def _table_counts(ledger: SqliteMemoryLedger) -> dict[str, int]:
    return {
        table: int(ledger._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in (
            "memory_records",
            "memory_events",
            "memory_payloads",
            "memory_sources",
            "memory_record_admission_metadata",
        )
    }


def test_sqlite_concurrent_event_retries_are_atomic_scope_bound_and_restart_clean(tmp_path):
    """Prove one bounded multiwriter wave preserves ledger command invariants.

    The first two workers are the same retry in one scope; a third is an
    independent record in that scope, and a fourth deliberately reuses the
    record/event identifiers in a sibling scope.  The expected durable result
    is therefore exactly three candidates and three observed events, followed
    by a successful reopen and retry.
    """

    path = tmp_path / "ledger-concurrency.db"
    initialized = SqliteMemoryLedger(str(path))
    initialized.setup()
    initialized.close()

    duplicate = _request(
        thread="primary",
        record_id="retry-record",
        event_id="shared-propose-event",
        content="exactly one candidate survives an idempotent retry",
        source_ref="source:retry",
    )
    parallel = _request(
        thread="primary",
        record_id="parallel-record",
        event_id="parallel-propose-event",
        content="a second writer may create an independent candidate",
        source_ref="source:parallel",
    )
    sibling = _request(
        thread="sibling",
        record_id="retry-record",
        event_id="shared-propose-event",
        content="same opaque IDs remain isolated in a sibling scope",
        source_ref="source:sibling",
    )

    results = _run_concurrent_proposals(
        path,
        [duplicate, duplicate, parallel, sibling],
        tmp_path,
    )
    assert results.count({"record_id": "retry-record", "revision": 1, "state": "candidate"}) == 3
    assert results.count({"record_id": "parallel-record", "revision": 1, "state": "candidate"}) == 1

    # A fresh connection must be able to acquire the schema write boundary;
    # that both proves no worker left a lock behind and checks setup/reopen
    # consistency before any inspection uses the ledger.
    ledger = SqliteMemoryLedger(str(path))
    try:
        ledger.setup()
        primary = _writer(ledger, thread="primary")
        sibling_writer = _writer(ledger, thread="sibling")

        retried = primary.get("retry-record")
        assert retried is not None
        assert retried.state is MemoryState.CANDIDATE
        assert retried.revision == 1
        assert retried.content == duplicate["content"]
        assert [event.event_id for event in primary.events("retry-record")] == [
            "shared-propose-event"
        ]

        independent = primary.get("parallel-record")
        assert independent is not None
        assert independent.state is MemoryState.CANDIDATE
        assert independent.content == parallel["content"]
        assert [event.event_id for event in primary.events("parallel-record")] == [
            "parallel-propose-event"
        ]

        scoped_sibling = sibling_writer.get("retry-record")
        assert scoped_sibling is not None
        assert scoped_sibling.content == sibling["content"]
        assert [event.event_id for event in sibling_writer.events("retry-record")] == [
            "shared-propose-event"
        ]
        assert primary.get("sibling-only") is None
        assert sibling_writer.get("parallel-record") is None

        # All candidate sidecars must be present exactly once.  This would
        # catch an event/record/payload/source partial commit just as the
        # process-death matrix does, but under independent multiwriter use.
        assert _table_counts(ledger) == {
            "memory_records": 3,
            "memory_events": 3,
            "memory_payloads": 3,
            "memory_sources": 3,
            "memory_record_admission_metadata": 3,
        }

        # Replaying the command through the reopened process cannot create a
        # second event or change the revision.  A new write immediately after
        # it makes a lingering SQLite writer lock visible to this bounded test.
        retry_after_reopen = primary.propose(
            kind=MemoryKind.FACT,
            content=str(duplicate["content"]),
            source_ref=str(duplicate["source_ref"]),
            evidence_refs=(str(duplicate["evidence_ref"]),),
            confidence=0.9,
            record_id="retry-record",
            event_id="shared-propose-event",
        )
        assert retry_after_reopen.record_id == "retry-record"
        assert retry_after_reopen.revision == 1
        post_reopen = primary.propose(
            kind=MemoryKind.FACT,
            content="post-reopen write proves the writer lock was released",
            source_ref="source:post-reopen",
            evidence_refs=("evidence:post-reopen",),
            confidence=0.9,
            record_id="post-reopen-record",
            event_id="post-reopen-event",
        )
        assert post_reopen.revision == 1
        assert _table_counts(ledger)["memory_records"] == 4
        assert [event.event_id for event in primary.events("retry-record")] == [
            "shared-propose-event"
        ]
    finally:
        ledger.close()
