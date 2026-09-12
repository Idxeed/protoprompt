"""Live PostgreSQL crash/reconnect/retry and bounded multiwriter evidence.

These tests use independent client processes and a disposable real PostgreSQL
schema.  The recovery cases terminate the worker's server backend after a
selected DML statement has executed but before commit, then prove that a fresh
connection sees the exact pre-command state and can retry the whole host
command.  This is logical transaction evidence, not a claim about managed
backup, PITR, replicas, or physical media erasure.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any
import uuid

import pytest

from protoprompt.ledger import MemoryKind, MemoryState, MemoryWriter
from protoprompt.ledger.postgres import PostgresMemoryLedger
from protoprompt.scope import MemoryScope


pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
_T0 = datetime(2041, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
_READY_TIMEOUT_SECONDS = 20
_COMMAND_TIMEOUT_SECONDS = 30
_CONNECTION_ABORT_EXIT = 91
_POSTCOMMIT_EXIT = 92


_FAULT_WORKER = r'''
from datetime import datetime, timezone
import os
from pathlib import Path
import sys
import time

from protoprompt.ledger import MemoryKind, MemoryWriter
from protoprompt.ledger.postgres import PostgresMemoryLedger
from protoprompt.scope import MemoryScope


T0 = datetime(2041, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
dsn = os.environ["PROTOPROMPT_POSTGRES_DSN"]
schema, case, ready_path, release_path = sys.argv[1:]
scope = MemoryScope(tenant="recovery", user="operator", thread="primary")
ledger = PostgresMemoryLedger(dsn, schema=schema)
ledger.setup()
writer = MemoryWriter(ledger, scope=scope, actor="postgres-recovery-host", clock=lambda: T0)


def active(record_id, source_ref):
    candidate = writer.propose(
        kind=MemoryKind.FACT,
        content="PostgreSQL recovery payload for " + record_id,
        source_ref=source_ref,
        evidence_refs=("evidence:" + record_id,),
        confidence=0.9,
        record_id=record_id,
        event_id="observe-" + record_id,
    )
    return writer.confirm(
        candidate.record_id,
        expected_revision=candidate.revision,
        event_id="confirm-" + record_id,
    )


active("purge-first", "source:purge:first")
active("purge-second", "source:purge:second")

if case == "postcommit":
    writer.purge_payloads("postgres-recovery-purge")
    os._exit(92)

marker = {
    "payload_delete": "DELETE FROM MEMORY_PAYLOADS",
    "receipt_insert": "INSERT INTO MEMORY_SCOPE_PAYLOAD_PURGE_RECEIPTS",
}[case]
original_execute = ledger._engine._conn.execute
triggered = False


def execute_and_pause(source, parameters=None):
    global triggered
    result = original_execute(source, parameters)
    normalized = " ".join(source.upper().split())
    if not triggered and marker in normalized:
        triggered = True
        Path(ready_path).write_text(
            str(ledger._engine._raw.info.backend_pid), encoding="ascii"
        )
        deadline = time.monotonic() + 20
        while not Path(release_path).exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("parent did not release aborted PostgreSQL worker")
            time.sleep(0.01)
    return result


ledger._engine._conn.execute = execute_and_pause
try:
    writer.purge_payloads("postgres-recovery-purge")
except BaseException:
    os._exit(91)
raise SystemExit("fault marker did not abort the PostgreSQL command")
'''


_PROPOSE_WORKER = r'''
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

from protoprompt.ledger import MemoryKind, MemoryWriter
from protoprompt.ledger.postgres import PostgresMemoryLedger
from protoprompt.scope import MemoryScope


T0 = datetime(2041, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
dsn = os.environ["PROTOPROMPT_POSTGRES_DSN"]
schema, request_json, ready_path, start_path = sys.argv[1:]
request = json.loads(request_json)
ledger = PostgresMemoryLedger(dsn, schema=schema)
writer = MemoryWriter(
    ledger,
    scope=MemoryScope(**request["scope"]),
    actor="postgres-concurrency-host",
    clock=lambda: T0,
)
try:
    Path(ready_path).touch()
    deadline = time.monotonic() + 20
    while not Path(start_path).exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("parent did not release PostgreSQL proposal workers")
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
        "thread": request["scope"]["thread"],
        "record_id": record.record_id,
        "revision": record.revision,
        "state": record.state.value,
    }, sort_keys=True))
finally:
    ledger.close()
'''


_PURGE_WORKER = r'''
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time

from protoprompt.ledger import MemoryWriter
from protoprompt.ledger.postgres import PostgresMemoryLedger
from protoprompt.scope import MemoryScope


T0 = datetime(2041, 3, 4, 5, 6, 7, tzinfo=timezone.utc)
dsn = os.environ["PROTOPROMPT_POSTGRES_DSN"]
schema, thread, ready_path, start_path = sys.argv[1:]
ledger = PostgresMemoryLedger(dsn, schema=schema)
writer = MemoryWriter(
    ledger,
    scope=MemoryScope(tenant="concurrency", user="operator", thread=thread),
    actor="postgres-concurrency-host",
    clock=lambda: T0,
)
try:
    Path(ready_path).touch()
    deadline = time.monotonic() + 20
    while not Path(start_path).exists():
        if time.monotonic() >= deadline:
            raise RuntimeError("parent did not release PostgreSQL purge workers")
        time.sleep(0.01)
    receipt = writer.purge_payloads("shared-purge-operation")
    print(json.dumps({
        "thread": thread,
        "records_forgotten": receipt.records_forgotten,
        "payload_rows_deleted": receipt.payload_rows_deleted,
        "scope_fingerprint": receipt.scope_fingerprint,
    }, sort_keys=True))
finally:
    ledger.close()
'''


@pytest.fixture
def dsn() -> str:
    value = os.environ.get("PROTOPROMPT_POSTGRES_DSN")
    if not value:
        pytest.skip("set PROTOPROMPT_POSTGRES_DSN to run PostgreSQL recovery tests")
    return value


@pytest.fixture
def schema(dsn: str) -> str:
    value = "pp_ledger_recovery_" + uuid.uuid4().hex
    try:
        yield value
    finally:
        _drop_schema(dsn, value)


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _raw_connection(dsn: str) -> Any:
    import psycopg
    from psycopg.rows import dict_row

    return psycopg.connect(dsn, autocommit=True, row_factory=dict_row)


def _drop_schema(dsn: str, schema: str) -> None:
    with _raw_connection(dsn) as connection:
        connection.execute(f"DROP SCHEMA IF EXISTS {_quoted_identifier(schema)} CASCADE")


def _scope(*, thread: str = "primary", tenant: str = "recovery") -> MemoryScope:
    return MemoryScope(tenant=tenant, user="operator", thread=thread)


def _writer(
    ledger: PostgresMemoryLedger,
    *,
    thread: str = "primary",
    tenant: str = "recovery",
    actor: str = "postgres-recovery-host",
) -> MemoryWriter:
    return MemoryWriter(
        ledger,
        scope=_scope(thread=thread, tenant=tenant),
        actor=actor,
        clock=lambda: _T0,
    )


def _active(writer: MemoryWriter, *, record_id: str, source_ref: str) -> Any:
    candidate = writer.propose(
        kind=MemoryKind.FACT,
        content="PostgreSQL concurrency payload for " + record_id,
        source_ref=source_ref,
        evidence_refs=("evidence:" + record_id,),
        confidence=0.9,
        record_id=record_id,
        event_id="observe-" + record_id,
    )
    return writer.confirm(
        candidate.record_id,
        expected_revision=candidate.revision,
        event_id="confirm-" + record_id,
    )


def _process_environment(dsn: str) -> dict[str, str]:
    environment = os.environ.copy()
    original_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        str(ROOT)
        if not original_pythonpath
        else str(ROOT) + os.pathsep + original_pythonpath
    )
    environment["PROTOPROMPT_POSTGRES_DSN"] = dsn
    return environment


def _wait_for_paths(paths: list[Path], *, message: str) -> None:
    deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
    while not all(path.exists() for path in paths):
        if time.monotonic() >= deadline:
            raise AssertionError(message)
        time.sleep(0.01)


def _wait_for_backend_pid(path: Path, *, message: str) -> int:
    """Wait until the worker has durably populated its connection marker."""

    deadline = time.monotonic() + _READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if path.exists():
            value = path.read_text(encoding="ascii").strip()
            if value.isascii() and value.isdecimal():
                return int(value)
        time.sleep(0.01)
    raise AssertionError(message)


def _table_count(dsn: str, schema: str, table: str) -> int:
    qualified = f"{_quoted_identifier(schema)}.{_quoted_identifier(table)}"
    with _raw_connection(dsn) as connection:
        row = connection.execute(f"SELECT COUNT(*) AS count FROM {qualified}").fetchone()
    assert row is not None
    return int(row["count"])


def _run_precommit_connection_abort(
    dsn: str,
    schema: str,
    case: str,
    tmp_path: Path,
) -> None:
    ready_path = tmp_path / f"{case}.ready"
    release_path = tmp_path / f"{case}.release"
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _FAULT_WORKER,
            schema,
            case,
            str(ready_path),
            str(release_path),
        ],
        cwd=ROOT,
        env=_process_environment(dsn),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        backend_pid = _wait_for_backend_pid(
            ready_path,
            message=f"PostgreSQL fault worker did not reach {case!r} DML marker",
        )
        with _raw_connection(dsn) as connection:
            row = connection.execute(
                "SELECT pg_catalog.pg_terminate_backend(%s) AS terminated",
                (backend_pid,),
            ).fetchone()
        assert row is not None and bool(row["terminated"])
        release_path.touch()
        stdout, stderr = process.communicate(timeout=_COMMAND_TIMEOUT_SECONDS)
        assert process.returncode == _CONNECTION_ABORT_EXIT, (
            f"PostgreSQL fault worker for {case!r} did not observe its aborted connection\n"
            f"stdout:\n{stdout}\nstderr:\n{stderr}"
        )
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)


def _run_postcommit_process_loss(dsn: str, schema: str) -> None:
    completed = subprocess.run(
        [sys.executable, "-c", _FAULT_WORKER, schema, "postcommit", "-", "-"],
        cwd=ROOT,
        env=_process_environment(dsn),
        capture_output=True,
        text=True,
        timeout=_COMMAND_TIMEOUT_SECONDS,
        check=False,
    )
    assert completed.returncode == _POSTCOMMIT_EXIT, (
        "PostgreSQL post-commit worker did not terminate after its durable commit\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )


def _run_worker_wave(
    *,
    dsn: str,
    schema: str,
    script: str,
    arguments: list[list[str]],
    tmp_path: Path,
) -> list[dict[str, object]]:
    start_path = tmp_path / ("start-" + uuid.uuid4().hex)
    processes: list[subprocess.Popen[str]] = []
    ready_paths: list[Path] = []
    try:
        for index, worker_arguments in enumerate(arguments):
            ready_path = tmp_path / f"worker-{index}-{uuid.uuid4().hex}.ready"
            ready_paths.append(ready_path)
            processes.append(
                subprocess.Popen(
                    [
                        sys.executable,
                        "-c",
                        script,
                        schema,
                        *worker_arguments,
                        str(ready_path),
                        str(start_path),
                    ],
                    cwd=ROOT,
                    env=_process_environment(dsn),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        _wait_for_paths(
            ready_paths,
            message="not all PostgreSQL workers reached the bounded start barrier",
        )
        start_path.touch()

        results: list[dict[str, object]] = []
        for index, process in enumerate(processes):
            stdout, stderr = process.communicate(timeout=_COMMAND_TIMEOUT_SECONDS)
            assert process.returncode == 0, (
                f"PostgreSQL concurrency worker {index} failed\n"
                f"stdout:\n{stdout}\nstderr:\n{stderr}"
            )
            results.append(json.loads(stdout))
        return results
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)


@pytest.mark.parametrize("case", ["payload_delete", "receipt_insert"])
def test_postgres_scope_purge_connection_abort_rolls_back_and_retries(
    dsn: str,
    schema: str,
    case: str,
    tmp_path: Path,
) -> None:
    """A server-side connection abort cannot expose a partial purge unit."""

    _run_precommit_connection_abort(dsn, schema, case, tmp_path)

    ledger = PostgresMemoryLedger(dsn, schema=schema)
    try:
        ledger.setup()
        writer = _writer(ledger)
        assert writer.payload_readback().payload_record_count == 2
        for record_id in ("purge-first", "purge-second"):
            record = writer.get(record_id)
            assert record is not None
            assert record.state is MemoryState.ACTIVE
            assert record.revision == 2
            assert record.content_available
            assert [event.event_type.value for event in writer.events(record_id)] == [
                "observed",
                "confirmed",
            ]
        assert _table_count(
            dsn, schema, "memory_scope_payload_purge_receipts"
        ) == 0

        receipt = writer.purge_payloads("postgres-recovery-purge")
        assert receipt.records_forgotten == 2
        assert receipt.payload_rows_deleted == 2
        assert receipt.readback.is_empty
        assert _table_count(
            dsn, schema, "memory_scope_payload_purge_receipts"
        ) == 1
    finally:
        ledger.close()


def test_postgres_scope_purge_postcommit_process_loss_replays_receipt(
    dsn: str,
    schema: str,
) -> None:
    """A lost response after commit replays the receipt without a new purge."""

    _run_postcommit_process_loss(dsn, schema)

    ledger = PostgresMemoryLedger(dsn, schema=schema)
    try:
        writer = _writer(ledger)
        assert writer.payload_readback().is_empty
        assert _table_count(
            dsn, schema, "memory_scope_payload_purge_receipts"
        ) == 1
        before = {
            record_id: len(writer.events(record_id))
            for record_id in ("purge-first", "purge-second")
        }
        receipt = writer.purge_payloads("postgres-recovery-purge")
        assert receipt.records_forgotten == 2
        assert receipt.payload_rows_deleted == 2
        assert receipt.readback.is_empty
        assert {
            record_id: len(writer.events(record_id))
            for record_id in ("purge-first", "purge-second")
        } == before
    finally:
        ledger.close()


def _proposal_request(
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
        "evidence_ref": source_ref + ":evidence",
    }


def test_postgres_multiprocess_proposal_wave_is_atomic_scope_bound_and_reopenable(
    dsn: str,
    schema: str,
    tmp_path: Path,
) -> None:
    """Independent clients preserve idempotency and sibling-scope isolation."""

    initialized = PostgresMemoryLedger(dsn, schema=schema)
    initialized.setup()
    initialized.close()

    duplicate = _proposal_request(
        thread="primary",
        record_id="retry-record",
        event_id="shared-propose-event",
        content="one PostgreSQL candidate survives duplicate retries",
        source_ref="source:retry",
    )
    parallel = _proposal_request(
        thread="primary",
        record_id="parallel-record",
        event_id="parallel-propose-event",
        content="an independent PostgreSQL writer keeps its own record",
        source_ref="source:parallel",
    )
    sibling = _proposal_request(
        thread="sibling",
        record_id="retry-record",
        event_id="shared-propose-event",
        content="equal opaque IDs remain isolated in the sibling scope",
        source_ref="source:sibling",
    )
    requests = [duplicate, duplicate, duplicate, parallel, sibling]
    results = _run_worker_wave(
        dsn=dsn,
        schema=schema,
        script=_PROPOSE_WORKER,
        arguments=[[json.dumps(request, sort_keys=True)] for request in requests],
        tmp_path=tmp_path,
    )
    assert sum(
        item["thread"] == "primary" and item["record_id"] == "retry-record"
        for item in results
    ) == 3
    assert sum(
        item["thread"] == "primary" and item["record_id"] == "parallel-record"
        for item in results
    ) == 1
    assert sum(
        item["thread"] == "sibling" and item["record_id"] == "retry-record"
        for item in results
    ) == 1

    reopened = PostgresMemoryLedger(dsn, schema=schema)
    try:
        reopened.setup()
        primary = _writer(reopened, tenant="concurrency")
        sibling_writer = _writer(reopened, thread="sibling", tenant="concurrency")
        retry_record = primary.get("retry-record")
        parallel_record = primary.get("parallel-record")
        sibling_record = sibling_writer.get("retry-record")
        assert retry_record is not None and retry_record.content == duplicate["content"]
        assert parallel_record is not None and parallel_record.content == parallel["content"]
        assert sibling_record is not None and sibling_record.content == sibling["content"]
        assert [event.event_id for event in primary.events("retry-record")] == [
            "shared-propose-event"
        ]
        assert [event.event_id for event in sibling_writer.events("retry-record")] == [
            "shared-propose-event"
        ]
        assert primary.get("sibling-only") is None
        assert sibling_writer.get("parallel-record") is None
        assert {
            table: _table_count(dsn, schema, table)
            for table in (
                "memory_records",
                "memory_events",
                "memory_payloads",
                "memory_sources",
                "memory_record_admission_metadata",
            )
        } == {
            "memory_records": 3,
            "memory_events": 3,
            "memory_payloads": 3,
            "memory_sources": 3,
            "memory_record_admission_metadata": 3,
        }
        post_reopen = primary.propose(
            kind=MemoryKind.FACT,
            content="a fresh write proves no advisory lock survived client exit",
            source_ref="source:post-reopen",
            evidence_refs=("evidence:post-reopen",),
            confidence=0.9,
            record_id="post-reopen-record",
            event_id="post-reopen-event",
        )
        assert post_reopen.revision == 1
    finally:
        reopened.close()


def test_postgres_multiprocess_scope_purge_wave_is_idempotent_and_scope_bound(
    dsn: str,
    schema: str,
    tmp_path: Path,
) -> None:
    """Concurrent duplicate purges seal one receipt per exact scope."""

    ledger = PostgresMemoryLedger(dsn, schema=schema)
    ledger.setup()
    primary = _writer(
        ledger,
        tenant="concurrency",
        actor="postgres-concurrency-host",
    )
    sibling = _writer(
        ledger,
        thread="sibling",
        tenant="concurrency",
        actor="postgres-concurrency-host",
    )
    _active(primary, record_id="primary-first", source_ref="source:primary:first")
    _active(primary, record_id="primary-second", source_ref="source:primary:second")
    _active(sibling, record_id="sibling-first", source_ref="source:sibling:first")
    ledger.close()

    results = _run_worker_wave(
        dsn=dsn,
        schema=schema,
        script=_PURGE_WORKER,
        arguments=[[thread] for thread in ["primary"] * 4 + ["sibling"] * 2],
        tmp_path=tmp_path,
    )
    primary_results = [item for item in results if item["thread"] == "primary"]
    sibling_results = [item for item in results if item["thread"] == "sibling"]
    assert len(primary_results) == 4
    assert len({json.dumps(item, sort_keys=True) for item in primary_results}) == 1
    assert primary_results[0]["records_forgotten"] == 2
    assert primary_results[0]["payload_rows_deleted"] == 2
    assert len(sibling_results) == 2
    assert len({json.dumps(item, sort_keys=True) for item in sibling_results}) == 1
    assert sibling_results[0]["records_forgotten"] == 1
    assert sibling_results[0]["payload_rows_deleted"] == 1
    assert primary_results[0]["scope_fingerprint"] != sibling_results[0][
        "scope_fingerprint"
    ]

    reopened = PostgresMemoryLedger(dsn, schema=schema)
    try:
        reopened.setup()
        primary = _writer(
            reopened,
            tenant="concurrency",
            actor="postgres-concurrency-host",
        )
        sibling = _writer(
            reopened,
            thread="sibling",
            tenant="concurrency",
            actor="postgres-concurrency-host",
        )
        assert primary.payload_readback().is_empty
        assert sibling.payload_readback().is_empty
        assert _table_count(
            dsn, schema, "memory_scope_payload_purge_receipts"
        ) == 2

        later = primary.propose(
            kind=MemoryKind.FACT,
            content="a later payload must survive replay of the sealed purge receipt",
            source_ref="source:later",
            evidence_refs=("evidence:later",),
            confidence=0.9,
            record_id="later-record",
            event_id="later-event",
        )
        retry = primary.purge_payloads("shared-purge-operation")
        assert retry.records_forgotten == 2
        assert retry.payload_rows_deleted == 2
        assert primary.get(later.record_id) is not None
        assert primary.payload_readback().payload_record_count == 1
    finally:
        reopened.close()
