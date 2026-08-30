# ProtoPrompt 0.13 — planned launch kit

This versioned file is the source of truth for the public v0.13 message. Use
it only after the `v0.13.0` tag, PyPI artifact, and live PostgreSQL release
gate are published. It makes no claim about throughput, planning latency,
model quality, universal prompt-injection immunity, automatic migration, or
an unlimited context window.

## Release evidence

- Release: <https://github.com/Idxeed/protoprompt/releases/tag/v0.13.0>
- PyPI: <https://pypi.org/project/protoprompt/0.13.0/>
- English PostgreSQL Ledger guide:
  <https://idxeed.github.io/protoprompt/en/postgres/>
- Russian PostgreSQL Ledger guide:
  <https://idxeed.github.io/protoprompt/ru/postgres/>
- Shared public Ledger conformance: `tests/ledger_conformance/core.py`
- PostgreSQL-specific evidence: fifteen live integration cases in
  `tests/integration/test_postgres_memory_ledger.py`
- Local live-gate command:
  `python -m pytest -q tests/integration/test_postgres_memory_ledger.py`

The proof suite validates host-facing semantics: lifecycle, exact scope,
idempotency, restart, strict admission/recall, checkpoint invalidation,
dedicated-schema setup, contention, and selected tamper boundaries. It is not
a performance benchmark or a model-quality comparison.

## RU

### Короткий анонс

**ProtoPrompt 0.13 переносит экспериментальный Ledger v6 в PostgreSQL, не
меняя sync host contract и не обещая «бесконечную память».**

`PostgresMemoryLedger` использует отдельную PostgreSQL schema и явный
sync-вызов `setup()`; импорт модуля и создание `MemoryWriter` не выполняют
DDL автоматически. Это fresh v6 backend: он не угадывает и не запускает
автоматическую миграцию существующего SQLite Ledger или PostgreSQL schema
ранних версий.

Для lifecycle writes, checkpoint changes и финальной проверки active snapshot
используется один Ledger-wide transaction-scoped advisory lock внутри
выделенной schema. PostgreSQL `lock_timeout` фиксирован на 5 секунд: при
контеншене команда fail-closed возвращает retryable `LedgerConflictError`, а
host сам решает, когда и как повторить команду. Это не throughput target и не
автоматический retry loop.

Ledger остаётся experimental и synchronous на storage edge. Он сохраняет
scope-pinned lifecycle, admission boundary, content-free receipts и sealed
v0.12 checkpoint semantics; HMAC secret, task, plaintext, provider messages и
agent state не становятся частью PostgreSQL manifest.

### Для кого

| Аудитория | Релевантное обещание | Следующий шаг |
|---|---|---|
| Python/agent engineers с PostgreSQL | Существующий `MemoryWriter` contract остаётся sync и explicit. | Поднять отдельную dev schema, вызвать `setup()` и прогнать conformance gate. |
| Platform/SRE teams | DDL изолирован в выделенной schema; backup не маскируется под file copy. | Проверить `pg_dump` или platform backup/retention policy до production use. |
| Security leads | Scope, lifecycle, admission и checkpoint invalidation имеют проверяемые storage boundaries. | Проверить DB permissions, schema ownership и threat model прямого SQL access. |
| Framework teams | Это storage parity, а не новый agent loop или orchestration layer. | Подключать Ledger явно через host-owned integration, без auto-wiring. |

### Adoption CTA

Начните с отдельной непубличной PostgreSQL schema и тестовых данных. Установите
optional extra, задайте `PROTOPROMPT_POSTGRES_DSN`, выполните explicit
`setup()` и запустите live integration suite. Для feedback откройте public
issue/Discussion с Python/PostgreSQL version, schema policy и outcome теста —
не публикуйте memory payload, source/evidence IDs или credentials.

### Честные границы FAQ

- Это experimental backend, не stable 1.x storage promise и не replacement
  для agent workflow, tool replay, leases или exactly-once delivery.
- Поддерживается только fresh PostgreSQL Ledger schema v6. Нет silent
  SQLite→PostgreSQL migration, in-place downgrade или automatic data copy.
- Advisory lock глобален для одного Ledger instance/schema, а не claim о
  масштабируемости базы. Пять секунд — lock timeout; при contention host
  получает retryable conflict, а не гарантию пропускной способности.
- `backup(destination)` намеренно не делает misleading file copy. Используйте
  `pg_dump` или backup policy вашей PostgreSQL platform.
- Direct database-owner access и arbitrary in-process code не становятся
  безопасными из-за этого backend. Они требуют собственной isolation,
  permissions и key-management модели.
- Пятнадцать PostgreSQL-specific integration cases и shared public conformance
  доказывают конкретные contract properties; collation-отрицательный кейс
  выполняется там, где сервер предоставляет такую collation. Они не измеряют
  latency, throughput, retrieval quality или превосходство над фреймворками.

## EN

### Short announcement

**ProtoPrompt 0.13 brings the experimental v6 Ledger to PostgreSQL without
changing the synchronous host contract—or promising “infinite memory.”**

`PostgresMemoryLedger` owns a dedicated PostgreSQL schema and requires an
explicit synchronous `setup()` call; importing the module or constructing a
`MemoryWriter` performs no DDL. It is a fresh-v6 backend: it neither guesses
nor performs an automatic migration of an existing SQLite Ledger or an older
PostgreSQL Ledger schema.

Lifecycle writes, checkpoint changes, and final active-snapshot validation use
one Ledger-wide transaction-scoped advisory lock inside the dedicated schema.
PostgreSQL `lock_timeout` is fixed at five seconds: contention fails closed as
a retryable `LedgerConflictError`, and the host decides whether and when to
retry. This is not a throughput target or an automatic retry loop.

The Ledger remains experimental and synchronous at its storage edge. It keeps
scope-pinned lifecycle, the admission boundary, content-free receipts, and
sealed v0.12 checkpoint semantics; an HMAC secret, task text, plaintext,
provider messages, and agent state do not become PostgreSQL manifest data.

### Audience and CTA

| Audience | Relevant promise | Next step |
|---|---|---|
| Python and agent engineers using PostgreSQL | The existing `MemoryWriter` contract remains synchronous and explicit. | Create a dedicated dev schema, call `setup()`, and run the conformance gate. |
| Platform and SRE teams | DDL is isolated to a dedicated schema; backup is not disguised as a file copy. | Validate `pg_dump` or a platform backup/retention policy before production use. |
| Security leads | Scope, lifecycle, admission, and checkpoint invalidation have testable storage boundaries. | Review DB permissions, schema ownership, and the direct-SQL-access threat model. |
| Framework teams | This is storage parity, not a new agent loop or orchestration layer. | Connect Ledger explicitly through a host-owned integration; do not expect auto-wiring. |

### Adoption CTA

Start with a dedicated non-public PostgreSQL schema and test data. Install the
optional extra, set `PROTOPROMPT_POSTGRES_DSN`, run explicit `setup()`, and run
the live integration suite. File reproducible adoption feedback in a public
issue or Discussion with Python/PostgreSQL version, schema policy, and test
outcome—never memory payloads, source/evidence IDs, or credentials.

### Honest FAQ boundaries

- This is an experimental backend, not a stable 1.x storage promise or a
  replacement for an agent workflow, tool replay, leases, or exactly-once
  delivery.
- Only a fresh PostgreSQL Ledger schema v6 is supported. There is no silent
  SQLite-to-PostgreSQL migration, in-place downgrade, or automatic data copy.
- The advisory lock is Ledger-wide for one instance/schema, not a database
  scalability claim. Five seconds is a lock timeout; on contention the host
  receives a retryable conflict, not a throughput guarantee.
- `backup(destination)` deliberately does not perform a misleading file copy.
  Use `pg_dump` or your PostgreSQL platform's backup policy.
- Direct database-owner access and arbitrary in-process code do not become
  safe because of this backend. They need their own isolation, permissions,
  and key-management model.
- Fifteen PostgreSQL-specific integration cases plus the shared public
  conformance suite demonstrate concrete contract properties; the
  collation-negative case runs where the server provides that fixture. They
  measure no latency, throughput, retrieval quality, or superiority over a
  framework.

## Maintainer launch checklist

Do not publish an external announcement until the v0.13.0 tag, PyPI artifact,
GitHub Release assets/checksums, bilingual docs, and a green live PostgreSQL
integration gate are available.

1. Reuse the matching RU or EN text above without expanding the stated
   boundaries or converting the 5-second lock timeout into a performance SLA.
2. Link the exact release and PyPI URLs above, then direct adopters to the
   PostgreSQL guide and the live integration command.
3. Collect adoption evidence as reproducible configuration/outcome reports;
   do not collect user memory content, source references, evidence IDs, or
   credentials.
4. Treat issue volume, GitHub Traffic, or install counts as awareness signals,
   not quality, latency, throughput, or recall benchmarks.

Success is a narrow, explicit PostgreSQL storage boundary with tested Ledger
semantics—not an unsupported claim that ProtoPrompt replaces an entire agent
framework or offers limitless context.
