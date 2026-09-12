# ProtoPrompt 0.19.0 release candidate security review

**Release-gate status:** release-candidate review. This is not an external
penetration test, security certification, managed-database recovery approval,
or a claim that 0.19.0 has been published.

This review covers the v0.19 delta relative to the verified v0.18.0 tag. The
delta intentionally does not change Ledger runtime command semantics. It adds
live PostgreSQL connection-abort and independent-process evidence, makes that
evidence mandatory in CI/publication, updates documentation, and aligns package
versions.

## Result

No confirmed P0 or P1 issue was found under the stated disposable-database
test model. The new evidence closes a local transaction/reconnect gap:

- The test worker opens a real psycopg connection and pauses only after a
  selected DML statement has reached PostgreSQL inside the enclosing Ledger
  transaction.
- An independent control connection calls `pg_terminate_backend()` for that
  exact worker PID. A new `PostgresMemoryLedger` connection must see either the
  complete prior state or the complete committed purge, never an intermediate
  event/payload/receipt combination.
- Pre-commit aborts cover payload deletion and final aggregate-receipt
  insertion. The same immutable operation ID, scope, actor, and reason then
  retry successfully through a fresh connection.
- A post-commit process exit covers the ambiguous-response boundary: retry
  returns the original durable receipt and does not delete a later record.
- Independent-process waves cover duplicate proposal and purge commands,
  independent records, equal opaque IDs in sibling scopes, exact receipt
  identity, reconnect, and advisory-lock release.

## Test-only authority boundary

The server-termination capability exists only in
`tests/integration/test_postgres_recovery_concurrency.py` and is never imported
by the package runtime. It requires an operator-supplied
`PROTOPROMPT_POSTGRES_DSN`, runs under the `integration` pytest marker, allocates
a random dedicated schema, and drops only that schema during cleanup. The
connection PID is written to a temporary local marker and is not persisted in
Ledger rows, receipts, package telemetry, or provider context.

The test database role must be able to terminate its own sessions. Production
applications do not need and should not receive `pg_signal_backend` merely to
use ProtoPrompt.

## Evidence

- Clean local PostgreSQL 17/pgvector run on Python 3.12:
  `23 passed, 1 environment-specific non-deterministic-collation skip` across
  the existing Ledger suite and the new recovery/concurrency matrix.
- The new matrix alone: `5 passed` on Windows against a disposable PostgreSQL
  service.
- GitHub Actions branch run `34725995940` for commit
  `72f148bb33cd04ca0ae8a5e4738ef4e4208eb049` concluded `success` for every
  required Python 3.11/3.12/3.13, Windows CLI, Ollama app, docs, package,
  benchmark, and PostgreSQL/Redis job. This proves the process harness also ran
  successfully on the Ubuntu integration runner.
- Strict RU/EN documentation build and core sdist validation succeeded; the
  sdist contains the recovery/concurrency evidence file.
- The production source delta from v0.18.0 contains version metadata only. The
  v0.18.0 Bandit 1.9.4 triage therefore remains applicable to runtime code: no
  `# nosec` suppressions were added, and no new runtime shell or SQL
  interpolation path was introduced.

## Guarantees and exclusions

This evidence supports a narrow claim: PostgreSQL rolls back the tested Ledger
transaction when the client connection is terminated, and a fresh client can
retry a stable host command without partial state or cross-scope collision.

It does not establish:

- database-server crash recovery or durability under storage corruption;
- managed PostgreSQL backup restore, PITR, failover, replica consistency, WAL
  retention, or physical-media erasure;
- distributed exactly-once delivery, a multi-process application queue, or a
  throughput/latency SLA;
- authorization for arbitrary database users, tenant-facing network service
  posture, or filesystem/secret custody;
- model quality, prompt-injection immunity, perfect recall, unlimited context,
  or infinite memory.

Schema-wide Ledger writes remain deliberately serialized by one
transaction-scoped advisory lock. Hosts must retry the whole trusted command
with its stable idempotency identity after `LedgerConflictError`; they must not
retry arbitrary SQL fragments.

The tag workflow must repeat deterministic core/app suites, the combined live
PostgreSQL matrix, frozen semantic benchmarks, strict documentation,
clean-install smoke checks, and artifact digest reconciliation on the final
source revision before publication. Managed deployment approval still requires
an independently scoped restore/failover exercise and deployment-specific
threat model.
