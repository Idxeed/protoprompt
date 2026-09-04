# RFC: v7 strict recall candidate projection

> **Versioning note.** `v7` in this RFC names the proposed SRCP projection
> protocol, not the main Ledger storage schema. Ledger storage schema v7 is
> now reserved for durable exact-scope payload-purge receipts. A future SRCP
> storage deployment must therefore use a later storage-schema migration
> (v8 or later), while preserving the protocol semantics described here.

**Status:** proposal for local implementation review. It is not a public API,
storage-version change, performance claim, or release decision.

**Scope:** a durable, derived candidate projection for the strict Ledger recall
lane. The proposal exists to make the existing 10,000-record local planning
gate achievable without weakening the v6 lifecycle, scope, audit, deletion, or
final-resolution semantics.

## 1. Problem and decision boundary

The v6 strict planner deliberately obtains complete `MemoryRecord` objects for
the bounded active window, parses their relations and admission-audit sidecars,
derives lexical terms from plaintext, ranks them, and packs whole JSON records.
That is a sound default for a small window, but it makes a 10k full-window
benchmark do work proportional to all payloads, relations, and audit receipts.
The performance protocol therefore measures hundreds of milliseconds on the
current local implementation; it does not meet the roadmap's `p95 <= 50 ms`
planning gate.

This RFC proposes a **strict recall candidate projection** (abbreviated
`SRCP-v7`). It is a private, rebuildable read model derived from the Ledger's
canonical tables. It may make a record a *candidate for ranking*; it never
makes a record recallable, confirms a candidate, owns scope, or authorizes a
write. The canonical Ledger remains the sole source of truth.

The v7 label is intentionally about a prospective **storage schema 7**. It
does not promise that the public record schema, storage contract, or package
version changes here. Those version decisions require a separate API/storage
RFC and migration review.

### Decision

Implement SRCP-v7 only if all of the following can be proven:

1. For its supported strict-policy lane, it produces the same selection,
   content-free decision receipt, token/byte accounting, and stale-plan
   behavior as the v6 planner for the same canonical snapshot.
2. A projection anomaly, missing row, unsupported counter, incomplete
   migration, unavailable host key, or failed integrity check selects the
   existing strict v6 path; it never silently returns a smaller answer set.
3. Every write, lifecycle transition, source revocation, `forget`, and hard
   erase updates or removes all projection derivatives in the same storage
   transaction as the canonical mutation.
4. SQLite and PostgreSQL pass the same semantic/concurrency/erasure suite.
5. An operator-verified reference run of the existing raw protocol proves the
   10k full-coverage planning gate. A local verification-only timing is not
   sufficient evidence.

The proposal must be rejected if meeting the timing target requires a lossy
term index, a trust exception, skipped audit verification, approximate
ranking, a larger default read limit, or a weaker final resolve boundary.

## 2. Non-goals

SRCP-v7 does not:

- add embeddings, an LLM call, network I/O, a vector database, or FTS as a
  required dependency;
- make the compatibility recall policy faster by changing its semantics;
- expose a storage-plugin protocol or a model-controlled projection endpoint;
- cache plaintext records, rendered context, task text, source/evidence IDs,
  or completed plans across requests;
- turn a database backup/PITR policy into a core guarantee; or
- claim universal latency, throughput, semantic-answer quality, or physical
  erasure from backups/WAL/replicas.

The narrow target is a host-configured strict lane using an explicit audited
policy and the owned `RegexTokenCounter` contract. The public default remains
conservative unless a future stable-API decision says otherwise.

## 3. Semantics that must remain exactly v6

The projection is accepted only if it preserves these v6 facts at the same
linearization boundaries:

| Invariant | Required v7 behavior |
| --- | --- |
| Scope | Every candidate query uses the exact `scope_id` and canonical `scope_json` pinned by `MemoryWriter`; no caller/model input widens it. |
| Active window | The window is the exact `state=active`, `trust=host_confirmed`, payload-present, validity-current top-N ordered by `updated_at DESC, record_id ASC`. |
| Admission | A concrete origin is eligible only with its immutable allow audit correctly bound to the record, origin, candidate revision, lifecycle event, reason, policy receipt and event command hash. `unknown` and `legacy_unknown` cannot enter the strict lane. |
| Lifecycle | Candidate, quarantined, retracted, superseded, expired, forgotten, and erased records are never returned as active candidates. |
| Selection | v6 policy filtering, candidate-limit ordering, scan-byte handling, lexical term normalization, relevance/confidence/recency ranking, exact tie-breakers, and whole-record packing are unchanged. |
| Plan seal | A plan still seals only record ID, revision, content hash, kind, scope, policy, counter, and budgets; it retains no plaintext or task text. |
| Resolve | `resolve()` still re-reads selected records and validates active-window membership, revision, content hash, kind, policy, lifecycle, and final token/byte rendering before returning data. |
| Delete race | A delete/transition that wins before the final validation makes resolution fail closed. A context already returned must not be retained or sent after a later deletion request. |

In particular, a projection query is **not** a replacement for
`_validate_active_snapshot`. The existing final write-lock/advisory-lock
linearization point remains mandatory.

## 4. Supported lane and explicit fallback

An accelerated attempt is allowed only when all conditions are true:

```text
policy.require_admission_audit == true
policy has concrete allowed origins (no unknown / legacy_unknown)
counter is exactly the owned RegexTokenCounter contract and counter_id is
    regex-token-counter-v1
SRCP schema, renderer, lexer and key generation are ready for this scope
canonical active-window coverage matches projection coverage
```

The first implementation may require an explicit `MemoryPolicy` whose recall
origins are concrete and whose admission/recall relationship already passes
the `MemoryPolicy` validation rules. Supporting the older unrestricted-origin
compatibility policy is out of scope.

Any false condition takes the named `strict_v6_fallback` path. The fallback is
not an empty recall, a partial top-k, or a best-effort projected result. It is
the current full strict reader and planner, with the same counter behavior.
The planner's content-free explain receipt should identify only the execution
mode and fallback reason (for example `counter_not_owned`,
`projection_not_ready`, or `projection_integrity_failed`); it must not reveal
record IDs, scope, task terms, source references, paths, key material, or
plaintext.

The raw 10k timing gate is eligible only when the receipt says
`strict_projection_v7`, coverage is complete, and no fallback occurred.

## 5. Logical projection schema

The physical DDL may differ between SQLite and PostgreSQL, but both backends
must represent the following logical data. All fields are scoped by the exact
canonical `(scope_id, scope_json)` pair.

### 5.1 Candidate row

`ledger_strict_recall_candidates_v7` has one row for every currently active,
payload-present canonical record in a projection-ready scope; a plan then
chooses its policy-specific top-N active window from those rows. This includes
a metadata-only row for a record that strict policy excludes due to
legacy/unknown origin. It contains only:

- `record_id`, `record_revision`, `content_hash`, `kind`, `origin`,
  `state`, `trust`, `confidence`, `updated_at`, `valid_from`, `valid_until`;
- the canonical admission binding: `admission_event_id`,
  `admission_candidate_revision`, `admission_policy_id`,
  `admission_policy_version`, `admission_policy_fingerprint`,
  `admission_action`, `admission_reason_code`, and the expected immutable
  admission event command hash;
- exact derived accounting metadata: raw UTF-8 status/byte count,
  rendered-record UTF-8 byte count, and owned-regex rendered-record token
  count;
- `lexer_id`, `renderer_id`, `counter_id`, index-key generation, projection
  schema version, and a content-free projection-binding digest; and
- no content, source/evidence reference, task, embedding, rendered JSON, or
  raw lexical term.

The admission binding must represent the same fact verified by v6 today:
the admission audit names the candidate revision, and its paired lifecycle
event has `revision = candidate_revision + 1`, the right action/reason/origin,
and the expected command hash. The candidate row separately binds its current
`record_revision` and `content_hash` to canonical `memory_records`. A later
lifecycle transition changes the current revision and makes the row unusable
or deletes it; it cannot inherit a former allow audit merely because the
record ID is the same.

`content_hash` is retained only because v6 already uses it as an operational
selection marker. SRCP-v7 must not introduce a second plaintext-derived hash
with weaker handling.

### 5.2 Lexical tag table

`ledger_strict_recall_terms_v7` contains one unique entry per distinct v6
normalized term for an eligible projected record:

```text
(scope_id, scope_json, record_id, record_revision, content_hash,
 lexer_id, index_key_generation, term_tag)
```

`term_tag` is a domain-separated HMAC-SHA-256 value of the normalized term;
the HMAC key is host-owned and is never written to either database. The host
computes request-term tags in memory, queries indexed tags in bounded batches,
and discards them with the request. This preserves the v6 lexical semantics
without putting raw words into a new FTS/index table.

The normalizer is versioned and must be byte-for-byte equivalent to the v6
`_terms()` contract, including its ASCII fast path and Unicode casefold/boundary
behavior. There is no lossy term cap. If a record cannot be represented
exactly (including invalid UTF-8 or an unsupported lexer/key generation), the
scope is not projection-ready and must use strict v6 fallback.

`term_tag` values are still sensitive derived data: they may reveal frequency,
language, and equality of a term within an index-key generation. They receive
the same access control, retention, backup and erasure treatment as memory
payload derivatives.

### 5.3 Scope coverage/generation row

`ledger_strict_recall_scope_state_v7` records a projection generation for one
scope and key/lexer/renderer contract:

- `state`: `building`, `ready`, `fallback_required`, or `retired`;
- canonical active coverage count for the entire scope and a monotonic
  canonical mutation marker;
- candidate coverage count, schema/lexer/renderer/counter/key generation; and
- a content-free integrity/coverage digest.

It is not a cache TTL. A planner may use SRCP only if the state is `ready` and
one snapshot query proves the candidate rows cover every canonical active
record in that scope/window. A missing, duplicated, stale, malformed, or
unbound row yields `fallback_required`, not a partial result.

## 6. Read algorithm

### 6.1 Planning snapshot

The accelerated planner opens the same read snapshot used by v6 and performs
bounded set-oriented work:

1. Read the exact active top-N window from canonical `memory_records` plus
   payload existence and admission metadata. Do **not** read payload text,
   relations, or all audit rows into Python.
2. Join each row to the candidate projection on scope, record ID, revision,
   content hash, lexer/renderer/counter/key generation. For a concrete origin,
   join its immutable audit/event/metadata predicates as a relational proof
   of the v6 allow-audit invariant. For a legacy/unknown row, retain only its
   metadata so the strict planner can emit the existing exclusion reason.
3. Prove coverage: every canonical active row has exactly one compatible
   candidate row, and no candidate row represents a record outside that exact
   window. If the proof fails, abandon the accelerated snapshot and run v6.
4. Apply the current policy's exclusions and `candidate_limit` in the same
   active-window order as v6, before ranking. Use projected raw-content byte
   metadata to reproduce `candidate_scan_byte_budget` and its decision
   reasons exactly.
5. Normalize the in-memory task into v6 terms. HMAC only these request terms;
   query term tags in bounded parameter batches and obtain each candidate's
   distinct intersection count. Do not persist the task or term tags.
6. Calculate relevance, confidence signal, recency signal, rounding, and
   deterministic sort in the existing planner code, not backend-specific SQL
   floating-point expressions. This avoids SQLite/PostgreSQL rounding drift.
7. Under the owned regex counter, use the projected exact per-record rendered
   bytes/tokens for additive packing, then obtain only the selected payloads
   by ID and perform the existing one full-envelope reconciliation. A mismatch
   is a projection failure and falls back/fails closed; it is never rounded or
   clipped.
8. Seal the ordinary v6-shaped plan with canonical record revision/content
   hash/kind markers. The plan does not store a projection row, term tag,
   plaintext, or task text.

The candidate query must use parameterized SQL and a fixed batch size below
both SQLite variable limits and PostgreSQL parameter limits. It must have
supporting indexes for `(scope_id, scope_json, term_tag, record_id)` and the
canonical active-window order. Query-count limits belong in the acceptance
tests; no N+1 relation/audit/payload query is allowed.

### 6.2 Resolve is still canonical

`resolve()` is intentionally not accelerated beyond the existing selected-ID
re-read:

1. Re-read only the sealed selected IDs under the exact active-window
   membership predicate; validate all v6 sidecars and canonical payloads.
2. Render and count the final JSON data envelope outside the write lock.
3. Take the short canonical final validation transaction: SQLite keeps
   `BEGIN IMMEDIATE`; PostgreSQL keeps the transaction-scoped advisory lock.
   Verify top-N membership plus record ID/revision/content-hash/kind markers.
4. Re-check policy and validity at final host time. Return the context only if
   exact budgets still hold; otherwise raise the same stale-plan/budget error.

Projection rows are not read in the final acceptance decision. This is the
key property that preserves deletion and concurrent-transition semantics.

## 7. Write, lifecycle, and erasure protocol

All changes below occur inside the existing canonical write transaction and
under its existing backend-specific linearization mechanism. No best-effort
worker may create a gap in which a scope reports projection-ready while its
rows are incomplete.

| Canonical operation | SRCP-v7 action in the same transaction |
| --- | --- |
| observe/assert candidate | Do not index plaintext. Create at most a metadata coverage marker in `building`; it is not recallable. |
| review allow / confirm | After canonical candidate/revision/content-hash and source-revocation checks, append lifecycle event and immutable audit, then atomically derive candidate row, term tags and scope coverage state before commit. Any derivation failure aborts the confirmation in accelerated-required mode; otherwise it marks scope fallback-required. |
| review quarantine / reject | Delete/withhold candidate row and every term tag. Reject's existing payload erasure also removes its derivatives before commit. |
| supersede, retract, expire, expire_due | Invalidate sealed checkpoints as v6 does, then delete the active candidate row/tags in the same transition transaction. |
| future content-changing transition | Increment revision first; replace candidate row/tags only for the exact new `(revision, content_hash)`. Never update term tags in place across a content hash. |
| `forget(record)` | Remove candidate row, term tags, accounting metadata and coverage reference before plaintext/source payload deletion commits. The erasure receipt may report derivative counts but no values. |
| `forget_by_source` | Insert source-revocation tombstone and remove all affected projections/terms transactionally with every affected record. No post-commit reindex is permitted. |
| hard erase | Use the same narrowly controlled immutability-guard suspension as canonical hard erase, delete every projection derivative and related checkpoint selection, then assert no orphan remains before commit. |

The projection must have foreign keys/unique constraints where each backend can
enforce them, plus setup-time conformance checks which catch data written while
foreign-key/trigger protections were disabled. Its own triggers must never
copy plaintext into events, receipts, or telemetry.

Writes must derive HMAC tags before entering a database callback/lock when
possible, but bind them to the candidate's canonical revision/content hash
inside the transaction. Host callbacks, token counters, models, and network
calls remain forbidden while the lock is held.

## 8. Integrity, data sensitivity, and key handling

### 8.1 Binding contract

Each usable candidate row must be bound to all of these values:

```text
scope + record_id + record_revision + content_hash + kind + origin
+ current lifecycle state/trust/validity fields
+ immutable allow-audit event ID/candidate revision/origin/policy receipt
+ paired lifecycle event revision/reason/command hash
+ lexer + renderer + owned-counter + index-key generation
+ derived accounting metadata and term-set digest
```

At a minimum, the reader proves the canonical values through joins and validates
the immutable audit/event exactly as v6 does. The implementation should also
use a host-keyed projection-binding MAC over this content-free tuple so a
projection row cannot be treated as authoritative merely because it looks
well-formed. The key is separate from any checkpoint HMAC secret by domain
separation and is never persisted in SQLite/PostgreSQL, exports, receipts, or
logs.

External raw database writes are already outside the normal trusted Ledger
command boundary. They must result in conformance/integrity failure and
fallback or fail-closed behavior; SRCP may not make such tampering an
opportunity to surface a record that v6 would reject.

### 8.2 Sensitive data rules

- Plaintext remains only in canonical payload storage; the projection stores no
  plaintext, source/evidence reference, rendered JSON, task, or embedding.
- HMAC term tags, token/byte sizes, timestamps, record IDs and content hashes
  are sensitive operational metadata. They are excluded from public
  `explain()`, default export, telemetry and error messages.
- `export(include_content=False)` must exclude internal projection rows. A
  backup includes them only as sensitive derived storage; restoring a canonical
  backup without them must put scopes into `fallback_required` until a verified
  rebuild finishes.
- Key rotation creates a new generation. Until it reaches complete canonical
  coverage, the old compatible generation stays active or the scope uses v6;
  the system never mixes key generations in one plan. Retirement securely
  deletes old tags in the same controlled maintenance transaction.
- Loss of the projection key never grants access to plaintext or bypasses
  lifecycle checks. It disables acceleration and requires v6 fallback/rebuild
  once the host supplies a new key.

## 9. SQLite/PostgreSQL parity

The logical contract is shared, but the implementation should use each
backend's native transactional semantics:

| Topic | SQLite | PostgreSQL | Shared requirement |
| --- | --- | --- | --- |
| plan snapshot | Read transaction | `REPEATABLE READ READ ONLY` | Candidate coverage and audit joins observe one canonical snapshot. |
| final resolve | `BEGIN IMMEDIATE` | existing transaction-scoped advisory lock | Same selected markers either validate together or fail stale. |
| writes | existing serialized write transaction | existing advisory-locked write transaction | Canonical lifecycle + projection mutation commit/rollback together. |
| terms | BLOB HMAC tag + bounded `IN` batches | `bytea` HMAC tag + bounded array/parameter batches | Same HMAC bytes, lexer, sort and decision output. |
| setup | explicit v6→v7 migration | explicit v7 deployment/migration contract | No backend is labelled conformant without complete projection coverage. |
| backup | `backup()` file copy | operator-managed `pg_dump`/platform backup | Restore/rebuild and erasure behavior are tested separately. |

PostgreSQL cannot receive a paper parity claim merely because its inherited
Python methods compile. The integration suite needs a disposable real
PostgreSQL DSN and must execute the v7 conformance runner, including advisory
lock contention and database-level constraint/trigger checks.

## 10. Migration, backup, and rollback

SRCP-v7 is a schema migration because term tags and coverage state are durable.
The migration must be explicit and observable.

1. `dry_run_setup()` reports source/target schema, projection key/lexer
   prerequisites, expected rebuild work, and whether the scope will remain
   fallback-only. It does not write or expose payloads.
2. SQLite takes an operator-selected file-copy backup before an in-place
   v6→v7 upgrade. DDL, candidate rows, term tags, indexes and scope state are
   created atomically where practical; a failed transaction leaves v6 usable.
   Where a large rebuild cannot fit one transaction, the scope is marked
   `building` and the planner remains strict-v6 until one final transaction
   changes it to `ready` after coverage validation.
3. PostgreSQL has a reviewed operator runbook: database/schema backup,
   migration in a maintenance window or controlled staged build, verification,
   and restore/PITR fallback. If the initial PostgreSQL release continues to
   support only fresh schemas, its capability receipt must say so and it cannot
   satisfy the v7 migration conformance gate for existing data.
4. Rollback means restoring the verified v6/managed backup or disabling the
   projection and using v6. It is not a destructive down-migration that edits
   canonical events or payloads.
5. A rebuilt projection must produce the same strict plans as the pre-backup
   canonical ledger. It may be discarded and rebuilt; it may never be used as
   the recovery source for canonical records.

The future storage capability receipt should distinguish schema `7` and expose
the real setup/backup mode for each backend. Do not silently change the
existing v6 receipt in place.

## 11. Third-party token-counter behavior

Only the exact owned `RegexTokenCounter` can use precomputed per-record
packing data. Its renderer and punctuation behavior are versioned; after
projected packing, the planner still runs one exact full-envelope reconciliation
before sealing a plan.

Every other `TokenCounter`, including a wrapper delegating to regex, uses the
current conservative v6 path: fetch whole candidate payloads as needed,
render the entire prospective envelope for every selection decision, and
apply the existing non-monotonic-token safeguards. It receives no timing
claim from this RFC. A third-party counter must not be marked compatible based
only on a class name, sample count, or a reported `counter_id`.

This intentionally preserves correctness when a tokenizer merges or splits
tokens across JSON boundaries. A future counter-acceleration protocol would
need its own deterministic equivalence proof and versioned renderer contract.

## 12. Acceptance test matrix

No implementation is accepted based on microbenchmarks alone. The following
tests must exist and run against both backends where applicable.

### Semantic equivalence

1. Seeded corpus differential suite: for every supported strict policy and
   fixed clock, compare v6 and SRCP plan selection markers, core
   `LedgerRecallPlan.explain()` fields/decisions, selected rendered context,
   used tokens/bytes, and errors. Allow only an explicitly separate
   content-free execution-mode receipt to differ.
2. Cover empty task-term sets, ASCII, Cyrillic, CJK, emoji, Turkish/Unicode
   casefold boundaries, delimiter escaping, repeated terms, no match, equal
   score ties, all policy exclusion reasons, scan-byte budget, token budget,
   byte budget, candidate limit, and active-window limit.
3. Regression tests prove the exact current semantics that candidate limit is
   applied in active-window order before ranking and that third-party counters
   take the conservative path.
4. Property/fuzz suite generates valid record states, policies and task text;
   it compares SRCP to v6 or demands named fallback, never a different answer.

### Audit, lifecycle, and race safety

5. Tamper fixtures cover missing/mismatched audit, event revision/action/reason
   mismatch, origin mismatch, stale candidate revision/content hash,
   projection duplicate/missing/stale row, wrong key/lexer/renderer,
   malformed tag, and projection term index corruption. Each yields strict-v6
   fallback or a fail-closed `LedgerStateError`; none produces a context.
6. Fault-injection tests interrupt every lifecycle/projection write between
   canonical record change, audit insertion, candidate row, term rows,
   checkpoint invalidation and coverage state. After rollback/restart there is
   either no canonical change or a complete valid projection; no half-ready
   scope is usable.
7. Concurrent plan/confirm/supersede/retract/expire/forget/forget-by-source/
   erase tests prove the existing final resolve behavior, including a newer
   record pushing a selection out of its bounded top-N window.
8. Hard erase/forget tests assert zero remaining candidate/term/state rows for
   the erased record and zero term rows for every source-revoked record. They
   also assert no derived value appears in export, receipt, event or error.

### Storage, migration, and restart

9. The common storage conformance runner gains named v7 checks and runs the
   exact same scenario set for SQLite and a real disposable PostgreSQL
   database. PostgreSQL's run may not be replaced by collection-only evidence.
10. SQLite v6 fixture → backup → dry run → v7 upgrade → restart → plan/resolve
    equivalence → forget/erase → backup restore test passes. The restored v6
    file remains usable through the documented fallback/upgrade path.
11. PostgreSQL test exercises the documented backup/restore or staged migration
    procedure in a disposable database/schema, including restart and an
    incomplete-build fallback. If operator-managed restore cannot be tested,
    the v7 PostgreSQL release gate remains open.
12. Key-rotation and missing-key tests prove no mixed generation plan and no
    plaintext/key disclosure in diagnostics.

### Query and performance discipline

13. SQLite trace tests set an explicit upper bound on plan query count at 10k,
    prohibit one query per record/relation/audit/payload, and prove that the
    only full payload reads are selected-ID materialization and normal resolve.
    PostgreSQL has equivalent instrumentation/assertions.
14. The existing raw protocol runs unchanged in its declared configuration:
    10,000 persisted/active/eligible/candidates/scanned records, no candidate
    truncation, `token_budget=2048`, `byte_budget=32768`, 5 discarded warmups,
    30 retained plan/resolve pairs, no embedding/provider/network/remote DB.
15. On an operator-verified, exact-commit reference manifest, planning p95 is
    `<= 50 ms` with `execution_mode=strict_projection_v7`; resolve and
    end-to-end p50/p95 are reported but do not substitute for the planning
    gate. Keep raw JSON, manifest and source revision together.
16. Repeat the eligible reference run for two release cycles with no semantic
    regression. Verification-only, dirty-worktree, partial-coverage, cold,
    throughput, or RSS numbers may be reported as diagnostics but cannot be
    relabelled as the roadmap result.

## 13. Implementation sequence

1. Write a small storage-agnostic logical projection specification and a
   differential reference implementation that calls v6 terms/scoring/packing.
   Do not switch the planner yet.
2. Add SQLite schema v7, explicit fallback state, transactional lifecycle
   hooks, and fault-injection tests. Run the equivalence suite before timing.
3. Add the PostgreSQL implementation and real integration suite; fix parity
   gaps before optimising either backend.
4. Add owned-regex projected packing with full-envelope reconciliation, then
   query-budget checks and the raw reference protocol.
5. Only after all gates pass, propose a separate public API/storage-receipt
   update and release decision.

This ordering is intentional: the target is a faster proof-preserving read
model, not a shortcut around the Ledger's trust boundary.
