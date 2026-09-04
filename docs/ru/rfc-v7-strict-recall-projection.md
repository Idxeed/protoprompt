# RFC: strict recall candidate projection v7

> **Примечание о версиях.** `v7` в этом RFC — версия предлагаемого протокола
> SRCP, а не главной storage schema Ledger. Storage schema Ledger v7 теперь
> зарезервирована под durable receipt точной очистки payload. Будущее storage
> внедрение SRCP должно использовать более позднюю миграцию schema (v8 или
> новее), сохраняя описанную здесь семантику протокола.

**Статус:** локальное предложение для implementation review. Это не изменение
public API, storage version, runtime claim или решение о релизе.

Полный normative текст с логической схемой и матрицей тестов находится в
[английской RFC](/en/rfc-v7-strict-recall-projection/). Эта страница
фиксирует ту же границу решения по-русски.

## Зачем это нужно

Строгий v6 Ledger planner корректно читает весь active window, проверяет
relations и immutable admission audits, извлекает lexical terms из payload и
пакует целые JSON records. Для небольших окон это правильный default, но при
full-window 10k он десериализует и валидирует слишком много data. Текущий
local verification не выполняет roadmap gate `p95 planning <= 50 ms`.

Предлагаемый **SRCP-v7** — private, rebuildable read projection из canonical
Ledger tables. Он может быстрее выбрать кандидатов для ranking, но не может
подтвердить candidate, сделать record recallable, назначить scope или
авторизовать write. Единственным source of truth остаётся canonical Ledger.

`v7` здесь означает возможную будущую storage schema 7, а не обещанную
версию пакета или record schema.

## Scope и не-цели

В scope только strict host-controlled recall lane с audited admission и exact
owned `RegexTokenCounter`. Проекция должна воспроизводить v6 selection:
active-window order, policy exclusions, candidate/scan-byte limits, Unicode
term normalization, relevance/confidence/recency ranking, tie-breakers и
whole-record token/byte packing.

Она не добавляет embeddings, LLM, network I/O, vector DB, FTS dependency,
storage-plugin API или model-controlled endpoint. Она не хранит plaintext,
task text, source/evidence refs, rendered context или plan между запросами и
не создаёт universal latency/throughput claim.

## Решение и обязательные gates

SRCP-v7 допускается только если одновременно доказано:

1. Для одного canonical snapshot он даёт те же selection markers,
   content-free decisions, token/byte receipt, rendered context и
   stale-plan behaviour, что v6.
2. Missing/stale/malformed projection row, incomplete migration, unavailable
   key, integrity error или unsupported counter запускают named
   `strict_v6_fallback`, а не пустой/частичный recall.
3. Lifecycle, `forget`, source revocation и hard erase изменяют canonical
   rows и все projection derivatives в одной backend transaction.
4. SQLite и реальный disposable PostgreSQL проходят один semantic,
   concurrency и erasure conformance suite.
5. Только operator-verified reference manifest с exact commit и full 10k
   coverage может доказать `p95 <= 50 ms`; dirty-worktree или
   verification-only timing этого не доказывает.

Решение отклоняется, если target достигается через approximate ranking,
lossy term index, пропуск audit verification, более слабый final resolve или
скрытое расширение default limits.

## Safety model и fallback

Ускоренный путь разрешён лишь при concrete allowed origins,
`require_admission_audit=true`, exact `RegexTokenCounter` и готовой проекции
для scope/lexer/renderer/key generation. Старый compatibility policy и любой
third-party `TokenCounter` остаются на консервативном v6 path.

SRCP-row связывает exact scope, record ID, current revision, `content_hash`,
kind, origin, lifecycle/validity fields и immutable admission proof: audit
event, candidate revision, policy receipt, action/reason и paired lifecycle
event command hash. Reader сверяет эти поля с canonical tables в одном read
snapshot. `unknown` и `legacy_unknown` не могут попасть в strict lane.

Финальный `resolve()` не доверяет projection: он по-прежнему читает только
sealed selected IDs из canonical Ledger, проверяет membership в exact top-N
window, revision/hash/kind/policy/lifecycle и в короткой final transaction
линеаризует delete/transition race. Поэтому выигравший `forget`/`retract`
делает plan stale, как в v6.

## Производные данные и erasure

Projection не хранит content. Для exact lexical matching она хранит только
domain-separated HMAC-SHA-256 tags нормализованных v6 terms; host key не
пишется в SQLite/PostgreSQL, export, receipt, telemetry или log. Tags,
content hashes, timestamps и accounting metadata всё равно sensitive
derivatives: они исключаются из public `explain()` и default export, входят
в protected backup и удаляются вместе с memory data.

| Canonical action | Обязательное действие SRCP в той же transaction |
| --- | --- |
| allow/confirm | Создать candidate row, term tags и complete coverage state после lifecycle event/audit; ошибка derivation aborts strict-required write либо переводит scope в fallback-required. |
| quarantine/reject/supersede/retract/expire | Удалить или инвалидировать active projection/tags и selected checkpoints. |
| `forget` / `forget_by_source` | Удалить все candidate/term/coverage derivatives до commit payload/source erasure; source tombstone и cascade остаются атомарными. |
| hard erase | Под тем же controlled guard удалить все projection rows/tags и доказать отсутствие orphan rows до commit. |

Key rotation создаёт отдельное generation. Пока оно не полностью покрывает
canonical active records, используется старое compatible generation или v6
fallback; один plan никогда не смешивает generations.

## SQLite/PostgreSQL и migration

Логическая схема одна: candidate row, HMAC term table и per-scope
coverage/generation row. SQLite и PostgreSQL могут использовать native BLOB /
`bytea`, parameter batches, indexes и transaction primitives, но не разные
semantics. SQLite сохраняет `BEGIN IMMEDIATE` на final resolve; PostgreSQL —
существующий transaction-scoped advisory lock. PostgreSQL parity нельзя
закрыть collection-only тестом.

v7 — explicit migration. `dry_run_setup()` показывает prerequisites и работу
без payload disclosure. SQLite делает operator-selected file-copy backup до
v6→v7 и остаётся strict-v6/fallback, пока rebuild не докажет complete coverage.
PostgreSQL требует reviewed operator backup/restore or staged-migration
runbook. Если backend всё ещё умеет только fresh schema, это должно быть в
capability receipt и не может считаться закрытым migration gate.

Rollback означает restore verified backup либо отключение projection с v6
fallback — не destructive down-migration canonical events/payloads. Projection
rebuildable, но никогда не является источником восстановления canonical memory.

## Приёмочные критерии

До реализации/release должны пройти как минимум следующие проверяемые gates:

1. Differential seeded/property suite сравнивает SRCP с v6 для strict
   policies, Unicode/ASCII/CJK, empty/no-match terms, ties, limits и budgets.
2. Строго проверяются audit/event/origin/revision/content-hash mismatches,
   stale/missing/duplicate rows, wrong key/lexer/renderer и index corruption:
   только fallback либо fail closed.
3. Fault injection покрывает все точки между canonical mutation, audit,
   projection rows/tags, checkpoint invalidation и coverage state; после
   restart нет usable half-ready scope.
4. Concurrency suite покрывает plan vs confirm/supersede/retract/expire/
   forget/forget-by-source/erase, включая вытеснение selected record из
   bounded active window.
5. Erasure suite доказывает ноль projection/term rows после `forget`/hard
   erase и source revocation, без утечки derivatives в events/receipts/export.
6. Shared storage conformance запускается для SQLite и real PostgreSQL,
   включая migration/backup/restore/restart path и incomplete-build fallback.
7. Query instrumentation запрещает N+1 reads по records/relations/audits/
   payloads; full payload materialization разрешена только для selected IDs
   и normal final resolve.
8. Existing raw protocol выполняется без изменения условий: ровно 10k
   persisted/active/eligible/candidates/scanned, no truncation, budget 2048/
   32768, 5 warmups, 30 retained samples, no remote/provider/embedding I/O.
9. На operator-verified reference setup plan p95 `<=50 ms` при
   `execution_mode=strict_projection_v7`; JSON, manifest и commit сохраняются
   вместе. Repeat gate зелёный два release cycles подряд.

До выполнения этих критериев SRCP-v7 остаётся design candidate, а не
доказательством готовности 1.0.
