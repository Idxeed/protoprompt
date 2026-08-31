# ProtoPrompt Memory Benchmark v0.1

This is a small, versioned regression suite for the memory and context layer.
It is intentionally **not** a model benchmark, a latency benchmark, or a
third-party leaderboard. It runs without Ollama, network access, API keys, or
optional dependencies.

## Run

From the repository root:

```powershell
.\.venv\Scripts\python.exe scripts\run_memory_benchmark.py --suite v0.1 --verify
```

Optional reports are written only when an explicit path is passed:

```powershell
.\.venv\Scripts\python.exe scripts\run_memory_benchmark.py --suite v0.1 `
  --json benchmarks\results\local.json `
  --markdown benchmarks\results\local.md
```

`--verify` compares a freshly calculated semantic report with the frozen
`expected.json` and exits non-zero on any regression. The CI job runs that
command. It does not make a network request.

## What v0.1 covers

| Case | Contract |
|---|---|
| `delayed_cold_recall` | An early fact remains retrievable after SQLite close/reopen while a two-record tail window loses it. |
| `near_collision_recall` | A seeded feature-hash retrieval picks the target rather than a deliberately similar distractor. |
| `scope_isolation` | Equal logical memory IDs in independently changed tenant, user, and thread scopes do not leak into search or ContextPlan. |
| `final_request_budget` | RAG, session evidence, a tool-call/result pair, final input, provider framing, and output reserve reconcile in one request receipt. |

The suite verifies semantic outcomes only: evidence availability, target rank,
scope leaks, budget violations, receipt reconciliation, decision-contract
coverage, and whether `ContextPlan.explain()` stays content-free. It excludes
trace IDs, opaque source IDs, raw similarity scores, timings, host/platform
metadata, and all local database paths.

## Fixed columns

- `tail_window_v1`: last two primary records; transparent bounded-context
  baseline. Its supported recall payloads pass through the deterministic
  final-request packer before claiming zero budget violations.
- `rolling_summary_v1`: deterministic fixture key/value ledger, last write per
  key; deliberately not an LLM summary claim. Its supported recall payloads
  use the same final-request packer.
- `vector_recall_v1`: scoped `MemoryService.search()` using the seeded local
  embedding contour, including cold SQLite reopen where applicable, followed by
  the same final-request packer.
- `protoprompt_0_6_1`: frozen reference from tag `v0.6.1`, never executed
  through current code. `not_supported` means that the legacy public API had
  no ContextPlan/receipt/explanation contract, not that it failed.
- `protoprompt_context_plan_v0_7`: the current candidate, exercised through
  `TokenBudgetedContextBuilder.plan_messages()`.

Some baselines are intentionally marked `not_supported` for a case outside
their contract (for example, a bare tail window cannot demonstrate enforced
scope isolation or final-request accounting). This avoids presenting a missing
capability as either a pass or a failure.

## What v0.2 covers — Ledger request composition

`v0.2` is a separate frozen semantic suite for the experimental
`LedgerContextComposer`; it does not rewrite or compare itself to `v0.1`
baselines because prior releases did not have this narrow capability.

```powershell
.\.venv\Scripts\python.exe scripts\run_memory_benchmark.py --suite v0.2 --verify
```

The frozen fixture SHA-256 is
`9bc849dd1d441b2c53d0bad558666b1dd22ad4cf4c8302d5ef5c005102f271c1`. Its
verified reference outcome is 5/5 cases and 17/17 semantic checks passing:

| Case | Contract |
|---|---|
| `strict_raw_exclusion` | An admitted document record enters the strict lane; a confirmed raw `unknown` record does not reach messages or `explain()`. |
| `ledger_lane_budget` | Exact request boundary fits, while a one-token-short mandatory lane fails as `ledger_data`; receipts reconcile. |
| `tool_dependency` | The fixed Ledger prefix precedes and does not split an assistant tool-call / tool-output pair. |
| `content_free_explain` | Injection-shaped memory decodes only from the `user` JSON lane, never a system message or content-free explanation. |
| `stale_forget_race` | An event-gated concurrent `forget()` that wins during asynchronous context work causes fail-closed final validation. |

`v0.2` reports PASS/FAIL contract checks only. It deliberately contains no
latency, model-quality, hardware, provider, or prompt-injection-immunity claim.

The packer is explicitly versioned as `greedy-final-request-packer-v1`. It
accounts for system text, optional evidence, optional history, final input,
per-message framing from `RegexTokenCounter`, and the same output reserve as
the candidate. It is deliberately simpler than ContextPlan and does not claim
its tool-dependency or explainability guarantees.

## What v0.3 covers — sealed Ledger checkpoint resume

`v0.3` is a separate frozen semantic suite for the experimental v0.12 sealed
Ledger checkpoint boundary. It does not measure an agent loop, tool execution,
latency, throughput, or retrieval quality.

```powershell
.\.venv\Scripts\python.exe scripts\run_memory_benchmark.py --suite v0.3 --verify
```

The frozen fixture SHA-256 is
`38ab32e37d7f736152710108d0df8a60e9782ef0c94e1cc6e2be7a6a1cb4b1b6`.
Its verified reference outcome is 4/4 cases and 13/13 semantic checks passing:

| Case | Contract |
|---|---|
| `restart_success` | A strict sealed selection re-plans and composes after a SQLite restart; raw memory returns only in the fixed user data lane. |
| `tamper_rejected` | A direct SQLite mutation of a sealed manifest is rejected by host-held HMAC verification. |
| `lifecycle_invalidated` | Forgetting a selected record invalidates its checkpoint, removes selection markers, and blocks resume. |
| `resume_query_binding_and_composition_boundary` | A fresh resume cannot be composed for an unrelated query and preserves the fixed data-lane/request-receipt boundary. |

The fixture, frozen expected report, and rendered reports contain no payload,
checkpoint secret, or scope correlation ID. This is a PASS/FAIL contract for a
narrow host-owned selection continuation, not a claim of agent-state recovery,
lease/exactly-once behavior, or checkpointed workflow execution.

## What v0.4 covers — host-confirmed task-episode resume

`v0.4` is a separate frozen semantic suite for the experimental v0.17
`TaskResumePlanner` boundary. It uses deterministic SQLite only and does not
measure model quality, latency, workflow recovery, exactly-once execution, or
procedure conflict resolution.

```powershell
.\.venv\Scripts\python.exe scripts\run_memory_benchmark.py --suite v0.4 --verify
```

The frozen fixture SHA-256 is
`11178b65593b008a0218927e44cf50d48182f99cc6bb7115121a3e03a16b7c27`.
Its verified reference outcome is 5/5 cases and 21/21 semantic checks passing:

| Case | Contract |
|---|---|
| `restart_mapping_live_query_rag` | A host reconstructs the adapter after SQLite restart from its own task mapping, while the live request query remains the RAG query and does not replace the frozen descriptor. |
| `strict_host_episode_typed_enforcement` | Only admitted `host_assertion` `Episode` data can seal; document/procedure records are excluded and malformed or cross-task episode data fails closed. |
| `task_and_parent_scope_isolation` | Separate task references and equal task references under distinct parent scopes cannot cross-read checkpoints. |
| `continuation_and_lifecycle_fail_closed` | A mismatched continuation reference and a forgotten selected episode both block composition. |
| `receipt_redaction_and_composer_owned_lane` | Lookalike Ledger JSON in user, history, or final messages cannot replace the fixed composer-owned data lane; public receipts remain content-free. |

The fixture and frozen expected report contain contract metadata and PASS/FAIL
outcomes only—no task descriptor, host mapping, checkpoint identifier, scope
correlation ID, payload, source/evidence reference, or checkpoint secret. The
suite is a narrow host-confirmed reference-data continuation check, not an
agent workflow or procedure-execution claim.

## What v1.0 covers — frozen dual-backend Ledger recall evidence

`v1.0` is the version of this **evidence protocol**, not a claim that the
package has already shipped `1.0.0`. It is a narrow, deterministic regression
gate for the strict Ledger read path on both durable implementations:
SQLite and PostgreSQL. It does not call a model, embedding service, provider,
or remote retrieval service; PostgreSQL is a local durable-storage dependency.

It requires a local PostgreSQL service and the optional dependency:

```bash
pip install -e ".[postgres,dev]"
export PROTOPROMPT_POSTGRES_DSN=postgresql://protoprompt:protoprompt@localhost:5432/protoprompt_test
python scripts/run_memory_benchmark.py --suite v1.0 --ledger-backend all --verify
```

`--verify` deliberately refuses a single backend. It runs the same fixture in
fresh SQLite and PostgreSQL storage, normalizes only the backend identifier,
and requires both reports to equal the same frozen expected outcome. The
fixture SHA-256 and expected-report SHA-256 are bound by
[`fixtures/v1.0/manifest.json`](fixtures/v1.0/manifest.json).

The fixed matrix contains 18 cases: delayed active recall at 100, 500, and
1,000 records under 1k/2k/4k token and independent byte budgets; then
superseded, retracted, and source-revoked lifecycle cases for each depth. It
also tests tenant, user, and thread isolation across the three depths,
whole-record token/byte packing, plan/resolve receipt reconciliation, and
content-free explanations.

Its reported `9/9` strict-Ledger versus `0/9` 20-record-tail result is only
**target availability in this named, synthetic lexical fixture**. The query
contains the target terms while fillers intentionally do not. It is neither
model-answer quality, general semantic recall, latency, throughput, a
10k-record runtime result, nor a comparison with another framework.

## Fixture policy

`fixtures/v0.1/suite.json` is immutable. It records the schema, embedding
algorithm and seed, collision guards, records, scopes, request budget, and
cases. The report carries its canonical SHA-256. `expected.json` and the
frozen `protoprompt-0.6.1.json` reference are bound to that exact hash.

Changing a case, seed, expected semantic outcome, or baseline policy creates a
new directory such as `fixtures/v0.3/`; it does not rewrite an earlier suite. Generated
reports under `benchmarks/results/` remain local and are ignored by Git.

## Boundaries

Feature hashing is a deterministic test double for retrieval plumbing; it
does not predict how a production embedding model will perform. The v0.1 suite
does not claim lifecycle semantics such as conflict resolution, supersession,
or retraction—those belong to the versioned Memory Ledger work in v0.8.

---

## По-русски

Это воспроизводимый офлайн-regression suite для памяти и сборки контекста, а
не бенчмарк моделей, скорости или сравнительная таблица с чужими продуктами.
Он не требует Ollama, сети, ключей или optional-зависимостей.

`v0.1` проверяет: recall старого факта после SQLite reopen, выбор из близких
фактов, отсутствие cross-scope утечки и финальный запрос с RAG, session,
tool-pair и output reserve. Фикстуры версионированы и неизменяемы; изменение
сценария создаёт `v0.2`, а не переписывает baseline. `not_supported` честно
обозначает отсутствие контракта у baseline, а не успешный/неуспешный результат.

`v0.2` — отдельный frozen semantic suite для experimental
`LedgerContextComposer`; он не переписывает `v0.1` и не делает искусственного
сравнения с прежними релизами, где этого узкого API не было. Запуск:

```powershell
.\.venv\Scripts\python.exe scripts\run_memory_benchmark.py --suite v0.2 --verify
```

Зафиксированный SHA-256 fixture:
`9bc849dd1d441b2c53d0bad558666b1dd22ad4cf4c8302d5ef5c005102f271c1`.
Проверенный результат — 5/5 cases и 17/17 semantic checks: strict exclusion
raw `unknown`, точная `ledger_data` boundary, сохранение tool-pair,
injection-shaped payload только в `user` JSON lane без утечки в `explain()`, и
event-gated race `forget()` с fail-closed final validation. Это PASS/FAIL
контракт, не claim о latency, качестве модели, железе, provider-е или полной
защите от prompt injection.

`v0.3` — отдельный frozen semantic suite для sealed Ledger checkpoint boundary
из v0.12. Он не измеряет agent loop, tool execution, latency, throughput или
качество retrieval:

```powershell
.\.venv\Scripts\python.exe scripts\run_memory_benchmark.py --suite v0.3 --verify
```

SHA-256 зафиксированной fixture:
`38ab32e37d7f736152710108d0df8a60e9782ef0c94e1cc6e2be7a6a1cb4b1b6`.
Проверенный результат — 4/4 cases и 13/13 semantic checks:

| Case | Контракт |
|---|---|
| `restart_success` | Strict sealed selection заново планируется и компонуется после SQLite restart; raw memory возвращается только в фиксированном user data lane. |
| `tamper_rejected` | Прямая SQLite-мутация sealed manifest отклоняется HMAC-проверкой у host-а. |
| `lifecycle_invalidated` | `forget()` выбранной записи инвалидирует checkpoint, очищает selection markers и блокирует resume. |
| `resume_query_binding_and_composition_boundary` | Fresh resume нельзя скомпоновать с неродственным query; фиксированные data lane и request receipt сохраняются. |

Fixture, frozen expected report и rendered reports не содержат payload,
checkpoint secret или scope correlation ID. Это PASS/FAIL контракт для узкого
host-owned continuation выбора, не заявление о recovery agent state,
lease/exactly-once behavior или checkpointed workflow execution.

## Что проверяет v0.4 — host-confirmed task-episode resume

`v0.4` — отдельный frozen semantic suite для experimental v0.17 boundary
`TaskResumePlanner`. Он использует только deterministic SQLite и не измеряет
качество модели, latency, workflow recovery, exactly-once execution или
разрешение конфликтов procedure.

```powershell
.\.venv\Scripts\python.exe scripts\run_memory_benchmark.py --suite v0.4 --verify
```

SHA-256 зафиксированной fixture:
`11178b65593b008a0218927e44cf50d48182f99cc6bb7115121a3e03a16b7c27`.
Проверенный результат — 5/5 cases и 21/21 semantic checks:

| Case | Контракт |
|---|---|
| `restart_mapping_live_query_rag` | Host после SQLite restart заново создаёт adapter из своей task mapping, а живой request query остаётся RAG query и не заменяет frozen descriptor. |
| `strict_host_episode_typed_enforcement` | Seal допускает только admitted `host_assertion` `Episode`; document/procedure исключаются, malformed и cross-task episode data fail closed. |
| `task_and_parent_scope_isolation` | Разные task refs и одинаковый task ref в разных parent scopes не могут cross-read checkpoint. |
| `continuation_and_lifecycle_fail_closed` | Несовпадающий continuation ref и забытый selected episode оба блокируют composition. |
| `receipt_redaction_and_composer_owned_lane` | Lookalike Ledger JSON в user, history или final messages не может заменить fixed composer-owned data lane; public receipts остаются content-free. |

Fixture и frozen expected report содержат только metadata контракта и PASS/FAIL
результаты: без task descriptor, host mapping, checkpoint ID, scope correlation
ID, payload, source/evidence ref или checkpoint secret. Это узкая проверка
host-confirmed continuation reference-data, а не claim об agent workflow или
исполнении procedure.

## Что проверяет v1.0 — frozen dual-backend Ledger recall evidence

`v1.0` здесь — версия **протокола доказательств**, а не заявление о выходе
пакета `1.0.0`. Это узкий детерминированный regression gate strict Ledger
read-path сразу для двух durable реализаций: SQLite и PostgreSQL. Он не
вызывает модель, embedding-service, provider или remote retrieval service;
PostgreSQL остаётся локальной durable-storage зависимостью.

Нужны локальный PostgreSQL и optional-зависимость:

```powershell
pip install -e ".[postgres,dev]"
$env:PROTOPROMPT_POSTGRES_DSN = "postgresql://protoprompt:protoprompt@localhost:5432/protoprompt_test"
python scripts/run_memory_benchmark.py --suite v1.0 --ledger-backend all --verify
```

`--verify` намеренно не принимает один backend: один и тот же fixture
запускается в fresh SQLite и PostgreSQL storage, из отчётов нормализуется
только ID backend-а, и оба обязаны совпасть с одним frozen expected. SHA-256
fixture и expected-report скреплены
[`fixtures/v1.0/manifest.json`](fixtures/v1.0/manifest.json).

Фиксированная матрица содержит 18 cases: delayed active recall на 100, 500 и
1 000 records при token budget 1k/2k/4k и независимом byte budget; затем для
каждой глубины идут lifecycle cases `superseded`, `retracted` и
`source_revoked`. Она также проверяет tenant/user/thread isolation по трём
глубинам, whole-record token/byte packing, plan/resolve receipt reconciliation
и content-free explain.

Результат `9/9` strict Ledger против `0/9` 20-record tail означает только
**доступность target в этом именованном synthetic lexical fixture**: query
содержит слова target, а fillers намеренно их не содержат. Это не качество
ответов модели, не общее semantic recall, не latency/throughput, не результат
на 10k records и не сравнение с другим framework.
