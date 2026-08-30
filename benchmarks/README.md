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
