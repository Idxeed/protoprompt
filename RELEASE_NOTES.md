# protoprompt 0.19.0

ProtoPrompt 0.19.0 closes the local PostgreSQL fault-recovery and bounded
multiwriter evidence gate on the road to 1.0. The runtime contract remains the
same as 0.18.0; this release adds executable proof around ambiguous connection
loss, whole-command retry, exact-scope purge, and independent writers.

This is still an alpha release. PostgreSQL Ledger, task resume, `MemoryPolicy`,
and storage-conformance APIs remain experimental until the 1.0 public API
freeze and the remaining deployment/quality gates are complete.

## Real connection-abort recovery

The new live integration matrix runs against a disposable PostgreSQL 17
service and terminates the worker's actual server backend at two points inside
`MemoryWriter.purge_payloads()`:

- after canonical payload deletion has started;
- after the aggregate purge receipt has been inserted but before commit.

A fresh client connection must then observe the exact pre-command state: both
records remain active at the original revisions, payload/source sidecars and
events remain coherent, and no aggregate receipt is visible. Repeating the
same immutable host command must complete exactly once.

A separate worker exits after the transaction committed but before returning
its result. The new connection must replay the durable content-free receipt
without appending events or applying deletion again.

## Bounded independent-writer evidence

Two synchronized process waves open independent PostgreSQL connections against
one dedicated Ledger schema:

- three duplicate proposals, one independent proposal, and one proposal using
  the same opaque identifiers in a sibling scope;
- four duplicate scope purges in one scope and two duplicate purges using the
  same operation ID in a sibling scope.

The expected durable result is exact: one event per idempotent command, no
cross-scope collision, one sealed purge receipt per exact scope, successful
fresh-connection reopen, no lingering advisory lock, and survival of a later
record when an old purge receipt is replayed.

These are bounded correctness cases, not a throughput or latency benchmark.
Schema-wide writes remain intentionally serialized by a transaction-scoped
PostgreSQL advisory lock.

## Release-gate integration

The recovery/concurrency file is now:

- executed by the normal PostgreSQL/Redis CI integration job;
- executed explicitly by the tag-triggered publication workflow;
- required to be present in the core source distribution.

The existing catalog, guard-tamper, lifecycle, property, deletion, checkpoint,
storage-conformance, and frozen SQLite/PostgreSQL semantic parity checks remain
mandatory.

## Install

```bash
python -m pip install "protoprompt==0.19.0"
python -m pip install "protoprompt[documents,fastapi,ollama]==0.19.0"
python -m pip install "git+https://github.com/Idxeed/protoprompt.git@v0.19.0#subdirectory=apps/ollama-chat"
python -m pip install "https://github.com/Idxeed/protoprompt/releases/download/v0.19.0/protoprompt_cli-0.19.0-py3-none-any.whl"
```

`protoprompt-cli` is distributed as checksum-verified GitHub Release
wheel/sdist assets, not as a separate PyPI project. The local Ollama/PDF app
remains source-only and is built and tested by the same release workflow.

## Verification

Before the version cut, the exact recovery branch passed:

- live PostgreSQL Ledger matrix: 23 passed, one environment-specific
  non-deterministic-collation skip;
- new recovery/concurrency file alone: 5 passed on Windows and in GitHub's
  Ubuntu integration job;
- GitHub Actions branch run `34725995940`: every required Python 3.11/3.12/3.13,
  Windows CLI, Ollama app, package, docs, benchmark, and PostgreSQL/Redis job
  succeeded.

The final tag workflow repeats the complete deterministic core/app suites,
PostgreSQL recovery matrix, frozen benchmarks v0.1 through v1.0, strict RU/EN
documentation, wheel/sdist metadata checks, and clean-install smoke tests. It
publishes only artifacts derived from that verified tag and reconciles PyPI
SHA-256 digests before creating the GitHub Release.

## Explicit boundaries

0.19.0 proves logical transaction rollback/retry on the tested disposable
server. It does not prove database-server crash recovery, managed PostgreSQL
restore/PITR, WAL or replica erasure, physical-media deletion, distributed
exactly-once execution, or high-throughput writes.

The product is not a workflow engine, agent checkpoint, tool-authority system,
network service, automatic memory extractor, or infinite-memory claim. See
[SECURITY_REVIEW-v0.19.0.md](SECURITY_REVIEW-v0.19.0.md) and
[ROADMAP.md](ROADMAP.md).
