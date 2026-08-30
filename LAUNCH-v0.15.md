# ProtoPrompt 0.15 — dual-backend evidence launch kit

This file is the source of truth for the public v0.15 message. Use it only
after the `v0.15.0` tag, PyPI artifact, and green release gate are published.
It makes no claim about model quality, external-framework superiority,
latency, throughput, 10k-record performance, universal prompt-injection
immunity, unlimited memory, or a released package `1.0.0`.

## Release evidence

- Release: <https://github.com/Idxeed/protoprompt/releases/tag/v0.15.0>
- PyPI: <https://pypi.org/project/protoprompt/0.15.0/>
- Roadmap: <https://github.com/Idxeed/protoprompt/blob/master/ROADMAP.md>
- Fixture: `benchmarks/fixtures/v1.0/suite.json`
- Frozen result: `benchmarks/fixtures/v1.0/expected.json`
- Hash binding: `benchmarks/fixtures/v1.0/manifest.json`
- Runner: `benchmarks/ledger_recall_evidence_benchmark.py`

The release gate runs the exact command below against fresh SQLite and a
PostgreSQL 17 service. `--verify` requires both reports; each must match the
same normalized, content-free expected outcome.

```bash
python scripts/run_memory_benchmark.py --suite v1.0 --ledger-backend all --verify
```

The protocol has 18 cases over 100/500/1000-record histories and 1k/2k/4k
budgets. The report's 9/9 strict-Ledger versus 0/9 20-record-tail number is
only named-fixture target availability; it is not model-answer quality or a
general recall ranking.

## RU

### Короткий анонс

**ProtoPrompt 0.15 добавляет проверяемое доказательство семантики strict
Ledger recall сразу для SQLite и PostgreSQL.**

Мы зафиксировали один versioned synthetic fixture, content-free expected
result и SHA-256 manifest. Релизный gate создаёт fresh storage для обоих
backend-ов, запускает одну матрицу из 18 cases и принимает результат только
при exact parity после нормализации ID backend-а.

Проверяются delayed selection, `superseded`/`retracted`/`source_revoked`,
tenant/user/thread isolation, целостный token/UTF-8 byte packing, receipt и
отсутствие plaintext в `explain()`. Это узкое доказательство контракта, а не
модельный benchmark или попытка заменить другой framework.

### Честные границы

- `v1.0` — версия evidence protocol, не уже вышедший package `1.0.0`.
- `9/9` против `0/9` означает только доступность named target в специально
  сконструированном lexical fixture; не качество ответа модели, не universal
  semantic recall и не latency/throughput.
- Source-revocation case покрывает scrub/exclusion одной fixture-записи;
  атомарная batch revocation и re-ingest denial остаются отдельной property
  conformance областью.
- До `1.0` ещё нужны raw performance protocol на reference hardware,
  held-out quality/conflict evidence, migration proof и reviewed integrations.

## EN

### Short announcement

**ProtoPrompt 0.15 adds verifiable strict-Ledger recall semantics across both
SQLite and PostgreSQL.**

We froze one versioned synthetic fixture, a content-free expected result, and
a SHA-256 manifest. The release gate creates fresh storage for each backend,
runs the same 18-case matrix, and accepts it only when the two reports have
exact parity after normalizing their backend ID.

The protocol checks delayed selection, `superseded`/`retracted`/
`source_revoked`, tenant/user/thread isolation, whole-record token/UTF-8-byte
packing, receipts, and the absence of plaintext in `explain()`. It is narrow
contract evidence, not a model benchmark or an attempt to replace another
framework.

### Honest boundaries

- `v1.0` is the evidence-protocol version, not an already shipped package
  `1.0.0`.
- `9/9` versus `0/9` means only named-target availability in a deliberately
  constructed lexical fixture; it is not model-answer quality, universal
  semantic recall, or latency/throughput.
- The source-revocation case covers scrubbing/exclusion of one fixture record;
  atomic batch revocation and re-ingest denial remain separate property
  conformance scope.
- Before `1.0`, the project still needs raw performance evidence on reference
  hardware, held-out quality/conflict evidence, migration proof, and reviewed
  integrations.

## Maintainer launch checklist

1. Publish no external message before the tag, verified PyPI artifact, GitHub
   Release asset checksums, bilingual docs, and PostgreSQL release gate pass.
2. Reuse the matched RU or EN text without upgrading fixture-local numbers to
   model-quality or performance claims.
3. Link the exact release/PyPI pages and reproducible command; do not request
   payloads, scope values, record IDs, evidence IDs, or DSNs in public
   feedback.
4. Save the future v1 executive report/deck only with raw CI and benchmark
   evidence bound to their hashes; do not create numerical slides from this
   launch kit alone.
