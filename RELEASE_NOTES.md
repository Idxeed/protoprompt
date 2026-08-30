# protoprompt 0.14.0

ProtoPrompt 0.14 is an RC-hardening release. It adds no production feature or
public API surface. Instead, it makes the existing Ledger v6 lifecycle and
strict-recall contracts harder to regress through deterministic generated
conformance tests on both SQLite and PostgreSQL.

## Highlights

- **Bounded lifecycle state machine** — the `dev` extra now includes
  Hypothesis. A deterministic SQLite property gate runs 20 generated examples
  with 12 command steps each over `MemoryWriter`'s public host contract:
  propose, confirm, quarantine, expiry, retraction, supersession, exact
  idempotent retries, stale/invalid transition atomicity, `forget`, controlled
  hard erase, source revocation, and re-ingest rejection.
- **Scope is tested as a generated property** — peer writers use equal logical
  record IDs and source references while one scope forgets or erases data.
  The other scope must retain its own projection, payload, and event history.
- **Exact recall packing is generated too** — strict document admission goes
  through `MemoryReviewGate`; generated payloads and token/byte slack verify
  whole-record packing, `plan()`/`resolve()` receipt reconciliation, and the
  absence of every payload from public `explain()` outputs.
- **PostgreSQL parity** — the same backend-neutral properties run in the live
  PostgreSQL integration lane on fresh disposable schemas. This is semantic
  conformance, not a throughput or latency claim.

## Compatibility

There are no required application-code or data migrations in 0.14.0. The core
still has zero required third-party runtime dependencies; Hypothesis is only
part of the development extra used by the release/CI test gate. Existing
SQLite and optional fresh-v6 PostgreSQL Ledger deployments retain their
contracts and storage boundaries from 0.13.

The local Ollama/PDF reference app is released in lockstep:

```bash
pip install "protoprompt[documents,fastapi,ollama]==0.14.0"
pip install "git+https://github.com/Idxeed/protoprompt.git@v0.14.0#subdirectory=apps/ollama-chat"
pp-ollama-chat
```

## Evidence and release gate

The release gate retains the prior deterministic core, reference-app, frozen
semantic benchmark, strict RU/EN documentation, distribution, and clean-wheel
checks. It additionally executes the generated SQLite property tests and a
live PostgreSQL 17 Ledger suite with eighteen integration cases:

- backend-neutral lifecycle/admission/strict-recall/checkpoint conformance;
- generated scope/deletion/source-revocation and lifecycle-state properties;
- generated strict token/UTF-8-byte recall-packing and content-free-receipt
  properties;
- explicit setup, restart/contention, catalog/tamper/RLS/rule/inheritance/
  sequence/GUC boundaries.

The local reference PG17 run completed **17 passed, 1 conditionally skipped**:
the skipped negative case requires an installed non-deterministic PostgreSQL
collation. The suite never treats that missing server fixture as a pass.

Run the focused checks from a development checkout:

```bash
python -m pytest -q tests/test_ledger_property_conformance_sqlite.py \
  tests/test_ledger_recall_property_sqlite.py

PROTOPROMPT_POSTGRES_DSN=postgresql://protoprompt:protoprompt@localhost:55432/protoprompt_test \
  python -m pytest -q tests/integration/test_postgres_memory_ledger.py -m integration
```

## Explicit boundaries

These generated tests are a bounded semantic regression gate. They do **not**
measure model quality, recall quality against an external framework, latency,
throughput, a 10k-record performance target, prompt-injection immunity, or
agent/workflow recovery. The forthcoming v1 evidence package will use a
separate frozen benchmark protocol, raw measurement provenance, and reference
hardware before it makes any such quantitative claim.
