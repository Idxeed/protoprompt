# Memory policy contract (v1 candidate)

`MemoryPolicy` is the small, explicit contract that binds two different
durable-memory decisions:

1. `MemoryAdmissionPolicy` decides whether one concrete, host-owned ingress
   candidate may be confirmed.
2. `LedgerRecallPolicy` decides how already active records may be selected for
   one bounded recall lane.

Those phases are intentionally not aliases. Admission never performs recall;
recall never confirms a candidate. `MemoryPolicy` gives a host one versioned,
content-free object to review and pass to both explicit boundaries.

It is an additive **v1 API-freeze candidate**, not a workflow engine, model
policy, automatic ingestion feature, or a claim that every existing Ledger
adapter is already stable.

## Safe default

```python
from protoprompt.ledger import MemoryPolicy

policy = MemoryPolicy.safe_default()
assert policy.admission.allowed_origins == ("host_assertion",)
assert policy.recall.require_admission_audit is True
print(policy.explain())  # content-free policy receipt plus fingerprint
```

The default permits only reviewed `host_assertion` facts, decisions, and
preferences at confidence `0.75` or above. It does not create a record or
admit user, document, tool, or model text by itself.

## Explicit host integration

The host still owns the scope, ingress, review, and request boundary. Pass the
matching components explicitly rather than exposing the wrapper to a model or
browser client:

```python
from protoprompt import MemoryScope
from protoprompt.ledger import (
    MemoryPolicy,
    MemoryReviewGate,
    MemoryWriter,
    SqliteMemoryLedger,
)
from protoprompt.ledger.recall import LedgerRecallPlanner

ledger = SqliteMemoryLedger("ledger.db")
ledger.setup()
writer = MemoryWriter(
    ledger,
    scope=MemoryScope(tenant="local", user="alice", thread="chat-42"),
)
policy = MemoryPolicy.safe_default()

gate = MemoryReviewGate(
    writer,
    origin="host_assertion",
    policy=policy.admission,
)
planner = LedgerRecallPlanner(writer, policy=policy.recall)
```

For a custom policy, construct both components deliberately. The recall
component must be at least as restrictive as admission; otherwise
`MemoryPolicy(...)` fails during construction.

## Enforced relationship

`MemoryPolicy` rejects a pair unless all of these are true:

- recall requires immutable admission-audit evidence;
- recall declares concrete origins instead of the legacy unrestricted-origin
  compatibility lane, excluding `unknown` and `legacy_unknown`;
- recall origins and kinds are subsets of the admission origins and kinds;
- recall's minimum confidence is no lower than admission's minimum confidence.

This means a record selected through the combined contract could have passed
its paired admission rule. It is an API-shaping safety invariant, not a Python
sandbox or authorization system.

## Receipt and versioning

`policy.explain()` returns fresh JSON-safe metadata: the wrapper schema,
policy identity/version, both nested component receipts, and a deterministic
content-free fingerprint. It never contains memory text, scopes, record IDs,
task text, secrets, or provider messages.

`schema_version`, `policy_id`, and `policy_version` belong to the wrapper.
The nested admission and recall policies retain their own compatibility
versions. Persist an application's exact reviewed policy receipt next to its
deployment configuration; do not assume that matching human-readable names
mean matching semantics.

## Boundaries

- Existing standalone `MemoryAdmissionPolicy` and `LedgerRecallPolicy` uses
  remain supported as experimental compatibility APIs. They are not silently
  converted into `MemoryPolicy`.
- `MemoryPolicy` does not change `MemoryWriter`, auto-wire legacy vector or
  transcript storage, solve prompt injection, arbitrate conflicts, or send a
  provider request.
- The host must still protect its Ledger/database, scope assignment, review
  authority, and any checkpoint secrets. See the [Memory Ledger](memory-ledger.md)
  and [bounded Ledger recall](ledger-recall.md) guides for their separate
  lifecycle and request-boundary constraints.
