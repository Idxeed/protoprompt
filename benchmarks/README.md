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

The packer is explicitly versioned as `greedy-final-request-packer-v1`. It
accounts for system text, optional evidence, optional history, final input,
per-message framing from `RegexTokenCounter`, and the same output reserve as
the candidate. It is deliberately simpler than ContextPlan and does not claim
its tool-dependency or explainability guarantees.

## Fixture policy

`fixtures/v0.1/suite.json` is immutable. It records the schema, embedding
algorithm and seed, collision guards, records, scopes, request budget, and
cases. The report carries its canonical SHA-256. `expected.json` and the
frozen `protoprompt-0.6.1.json` reference are bound to that exact hash.

Changing a case, seed, expected semantic outcome, or baseline policy creates a
new directory such as `fixtures/v0.2/`; it does not rewrite `v0.1`. Generated
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
