# Raw local performance protocol

`v0.1`–`v0.5` and the Ledger `v1.0` suite are deterministic semantic
regression gates. They intentionally do **not** report latency, throughput,
or model quality. This separate protocol is the evidence scaffold required
before a runtime claim can be considered for the `1.0.0` roadmap.

It is local-only and dependency-light: a fresh on-disk SQLite Ledger receives
one deterministic synthetic corpus of **10,000** host-confirmed records in one
scope. The measurement path uses `LedgerRecallPlanner.plan()` and
`LedgerRecallPlanner.resolve()` only. It does not call a model, embedding
service, provider, network service, or remote database.

## What is fixed

| Item | Protocol value |
|---|---|
| Corpus | 10,000 synthetic local SQLite Ledger records in one scope |
| Corpus generator | `protoprompt-ledger-raw-performance-corpus-v1` with SHA-256 in every report |
| Storage start | Operator supplies an existing local scratch directory; corpus creation is excluded, then SQLite is closed and reopened once before warm-up |
| Warm-up | 5 discarded plan/resolve pairs minimum |
| Samples | 30 retained plan/resolve pairs minimum |
| Clock | `time.perf_counter_ns` |
| Percentiles | p50 and p95 nearest-rank order statistics over raw nanoseconds |
| Warm process | Warm-ups may populate only SQLite's content-free immutable-admission validation markers; any local or external database change invalidates them before the next read |
| Output | Explicit JSON and/or Markdown path only; no implicit artifact is written |

The raw JSON retains each timing sample in nanoseconds. The Markdown is only a
human-readable view; do not discard its matching JSON when comparing runs.

## Reference manifest

Measurements require an operator-reviewed manifest. This prevents an
accidental local timing from being presented as a baseline without its CPU,
RAM, storage, power profile, operating system, Python, SQLite, package, and
source-revision context.

Start with the generated template:

```powershell
.\.venv\Scripts\python.exe -m benchmarks.raw_performance_protocol `
  --init-reference-manifest benchmarks\results\reference-machine.json
```

Or copy the checked-in non-runnable template:

[`fixtures/raw-performance-v1/reference-manifest.template.json`](fixtures/raw-performance-v1/reference-manifest.template.json).

Fill every `REPLACE_*` field, set a positive `physical_memory_gib`, and set
`operator_verified` to `true`. The required
`manifest_class: "operator_verified_reference"` is intentionally explicit.
`source_revision` must be an exact 40-character Git commit; an uncommitted
worktree is not reference evidence. The runner rejects placeholders,
generic/example/local-verification labels, unverified manifests, and unknown
fields. A quick implementation check must stay a verification-only local run,
not a manifest labelled as a reference baseline.

For an implementation check on a dirty worktree or unknown machine metadata,
use the explicit
[`verification-manifest.template.json`](fixtures/raw-performance-v1/verification-manifest.template.json)
class instead. It requires `manifest_class: "verification_only"` and
`operator_verified: false`; its Markdown report carries a prominent
**not eligible as a reference baseline** banner. A verification-only result
may exercise the runner but cannot become a reference comparison by changing
its prose or report filename.

## Run one local measurement

```powershell
.\.venv\Scripts\python.exe -m benchmarks.raw_performance_protocol `
  --reference-manifest benchmarks\results\reference-machine.json `
  --storage-directory C:\BenchmarkScratch\protoprompt `
  --json benchmarks\results\raw-performance.json `
  --markdown benchmarks\results\raw-performance.md
```

`benchmarks/results/` is intentionally ignored by Git. The protocol makes a
new temporary SQLite file beneath the explicit operator-selected scratch
directory, deletes it after the process exits, and does not modify any
semantic benchmark fixture or baseline. It never falls back to the system
temporary directory. The report omits the path; an operator-verified manifest
attests that the chosen volume is local, because Python cannot prove physical
storage locality (for example, a mapped drive) by itself.

The run first populates the corpus outside timed operations, closes/reopens the
database, discards five warm-ups, then records at least thirty pairs:

1. `ledger_recall_plan`: local active-record read, eligibility, lexical score,
   whole-record packing, and plan receipt construction.
2. `ledger_recall_resolve`: local re-read, render, and final lifecycle
   validation for that plan.

The report separately records corpus population time but excludes it from p50
and p95 planning/resolve values. It also records the exact fixed token/byte
budgets, effective recall policy, and whether the planner reached a read or
candidate limit. A caller cannot vary those budgets while claiming this same
10k/50ms roadmap gate.

The warm process does **not** cache records, payloads, or rendered context.
SQLite may reuse a successful immutable admission-sidecar validation only while
both its external `data_version` and local change counter are unchanged. A
database change clears those content-free markers before the next active read,
which re-runs the full fail-closed audit check. PostgreSQL retains its uncached
strict-audit path.

## Current interpretation boundary

The conservative `LedgerRecallPolicy` defaults remain 1,000 active records
and 100 candidates. Its explicit public maxima now permit **10,000** for both
fields; this protocol opts into those 10k values rather than silently changing
the product default. The runner refuses a full-corpus measurement if the
active read, candidate, or scan-byte budget truncates the generated corpus.

An operator-verified reference run evaluates the roadmap p95 gate only when
the report proves all of these facts together: exactly 10,000 persisted
records, a 10,000-record active slice, 10,000 planner-visible and eligible
records, 10,000 candidates, all 10,000 scanned, and no candidate-limit
truncation. `active_read_limit_reached: true` is expected at the explicit
10,000 limit; the independently verified persisted count prevents that flag
from hiding additional rows. Only then does the report emit `observed_pass` or
`observed_fail` for that one declared configuration.

A `verification_only` run records the same raw timings and a diagnostic
target comparison, but its roadmap gate remains `not_evaluated` even with full
coverage. It cannot be cited as a reference baseline, comparison, or runtime
claim.

## Claims this protocol never makes

- No universal latency or throughput promise.
- No model-answer quality, general semantic-recall, or prompt-injection
  immunity claim.
- No embedding/provider/network performance claim.
- No comparison or superiority claim against another framework.
- No cross-hardware comparison: compare reports only when their reviewed
  manifest and source revision are intentionally equivalent.

The protocol is not a CI semantic gate and its raw timing results are not a
frozen expected JSON. CPU load, power state, OS scheduling, storage state, and
the executed source revision all matter; consider a result reference-comparable
only with its raw JSON and strictly validated manifest. A `local verification`,
`example`, or generic-label run is deliberately rejected rather than allowed to
masquerade as a reference result.

---

## По-русски

`v0.1`–`v0.5` и Ledger `v1.0` — детерминированные semantic regression gates,
а не тесты latency/throughput. Этот отдельный протокол — заготовка доказательств
для будущего runtime-критерия `1.0.0`.

Он работает только локально: в явно указанной оператором существующей local
scratch directory во fresh on-disk SQLite Ledger создаётся один детерминированный
synthetic corpus из **10 000** host-confirmed записей в одном scope. Измеряются лишь `LedgerRecallPlanner.plan()` и
`LedgerRecallPlanner.resolve()`; нет модели, embedding/provider service,
сети или remote DB.

Методика жёстко фиксирует минимум пять отбрасываемых warm-up пар, минимум
тридцать сохраняемых повторов, `time.perf_counter_ns` и nearest-rank p50/p95.
JSON отчёт хранит все raw samples в наносекундах; Markdown — только удобное
представление.

Warm-up может заполнить только content-free markers успешной проверки
неизменяемых admission sidecars SQLite. Ни records, ни payload, ни rendered
context не кешируются. Любое local/external изменение базы сбрасывает markers
по `data_version` и local change counter перед следующим read, после чего
полная fail-closed audit validation выполняется заново. PostgreSQL сохраняет
uncached strict-audit path.

Перед запуском нужен операторски проверенный hardware/software manifest.
Создайте шаблон:

```powershell
.\.venv\Scripts\python.exe -m benchmarks.raw_performance_protocol `
  --init-reference-manifest benchmarks\results\reference-machine.json
```

Заполните CPU, объём RAM, local storage, power profile, OS, Python, SQLite,
версию ProtoPrompt и точный 40-символьный Git commit; затем поставьте
`operator_verified: true`. Поле
`manifest_class: "operator_verified_reference"` обязательно и намеренно
явное. Незаполненный, generic/example/local-verification, uncommitted-worktree
или расширенный неизвестными полями manifest runner отвергнет. Быстрый local
implementation check не должен маркироваться как reference baseline.

Для implementation check на dirty worktree или с неизвестными metadata есть
явный
[`verification-manifest.template.json`](fixtures/raw-performance-v1/verification-manifest.template.json):
у него строго `manifest_class: "verification_only"` и
`operator_verified: false`. В Markdown появляется заметный статус
**not eligible as a reference baseline**. Такой результат проверяет runner,
но не может стать reference comparison простой сменой имени файла или текста.

Запуск с явными локальными путями:

```powershell
.\.venv\Scripts\python.exe -m benchmarks.raw_performance_protocol `
  --reference-manifest benchmarks\results\reference-machine.json `
  --storage-directory C:\BenchmarkScratch\protoprompt `
  --json benchmarks\results\raw-performance.json `
  --markdown benchmarks\results\raw-performance.md
```

Corpus population вынесен за пределы измерения; после него база закрывается и
открывается снова, warm-up отбрасывается. В system temp protocol не пишет:
временный SQLite создаётся только под явно заданной directory и удаляется после
run. Путь в report не попадает; local physical storage — operator attestation,
а не то, что Python может надёжно доказать для mapped drive. В отчёт попадают
p50/p95 для plan, resolve и end-to-end, fixed token/byte budgets, effective
policy и raw samples.

### Текущая честная граница

Консервативные defaults `LedgerRecallPolicy` не меняются: 1 000 active records
и 100 candidates. Но публичный максимальный предел обоих полей теперь явно
разрешает **10 000**, и этот protocol осознанно выбирает именно такие значения,
не расширяя default незаметно. Runner откажется от full-corpus measurement,
если active read, candidate или scan-byte budget обрежет corpus.

Operator-verified reference run оценивает roadmap p95 gate только если отчёт
одновременно подтверждает: ровно 10 000 persisted records, active slice из
10 000, 10 000 видимых planner и eligible записей, 10 000 candidates, все
10 000 scanned и отсутствие candidate-limit truncation.
`active_read_limit_reached: true` ожидаем при явном лимите 10 000; независимо
проверенный persisted count не даёт этому флагу скрыть дополнительные строки.
Только тогда возможен `observed_pass`/`observed_fail` для одной указанной
конфигурации.

`verification_only` run сохраняет те же raw timings и diagnostic comparison с
target, но его roadmap gate остаётся `not_evaluated` даже при full coverage.
Его нельзя трактовать как reference baseline, comparison или runtime claim.

Протокол никогда не обещает universal latency/throughput, качество ответов
модели, general semantic recall, prompt-injection immunity, скорость
embedding/provider/network, или превосходство над другим фреймворком. Сравнение
имеет смысл лишь для намеренно эквивалентных строго validated manifest и source
revision; raw JSON и manifest должны оставаться рядом с любым опубликованным
результатом. `local verification`, `example` и generic labels намеренно не
могут пройти как reference evidence.
