# ProtoPrompt 0.14 — planned RC-hardening launch kit

This file is the source of truth for the public v0.14 message. Use it only
after the `v0.14.0` tag, PyPI artifact, and green release gate are published.
It makes no claim about model quality, external-framework superiority, latency,
throughput, 10k-record performance, universal prompt-injection immunity, or
an unlimited context window.

## Release evidence

- Release: <https://github.com/Idxeed/protoprompt/releases/tag/v0.14.0>
- PyPI: <https://pypi.org/project/protoprompt/0.14.0/>
- Roadmap: <https://github.com/Idxeed/protoprompt/blob/master/ROADMAP.md>
- Public property conformance:
  `tests/ledger_conformance/property.py`
- SQLite wrappers:
  `tests/test_ledger_property_conformance_sqlite.py` and
  `tests/test_ledger_recall_property_sqlite.py`
- PostgreSQL evidence: eighteen live integration cases in
  `tests/integration/test_postgres_memory_ledger.py`

The tests are deterministic and bounded. SQLite runs 20 generated examples ×
12 lifecycle steps; the live PostgreSQL parity lane uses a smaller profile on
fresh disposable schemas. This is semantic conformance proof, not a load test.

## RU

### Короткий анонс

**ProtoPrompt 0.14 усиливает путь к 1.0: свойства памяти теперь проверяются
на сгенерированных сценариях, а не только на вручную выбранных примерах.**

Новый RC-gate не расширяет public API и не меняет модель продукта. Он проверяет
через тот же host-owned `MemoryWriter` contract: scope isolation, lifecycle
transitions, exact idempotent retry, stale/invalid atomicity, `forget`,
controlled hard erase и scoped source revocation. Отдельная строгая проверка
Ledger recall подтверждает, что документы проходят admission boundary, целиком
помещаются в token/UTF-8 byte budget, а plaintext не попадает в `explain()`.

Эти свойства запускаются для SQLite и для optional PostgreSQL Ledger на fresh
test schemas. Это не бенчмарк latency/throughput и не утверждение о качестве
модели: следующие доказательства до 1.0 потребуют отдельного frozen v1
benchmark и reference hardware protocol.

### Для кого

| Аудитория | Релевантное обещание | Следующий шаг |
|---|---|---|
| Python/agent engineers | Lifecycle и exact recall accounting получают generated regression proof. | Запустить SQLite property gate в своём checkout. |
| PostgreSQL/platform teams | Та же public semantic surface проверяется на живой БД в изолированных schemas. | Запустить PostgreSQL integration suite против disposable dev DB. |
| Security leads | Scope, deletion/source revocation и payload-free receipts проверяются на множестве путей. | Сверить DB permissions, backup/retention и собственную threat model. |
| Framework teams | Нет auto-wiring и нет нового orchestration API. | Подключать Ledger только явно через host-owned boundary. |

### Честные границы

- Hypothesis — только dependency development/release gate; core по-прежнему
  zero-dependency.
- Property profile ограничен и детерминирован; он ловит регрессии контракта,
  но не заменяет независимый security audit или production chaos testing.
- PostgreSQL profile создаёт fresh schemas и проверяет semantic parity, а не
  пропускную способность, latency SLA или migration старых PostgreSQL Ledger.
- `LedgerRecallPlanner` по-прежнему не является agent/workflow checkpoint и
  не отправляет provider request сам.

## EN

### Short announcement

**ProtoPrompt 0.14 strengthens the path to 1.0: memory properties now run
through generated scenarios, not only hand-picked examples.**

The new RC gate does not expand the public API or change the product model. It
checks the same host-owned `MemoryWriter` contract for scope isolation,
lifecycle transitions, exact idempotent retries, stale/invalid atomicity,
`forget`, controlled hard erase, and scoped source revocation. A separate
strict Ledger-recall property confirms that documents cross the admission
boundary, fit whole-record token/UTF-8-byte budgets, and never surface as
plaintext in `explain()`.

These properties run for SQLite and the optional PostgreSQL Ledger on fresh
test schemas. This is not a latency/throughput benchmark or a model-quality
claim: the remaining v1 evidence requires a separate frozen benchmark and
reference-hardware protocol.

### Honest boundaries

- Hypothesis is a development/release-gate dependency only; the core remains
  zero-dependency.
- The property profile is bounded and deterministic. It finds contract
  regressions but does not replace an independent security audit or production
  chaos testing.
- The PostgreSQL profile creates fresh schemas and proves semantic parity, not
  throughput, a latency SLA, or migration of old PostgreSQL Ledger layouts.
- `LedgerRecallPlanner` remains neither an agent/workflow checkpoint nor a
  provider request sender.

## Maintainer launch checklist

Do not publish an external announcement until the exact tag, PyPI artifact,
GitHub Release assets/checksums, bilingual docs, wheel/sdist smoke, and green
SQLite/PostgreSQL property gates are available.

1. Reuse the matching RU or EN message above without turning bounded test
   counts into quality or performance claims.
2. Link the exact release and PyPI URLs and provide the reproducible test
   command; do not request memory payloads, source IDs, evidence IDs, or DSNs
   in public feedback.
3. Treat repeatable adoption reports as integration evidence. Treat traffic,
   install counts, or issue volume only as awareness signals.
