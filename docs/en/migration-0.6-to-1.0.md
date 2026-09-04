# Migrating a 0.6 deployment to the 1.0 Ledger line

This is a **non-destructive cutover**, not an in-place import. `v0.6.1` had
SQLite vector/session stores and profile stores, but no Memory Ledger. Their
contents are legacy application data, not implicitly admitted `MemoryRecord`
rows.

The current Ledger is still experimental until the 1.0 RC exit gate is met.
This guide documents the verified safe direction of travel; it does not promise
that an old profile, summary, PDF, or model output is safe to recall as a
modern fact without host review.

## What the cutover preserves

```text
v0.6 SQLite source (read-only) ── snapshot + backup ──> retained rollback source
                                      |
                                      +──> new, separate Ledger SQLite file
                                                  |
                                                  +──> explicit host review and re-ingest
```

The new Ledger does not import the `chunks` table, profile JSON, or legacy
session summaries. It must be created in a separate SQLite file. Existing
application readers remain selectable until the rollback window ends.

## SQLite procedure

1. Stop or quiesce writes to the v0.6 data file and make an OS/database backup.
   Keep the original source read-only for the rollback window. Protect it as
   it may contain prompt, profile, document, or transcript content.
2. Record a content and catalog snapshot of the legacy file. At minimum retain
   a SHA-256 digest, row counts for `chunks` and `profiles`, and the intended
   v0.6 package pin.
3. Create a **different** Ledger path and run the explicit migration job:

   ```python
   from protoprompt.ledger import SqliteMemoryLedger

   ledger = SqliteMemoryLedger("/private/protoprompt-ledger-v1.db")
   print(ledger.dry_run_setup())  # inspect before writing
   ledger.setup()
   ledger.close()
   ```

   Importing `protoprompt` or constructing an ordinary vector/profile reader
   does not perform this operation.
4. Re-open the retained v0.6 source with its existing readers and compare the
   snapshot. The Ledger target must contain no records until trusted host code
   explicitly creates, reviews, and confirms them.
5. Introduce new Ledger-backed behavior behind a host-owned scope and a
   concrete admission policy. Do not bulk-promote profile facts, session
   summaries, PDF text, or model output merely because it exists in the old
   store.

The repository gate `tests/test_migration_from_v0_6.py` materializes the exact
published v0.6.1 `chunks` and `profiles` table shapes, creates a separate v7
Ledger, and proves byte-for-byte/source-catalog preservation. It also proves
that a copied rollback source remains readable by current vector and profile
readers.

## Rollback

Stop writes to the new Ledger target, switch the application back to the
retained v0.6 package/configuration and source database, then investigate the
separate Ledger copy. Do **not** try to downgrade the Ledger schema in place or
copy Ledger tables into the legacy source database. A rollback is source
selection, not schema reversal.

## PostgreSQL cutover

`PostgresMemoryLedger` supports a fresh, dedicated Ledger schema; it is not an
automatic upgrader for an application's v0.6 tables. Take and test a platform
backup (`pg_dump` or managed-database restore), create a separate empty Ledger
schema through the explicit migration role, and keep the prior application
schema read-only while shadow-reading or canarying the new path. Restoring the
old schema/configuration is the rollback; no destructive downgrade is offered.

## Boundaries

The v0.6 release did not include the current Ollama/PDF reference application,
so there is no app-database upgrade claim for that product. This guide proves
the legacy core SQLite cutover only. A production migration still needs its own
retention, backup, authorization, and explicit re-ingestion plan.
