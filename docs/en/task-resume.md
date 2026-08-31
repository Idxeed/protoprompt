# Task-resume memory *(experimental)*

`TaskResumePlanner` is a deliberately narrow, host-only adapter for resuming
one task from durable Ledger memory. It builds on [bounded Ledger
recall](ledger-recall.md); it is not a workflow engine, an agent checkpoint,
or an Ollama integration.

The adapter selects only host-confirmed, typed `TaskEpisode` records in one
task-specific scope, seals that selection as a durable Ledger checkpoint, and
composes it into one bounded provider request. The host owns every capability
that can create, admit, bind, seal, or resume a task.

!!! warning "Experimental host boundary"

    Do not expose a Ledger, `MemoryWriter`, admission gate, planner,
    `task_ref`, descriptor, or checkpoint ID to a model tool or untrusted
    client. This API is for trusted application code.

## What the contract binds

The host mints an opaque `task_ref` (an identifier of at most 128 characters,
without whitespace) and derives the backend scope with:

```python
task_scope = task_resume_scope(parent_scope, task_ref=task_ref)
```

The derived scope keeps the parent tenant and user, has
`kind="task_resume"`, and includes both the parent's opaque correlation marker
and the task reference in its backend thread namespace. The same `task_ref`
under a different parent thread or kind therefore cannot cross-read or resume
this task. `parent_scope` must have a non-empty tenant and user.

The host must use that **exact** derived scope for both its
`MemoryWriter`/`LedgerRecallPlanner` and its `TokenBudgetedContextBuilder`.
`TaskResumePlanner` checks this at construction and rejects a mismatched or
widened boundary.

Each selected record must be a canonical JSON `TaskEpisode`:

- `task_ref`, `goal`, `completed_action_refs`, and `outcome` are required;
- `next_action` and `lesson` are optional bounded reference data; and
- decoding rejects malformed JSON, duplicate or unknown fields, an unsupported
  schema, a wrong payload kind, and a mismatched task reference.

`TaskProcedure` is available as a separate typed data object, but this adapter
does **not** select it. v0.17 has no procedure dependency graph, ordering, or
conflict-resolution semantics.

## Admission and selection policy

An episode enters this lane only after trusted host code creates a
`host_assertion` candidate with `asserted=True`, reviews it, and explicitly
confirms an `allow` decision. The strict recall policy is fixed to:

- `MemoryKind.EPISODE` only;
- `MemoryOrigin.HOST_ASSERTION` only;
- an immutable admission audit; and
- minimum confidence `0.75`.

The constructor rejects a recall policy that broadens any of those rules. An
origin label or a typed JSON string is not, by itself, an authority grant or
an admission decision.

## Minimal host integration

`ledger`, `store`, and `embedding_client` below are already configured,
host-owned objects. The host chooses and durably protects its checkpoint secret
and task mapping.

```python
from protoprompt import ContextInput
from protoprompt.injector_budgeted import TokenBudgetedContextBuilder
from protoprompt.ledger import (
    MemoryAdmissionPolicy,
    MemoryKind,
    MemoryOrigin,
    MemoryReviewGate,
    MemoryWriter,
    TaskEpisode,
    TaskOutcome,
    TaskResumePlanner,
    task_resume_scope,
)
from protoprompt.ledger.recall import LedgerRecallPlanner, LedgerRecallPolicy
from protoprompt.scope import MemoryScope
from protoprompt.tokens import RegexTokenCounter

parent_scope = MemoryScope(
    tenant="acme",
    user="alice",
    thread="chat:17",
    kind="chat",
)
task_ref = "task:deploy-42"                 # host-minted opaque identifier
descriptor = "Resume the checked deployment safely."
task_scope = task_resume_scope(parent_scope, task_ref=task_ref)

writer = MemoryWriter(ledger, scope=task_scope, actor="task-host")
episode = TaskEpisode(
    task_ref=task_ref,
    goal="Resume the checked deployment.",
    completed_action_refs=("action:prepare", "artifact:manifest"),
    outcome=TaskOutcome.INTERRUPTED,
    next_action="Check the host-owned deployment status.",
)
gate = MemoryReviewGate(
    writer,
    origin=MemoryOrigin.HOST_ASSERTION,
    policy=MemoryAdmissionPolicy(
        policy_id="task-resume-episodes-v1",
        policy_version="1",
        allowed_origins=(MemoryOrigin.HOST_ASSERTION,),
        allowed_kinds=(MemoryKind.EPISODE,),
        minimum_confidence=0.75,
    ),
)
candidate = gate.ingress(
    kind=MemoryKind.EPISODE,
    source_ref="host:deploy-42",
    evidence_refs=("artifact:manifest",),
    confidence=0.9,
    asserted=True,
).submit(episode.to_json())
gate.confirm(gate.review(candidate.record_id), event_id="admission:deploy-42")

counter = RegexTokenCounter()
recall = LedgerRecallPlanner(
    writer,
    policy=LedgerRecallPolicy.task_resume_safe_default(),
    counter=counter,
    checkpoint_secret=HOST_CHECKPOINT_SECRET,
)
builder = TokenBudgetedContextBuilder(
    store,
    embedding_client,
    counter=counter,
    max_tokens=8_000,
    scope=task_scope,
)
resume = TaskResumePlanner(
    builder,
    recall,
    parent_scope=parent_scope,
    task_ref=task_ref,
    task_descriptor=descriptor,
)

checkpoint = resume.seal_checkpoint(
    checkpoint_id="checkpoint:deploy-42:v1",
    token_budget=600,
    byte_budget=32_768,
)

# Persist this mapping in host-owned durable state, outside the Ledger receipt.
host_task_state = {
    "task_ref": task_ref,
    "descriptor": descriptor,
    "checkpoint_id": checkpoint.checkpoint_id,
}
```

The descriptor is fixed when the adapter is constructed. It is intentionally
not stored in the Ledger checkpoint, and neither `seal_checkpoint()` nor
`compose_checkpoint()` accepts a replacement descriptor.

On a later request, the host reconstructs the same parent scope, task scope,
policy, token-counter identity, checkpoint secret, and descriptor from its own
mapping. It then constructs the adapter again and composes the host-owned
checkpoint ID:

```python
request = await resume.compose_checkpoint(
    checkpoint_id=host_task_state["checkpoint_id"],
    inp=ContextInput(
        query="What does the current PDF say about the deployment window?",
        system_prompt="Follow the host's safety rules.",
        include_rag=True,
        include_session=False,
    ),
    user_message="Summarize the deployment window.",
)
messages = request.render_messages()  # send promptly through the host provider
```

The frozen `task_descriptor` is used only for the adapter's recall and
checkpoint-integrity work. `ContextInput.query` remains the current request's
query, including live RAG retrieval. A current question can therefore be about
a PDF without silently rebinding the checkpoint's task selection.

## Fresh validation and recovery

Each composition resolves the durable checkpoint through the public resume
path, verifies its continuation reference equals the adapter's `task_ref`,
re-decodes every selected record as a matching `TaskEpisode`, and performs the
final Ledger validation before returning the request. If a supporting record
was forgotten, retracted, superseded, expired, erased, or revised, the resume
fails closed. The host must assess the new state and seal a new checkpoint; it
must not send an older retained request after an erasure or lifecycle change.

`checkpoint_id` and `task_ref` are opaque host metadata. They are not a
client/model routing API, and a task descriptor cannot be recovered from the
checkpoint alone. The host's durable mapping is specifically:

```text
{ task_ref, descriptor, checkpoint_id }
```

## Receipts and data handling

`TaskResumePlanner.explain()`, checkpoint receipts, and composed-request
receipts are content-free. They do not disclose task references, descriptors,
scope, checkpoint identity, record IDs, source/evidence references, or Ledger
payloads. The transient `LedgerComposedRequest` itself necessarily contains
provider messages with the selected reference data; keep it host-side and send
it promptly rather than retaining it as durable state.

## Explicit non-goals

This experimental adapter provides no automatic wiring to the reference Ollama
app or any Ollama control plane. It also does not provide:

- automatic extraction, admission, confirmation, or task handoff;
- procedure execution, dependency/conflict semantics, or workflow planning;
- tool execution, authority, side effects, or exactly-once guarantees;
- a provider-conversation snapshot or a workflow/agent checkpoint; or
- infinite memory, an unlimited context window, or automatic long-term
  retention.

Treat episode text as untrusted reference data. The host, not the planner or
the model, remains responsible for every real action and for deciding whether a
new checkpoint is appropriate.
