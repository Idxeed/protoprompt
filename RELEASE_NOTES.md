# protoprompt 0.18.0

ProtoPrompt 0.18.0 hardens the path from durable task memory to one bounded
provider request. It adds a provider-safe task projection, an explicit local
Ollama/PDF demonstration, v1-candidate policy and storage-conformance receipts,
non-destructive v0.6 cutover evidence, and SQLite crash/concurrency coverage.

This remains an alpha release. The new Ledger, task-resume, policy, and
conformance APIs are experimental until the 1.0 freeze is complete.

## Provider-safe task resume

`TaskResumePlanner.compose_checkpoint()` now returns an opaque
`TaskResumeReferenceRequest`. The host validates the selected raw
`TaskEpisode`, then reduces it to a fixed `TaskEpisodeReference` containing
only:

- goal;
- aggregate completed-action count;
- outcome;
- next action;
- lesson.

Raw task/action references, descriptor, checkpoint, scope, record/provenance
identifiers, and secrets are structurally absent from the provider lane.
Builder and recall planner must use the same exact token-counter instance, and
the selected lifecycle/payload is revalidated before composition.

## Local Ollama and PDF demonstration

The source-only `protoprompt-ollama-chat` reference app can opt into a private
host-seeded task-resume mode. It keeps normal PDF RAG live while binding one
conversation to one reviewed host assertion through an HMAC-authenticated
mapping stored separately from the Ledger.

The mode is deliberately local:

- browser and provider endpoints must be loopback;
- the browser cannot create or alter task bindings;
- model context is capped at 2048 tokens with an explicit output reserve;
- generation is serialized through one in-process queue;
- ordinary transcript/PDF/model text is never auto-admitted as task memory;
- deletion makes the binding non-resumable before Ledger cleanup.

See `docs/en/ollama-task-resume-demo.md` and
`docs/ru/ollama-task-resume-demo.md`.

## Memory policy and storage evidence

- `MemoryPolicy` immutably pairs explicit admission and recall rules. It
  rejects a recall configuration that is weaker than its paired admission
  boundary and exposes a content-free receipt.
- SQLite v7 and fresh-schema PostgreSQL v7 expose one named strict-host
  storage-conformance profile with a sealed, content-free report.
- `MemoryWriter.purge_payloads(operation_id)` performs exact-scope canonical
  payload deletion across every payload-bearing lifecycle state and replays a
  durable aggregate receipt after restart.
- SQLite process-death and bounded multi-process matrices cover observe,
  lifecycle transition, source revocation, hard erase, checkpoint
  invalidation, scope purge, idempotent retry, and sibling-scope isolation.

These checks do not claim managed PostgreSQL recovery, physical WAL/backup
erasure, or a general storage-plugin contract. PostgreSQL recovery/concurrency
evidence remains a 1.0 release gate.

## Migration and evaluation protocols

- A frozen v0.6.1 SQLite fixture proves non-destructive cutover: legacy
  vector/session/profile bytes remain unchanged, no record is auto-imported or
  admitted, and rollback selects the preserved source rather than attempting a
  destructive schema downgrade.
- Frozen task-resume projection benchmark v0.5 adds three cases and fifteen
  semantic checks for identifier omission, receipt integrity, and binding or
  lifecycle rejection.
- The versioned v1.0 dual-backend semantic fixture remains exact across SQLite
  and PostgreSQL.
- Raw 10k performance and held-out quality/conflict protocols are included as
  strict evidence scaffolds. They do not establish a public performance or
  model-quality claim.

## Install

```bash
python -m pip install "protoprompt==0.18.0"
python -m pip install "protoprompt[documents,fastapi,ollama]==0.18.0"
python -m pip install "git+https://github.com/Idxeed/protoprompt.git@v0.18.0#subdirectory=apps/ollama-chat"
python -m pip install "https://github.com/Idxeed/protoprompt/releases/download/v0.18.0/protoprompt_cli-0.18.0-py3-none-any.whl"
```

`protoprompt-cli` is distributed as verified GitHub Release wheel/sdist assets,
not as a separate PyPI project. The Ollama app remains source-only and is built
and tested by the same release workflow.

## Verification

The release candidate was checked locally on Python 3.12 from a clean Git
archive:

- core non-integration suite: 766 passed, 3 skipped, 27 deselected;
- deterministic agent CLI and Ollama reference-app suites: 321 passed,
  50 platform skips, 10 integration tests deselected;
- PostgreSQL integration suite: 18 passed, 1 environment-specific collation
  skip;
- frozen benchmarks v0.1 through v0.5 and v1.0 dual-backend parity verified;
- strict Russian and English documentation builds succeeded;
- core wheel/sdist, CLI wheel/sdist, and Ollama app wheel passed `twine check`.

The tag-triggered workflow repeats the release checks on Python 3.12 with a
fresh PostgreSQL service, verifies package/version alignment, publishes the
core artifacts to PyPI via OIDC, checks their SHA-256 digests against PyPI,
and creates a GitHub Release from the same verified artifacts.

## Explicit boundaries

0.18.0 is not a workflow engine, agent checkpoint, tool-authority system,
network service, automatic memory extractor, or infinite-memory claim. A safe
projection is data, not trusted instruction. Operators remain responsible for
filesystem protection, deployment secrets, backup/PITR, external indexes and
provider copies, and the threat model of any non-local deployment.

See [SECURITY_REVIEW-v0.18.0.md](SECURITY_REVIEW-v0.18.0.md) and
[ROADMAP.md](ROADMAP.md).
