# ProtoPrompt 0.18.0 — local internal security review

**Release-gate status:** local working-tree evidence only. This record is not
a release approval, external penetration test, security certification, or a
claim that 0.18.0 has been published.

This review covers the unpublished v0.18 task-resume demo delta relative to
the v0.17.0 tag. It was performed from the local source tree because external
code scanning and publication are out of scope for the current local-only
work mode.

## Result

No confirmed P0 or P1 issue was found under the stated local-demo threat
model. The new boundary is deliberately narrow:

- A host-owned seed is parsed strictly, canonicalized, bounded, and used only
  to create one `host_assertion` task episode. Browser chat input cannot mint
  a task reference, descriptor, admission decision, planner, or checkpoint.
- The raw `TaskEpisode` never becomes a provider message. The only supported
  provider lane is `TaskEpisodeReference`, a structural projection containing
  a goal, aggregate completed-action count, outcome, next action, and lesson;
  it omits raw task/action/checkpoint/descriptor/scope/provenance fields.
- `TaskResumeReferenceRequest` is an immutable opaque capability instead of
  a request-shaped raw payload. It only renders the safe projection and
  revalidates selected data before composition.
- The demo maps one local conversation to one task binding in a separate
  SQLite file. Its HMAC key is derived and retained outside that SQLite file;
  malformed, cross-conversation, stale, or lifecycle-invalid mappings fail
  closed.
- Deletion moves a binding to `closing` before Ledger cleanup. A cleanup
  failure leaves it non-resumable rather than silently restoring access.
- The demo remains loopback/local-Ollama only, has a fixed 2048-token context
  ceiling, serializes generation in one process, and does not make ordinary
  transcript archive or automatic model/PDF ingestion into Ledger task memory.

## Reviewed trust boundaries

| Boundary | Protection reviewed |
| --- | --- |
| Browser request to FastAPI demo | strict request/seed validation, loopback peer/host checks, local provider configuration |
| Host seed to task episode | bounded canonicalization and host-only `host_assertion` admission |
| Raw Ledger episode to provider | fixed safe reference projection and opaque reference request |
| `chat.db` binding to Ledger task scope | separate state store, HMAC integrity tag, exact derived scope, active/closing lifecycle |
| Concurrent compose/close/delete | lock release only around await followed by active-binding revalidation; close fails closed |
| SQLite files on the local machine | integrity validation and explicit lifecycle checks; filesystem protection remains operator responsibility |

## Local evidence

- Focused application task-resume host/parser/state suite: `18 passed` after
  malformed seed and conversation/reference validation hardening.
- Complete core non-integration suite at the v0.18 working-tree checkpoint:
  `669 passed, 3 skipped, 26 deselected`.
- Complete local Ollama/PDF reference-app suite at that checkpoint:
  `43 passed, 1 skipped`.
- Frozen offline memory benchmark protocol versions v0.1–v0.5 verified
  locally. These are contract checks, not model-quality or universal latency
  claims.
- English and Russian documentation builds succeeded with MkDocs strict mode;
  existing Material/MkDocs 2 compatibility advisories are not security test
  results.

## Remote CI evidence (2026-09-04)

The pushed feature branch was verified by GitHub Actions run
`33904938519` for commit `29813a911c5f7bbdf2f0e7a091858f66713b58c5`:
<https://github.com/Idxeed/protoprompt/actions/runs/33904938519>.
The run concluded `success` for every required job: Python 3.11/3.12/3.13,
Windows CLI, Ollama reference app, docs, lint, wheel/sdist package smoke,
offline memory benchmark, and PostgreSQL/Redis integration. The docs deploy
job was skipped because this is not `master`. This is branch-level CI evidence,
not a public-release approval or a substitute for owner review of the version
and tag.

Primary regression coverage lives in:

- `tests/test_ledger_task_resume.py`
- `tests/test_ledger_task_episode_reference.py`
- `tests/test_ledger_task_resume_projection_benchmark.py`
- `apps/ollama-chat/tests/test_task_resume_app.py`
- `apps/ollama-chat/tests/test_task_resume_demo_host.py`
- `apps/ollama-chat/tests/test_task_resume_state.py`

## Remaining responsibility and exclusions

- This is not a multi-user or network-service security design. The demo has
  no authentication, tenant isolation, remote deployment posture, or
  distributed queue.
- A same-OS principal with broad filesystem access can replace the state DB,
  Ledger DB, and secret material together, or roll them back. The local HMAC
  detects isolated state modification; it is not anti-rollback storage or a
  hardware-backed key boundary.
- Windows filesystem ACLs and deployment-secret handling are operator-owned;
  this code does not establish POSIX-style permission guarantees.
- A safe projection is not a general prompt-injection defense. Selected
  reference text remains model input and must not be treated as executable or
  authoritative instruction.
- The single-process generation lock is not a multi-process or distributed
  concurrency guarantee.
- No claim is made for infinite memory, lossless recall, RAG citation
  correctness, CRM/lead extraction, human handoff, model quality, or latency
  outside the measured local protocol.

Before any public release, repeat the complete local gates on the final
source, perform an independently scoped security review under an approved
code-sharing policy, verify build artifacts from the exact source revision,
and record the deployment-specific threat model.
