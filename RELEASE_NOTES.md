# protoprompt 0.20.0

ProtoPrompt 0.20.0 defines the narrow public API boundary intended to become
stable at 1.0. It preserves historical imports while giving applications one
machine-checked seam for explainable context plans, durable memory lifecycle,
scope, policy, and the two built-in storage backends.

This is still an alpha release. `protoprompt.api` is a **v1 candidate**, not a
claim that all remaining 1.0 deployment, security, quality, performance, and
independent-install gates are complete.

## One deliberate import seam

New 1.0-targeted code can import from `protoprompt.api`:

```python
from protoprompt.api import (
    MemoryKind,
    MemoryScope,
    MemoryWriter,
    SqliteMemoryLedger,
)

ledger = SqliteMemoryLedger("memory.db")
ledger.setup()
writer = MemoryWriter(
    ledger,
    scope=MemoryScope(tenant="acme", user="alice", thread="support"),
)
candidate = writer.assert_candidate(
    kind=MemoryKind.FACT,
    content="The approved support tier is enterprise.",
    source_ref="host:crm:account-42",
)
active = writer.confirm(candidate.record_id, expected_revision=candidate.revision)
```

The seam includes the core `ContextPlan` result family; `MemoryRecord`,
`MemoryEvent`, lifecycle enums and receipts; `MemoryScope`; `MemoryWriter`;
`MemoryPolicy.safe_default()`; Ledger errors; built-in SQLite/PostgreSQL
classes; and their sealed storage-capability descriptor.

## Executable contract

Every wheel and sdist contains `protoprompt/api_contract_v1.json`. Its canonical
SHA-256 is exposed as `V1_API_MANIFEST_SHA256`, and
`v1_api_manifest()` returns a detached JSON-safe copy without opening storage,
loading credentials, calling a network, or importing optional provider SDKs.

The contract tests freeze:

- exact exports and implementation identities;
- documented public fields and readers on result/value types;
- public `MemoryWriter` lifecycle, read, export, and scoped-erasure methods;
- lifecycle/provenance/trust/relation/storage enum values;
- the supported setup/close boundary of both built-in backends;
- the explicit list of experimental namespaces;
- canonical manifest digest and package inclusion.

Result objects such as `ContextPlan`, `MemoryRecord`, and `MemoryEvent` freeze
their public fields and readers, not their direct constructor signatures.
Private `_` names remain implementation details. For `MemoryPolicy`, only
`safe_default()`, fingerprint, and explanation enter this candidate boundary;
custom admission/recall policy composition remains experimental.

## PostgreSQL operational visibility

`PostgresMemoryLedger.dry_run_setup()`, `setup()`, and `schema_version()` are
now explicit class methods rather than dynamically delegated attributes. This
does not change their behavior; it makes the candidate storage boundary visible
to IDEs, type tools, documentation, and contract tests.

PostgreSQL backup mode remains `operator_managed`. This release does not claim
managed restore/PITR, replica or WAL guarantees, or physical-media erasure.

## Install

```bash
python -m pip install "protoprompt==0.20.0"
python -m pip install "protoprompt[documents,fastapi,ollama]==0.20.0"
python -m pip install "git+https://github.com/Idxeed/protoprompt.git@v0.20.0#subdirectory=apps/ollama-chat"
python -m pip install "https://github.com/Idxeed/protoprompt/releases/download/v0.20.0/protoprompt_cli-0.20.0-py3-none-any.whl"
```

`protoprompt-cli` is a checksum-verified GitHub Release wheel/sdist rather than
a separate PyPI project. The Ollama/PDF reference app remains source-only.

## Verification before the version cut

- exact contract branch core suite: `773 passed, 3 skipped, 32 deselected`;
- combined local PostgreSQL v7 and crash/concurrency suite: `23 passed, 1`
  environment-specific collation skip;
- deterministic agent CLI + Ollama application suites: `321 passed, 50`
  platform/optional skips, `10` integration deselected;
- frozen semantic memory benchmarks v0.1 through v0.5 verified;
- wheel/sdist inclusion and Twine metadata validation passed;
- strict Russian and English documentation builds passed;
- GitHub Actions branch run `34729177663` concluded `success` for commit
  `a6ddbe5ed819f61c2f6bd644cc1905a3558ca2dd` across Python 3.11–3.13,
  integration, package, docs, benchmark, Windows CLI, and Ollama jobs.

The final tag workflow repeats the full deterministic core/app suites, live
PostgreSQL matrix, frozen v0.1–v1.0 benchmarks, docs, package metadata, clean
install smokes, and artifact/PyPI digest reconciliation from the exact tag.

## Explicit boundaries

Existing `protoprompt`, `protoprompt.ledger`, and subpackage imports remain
available, but are not all covered by the future 1.x promise. Custom admission
and recall policy languages, checkpoint/task-resume workflows,
`protoprompt.agent`, provider/framework adapters, reference apps, and any
third-party Ledger backend remain experimental or separately scoped.

The manifest hash detects accidental drift; it is not a signature and does not
replace trusted package channels or release checksums. See
[SECURITY_REVIEW-v0.20.0.md](SECURITY_REVIEW-v0.20.0.md), the
[API stability guide](https://idxeed.github.io/protoprompt/en/api-stability/),
and [ROADMAP.md](ROADMAP.md).
