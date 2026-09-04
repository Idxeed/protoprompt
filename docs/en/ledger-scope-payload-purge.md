# Exact-scope payload purge (v1 candidate)

`MemoryWriter.payload_readback()` and `MemoryWriter.purge_payloads()` are an
experimental, host-only deletion contract for a single, already pinned
`MemoryScope`. They are not a stable 1.0 guarantee; a release may claim the
contract only with its own documented local and backend verification evidence.

Use this operation when the host must remove the live Ledger payload for one
client/context boundary without discovering record IDs first. It is deliberately
separate from a broad database, backup, conversation, or provider deletion
claim.

## Narrow public surface

The scope is fixed when the trusted host constructs the writer; neither method
accepts a tenant, user, thread, or arbitrary scope from a model or plugin.

```python
# ``writer`` was constructed by trusted host code for exactly one MemoryScope.
before = writer.payload_readback()

receipt = writer.purge_payloads(
    "deletion-request:opaque-host-id",
    reason_code="scope_payload_purged",  # the default
)

assert receipt.readback.is_empty
assert writer.payload_readback().payload_record_count == 0
```

`payload_readback()` returns a content-free `ScopePayloadReadback`: an opaque,
deterministic scope fingerprint and the number of currently payload-bearing
records in that exact scope. It does **not** enumerate record IDs, sources,
evidence, content hashes, plaintext, or raw scope fields.

`purge_payloads(operation_id, *, reason_code="scope_payload_purged")` returns
a content-free `ScopePayloadPurgeReceipt`. The receipt contains only the
host-minted opaque operation ID, aggregate deletion counts, the same opaque
scope fingerprint, and its final readback. It has no memory content or source
references.

`operation_id` is a durable retry key, not a user-visible identifier. Mint it
in trusted host code, persist it beside the host's deletion request, and reuse
the **same** value after an ambiguous timeout, process death, or restart. A
committed operation is reconciled through its content-free durable receipt; a
caller must not mint a second ID merely because it did not observe the first
response. The same operation ID in a different scope belongs to a different
operation namespace: the Ledger neither merges nor cross-reads it.
Within the same exact writer scope, reusing it for a different command is a
conflict. Hosts should still mint globally unique IDs so their own deletion
request log cannot confuse two scopes.

## What is removed, atomically

Within one Ledger write transaction, the operation finds every currently
payload-bearing record in the writer's exact scope. It covers records in all
logical lifecycle states:

| State | Included when a payload remains? |
|---|---|
| `candidate` | yes |
| `active` | yes |
| `superseded` | yes |
| `retracted` | yes |
| `expired` | yes |
| `quarantined` | yes |

For each such record it applies the normal forgotten lifecycle path and removes
the local plaintext/content payload, source and evidence payload data, scoped
source lookup data, and relations. The content-free lifecycle/audit history
needed to prevent silent resurrection is intentionally not presented as a
payload export. A record whose payload has already been forgotten is not made
payload-bearing again.

The command commits only if its final exact-scope readback is empty. A failed
command or process death before the commit leaves the pre-command state
observable after reopen; it must never expose a successful aggregate receipt
for a partial purge. A successful receipt therefore has
`receipt.readback.payload_record_count == 0` at its transaction boundary.

This is a logical, live-Ledger operation—not `erase()` of every audit/event
row. `erase()` remains the explicit per-record hard-delete escape hatch with
its own semantics.

## Required host deletion fence

The Ledger serializes its own mutation, but it does not own your application's
identity system, queues, browser sessions, or ingress adapters. Before calling
the purge, the host must establish its own deletion/ingress fence for the
affected principal and scope:

1. stop or reject new candidate/admission writes for that scope;
2. drain or cancel in-flight host writes that could add a payload immediately
   after the Ledger transaction; and
3. persist the deletion request and operation ID, then reconcile the final
   receipt/readback before declaring success to the client.

Without that boundary, another trusted writer can create new payload after the
purge transaction commits. Such a new write is not a failure of the exact-scope
readback; it is an application-level race the host must close.

Do not expose `MemoryWriter`, `MemoryReviewGate`, a writer's ingress object,
or `operation_id` generation to a model, browser client, or untrusted in-process
plugin. The host, not an LLM, decides the scope and starts deletion.

## Explicit limits

The operation does **not** retroactively remove text already sent to a model,
saved in a provider request/response, rendered in a chat UI, or stored in your
application's conversation archive. Those systems require their own deletion
workflow.

It also removes only live canonical Ledger payload and local derived rows. It
does not claim physical erasure from SQLite WAL/journal files, PostgreSQL WAL,
replicas, snapshots, backups, disks, logs, or physical media. Apply encryption
and a backup-retention/key-destruction policy when that property is required.

The core Ledger has no external vector/FTS projection in this contract. If an
adapter writes to another index, it must perform and durably acknowledge the
matching deletion before the host claims complete end-to-end erasure.
