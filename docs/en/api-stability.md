# ProtoPrompt v1 API boundary

ProtoPrompt `0.20` introduces `protoprompt.api`: the narrow import seam being
frozen for the `1.x` line. In pre-1.0 releases its status is **v1 candidate**.
At `1.0.0`, this same boundary becomes the SemVer-stable core after the
remaining release gates pass.

Historical imports keep working. They do not silently acquire the stronger
`1.x` compatibility promise.

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

## What is frozen

The packaged `api_contract_v1.json` manifest is executable release evidence,
not a generated API dump. It freezes:

- the exact names exported by `protoprompt.api`;
- the public fields and read methods of `ContextPlan`, `MemoryRecord`,
  `MemoryEvent`, receipts, and relations;
- the public `MemoryWriter` lifecycle, read, export, and scoped-erasure
  methods;
- `MemoryPolicy.safe_default()`, its fingerprint and content-free explanation;
- enum values for lifecycle, trust, provenance, relation, and built-in storage
  modes;
- the operational setup/close boundary for the built-in SQLite and PostgreSQL
  ledgers, plus the sealed storage-conformance receipt.

`ContextPlan`, `MemoryRecord`, `MemoryEvent`, audit objects, and erasure
receipts are result types. Their documented public fields and readers are the
contract; applications should not construct them directly. Private fields
whose names start with `_` are excluded.

`MemoryPolicy.safe_default()` is the candidate stable policy entry point.
Directly composing custom admission and recall policy components remains
experimental until those policy languages receive their own freeze.

## What remains experimental

The following do not enter the stable line merely because existing imports
continue to work:

| Surface | Status |
|---|---|
| `protoprompt.ledger.admission` custom policies and review workflow | experimental |
| `protoprompt.ledger.recall` planner, composer, and checkpoints | experimental |
| task-resume formats and planner | experimental |
| `protoprompt.agent` lineage / working-memory research | experimental |
| provider and framework integrations | optional adapters, independently versioned behavior |
| `apps/*` | reference applications and demos |
| third-party Ledger backends | unsupported; there is no public backend plugin protocol |

The built-in PostgreSQL class is in the candidate API, but its
`operator_managed` backup mode remains an explicit operational obligation.
The API freeze does not claim that managed backup/restore, PITR, replicas,
WAL retention, or physical-media erasure have been proven.

## Compatibility policy

Before `1.0`, a candidate-boundary change requires an explicit changelog entry
and an intentional update of the contract tests and manifest hash. From
`1.0.0` through `1.x`, documented names and meanings remain compatible unless
a security repair requires a fail-closed change. Additive methods and optional
fields may be introduced when existing callers keep working.

Exceptions, validation failures, scope isolation, trust transitions, and
deletion semantics are behavior—not implementation detail. Private methods,
database internals, exact exception messages, object `repr`, and direct
constructors for result-only types are not frozen.

## Inspect the contract

The manifest is installed in wheels and sdists and can be read without opening
storage, loading credentials, making network calls, or importing an optional
provider SDK:

```python
from protoprompt.api import V1_API_MANIFEST_SHA256, v1_api_manifest

manifest = v1_api_manifest()
assert manifest["status"] == "v1_candidate"
print(V1_API_MANIFEST_SHA256)
print([item["name"] for item in manifest["exports"]])
```

CI checks the exact export order, implementation identity, public dataclass
fields, public members, enum spellings, experimental exclusions, manifest
digest, package inclusion, and optional-dependency-free import.
