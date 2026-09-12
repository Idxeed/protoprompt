# ProtoPrompt 0.18.0 release candidate security review

**Release-gate status:** release-candidate review. This record is not an
external penetration test, security certification, deployment approval, or a
claim that 0.18.0 has been published.

This review covers the unpublished v0.18 delta relative to the v0.17.0 tag:
provider-safe task projection, the local task-resume demo host, exact-scope
payload purge, policy/storage conformance, migration evidence, and SQLite
fault/concurrency hardening. Final publication remains conditional on the
tag-triggered release workflow verifying the exact source and artifacts.

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

- Clean-archive Python 3.12 core non-integration suite on 2026-09-10:
  `766 passed, 3 skipped, 27 deselected`.
- Deterministic agent CLI and Ollama/PDF reference-app suites:
  `321 passed, 50 platform-specific skips, 10 integration tests deselected`.
- Disposable PostgreSQL 17/pgvector integration suite:
  `18 passed, 1 environment-specific collation skip`.
- Frozen offline memory benchmark protocol versions v0.1–v0.5 and exact
  v1.0 SQLite/PostgreSQL semantic parity verified locally. These are contract
  checks, not model-quality or universal latency claims.
- Core wheel/sdist, CLI wheel/sdist, and source-only Ollama app wheel built and
  passed `twine check` from the same clean archive.
- English and Russian documentation builds succeeded with MkDocs strict mode;
  existing Material/MkDocs 2 compatibility advisories are not security test
  results.

## Static analysis triage (2026-09-13)

Bandit 1.9.4 scanned 30,670 lines across the core package, agent CLI, and
Ollama reference app. The unfiltered medium/high report contains one B602
finding and 17 B608 findings. Each was reviewed at its call site rather than
suppressed:

- B602 identifies the agent CLI's intentional shell-tool boundary. Arbitrary
  browser or model input cannot invoke it directly: the host permission layer
  must approve the command, the project identity is checked before and after
  execution, and descriptor-pinned jailed execution fails closed outside its
  supported Linux environment. The jail is documented as containment for the
  working directory, not as a general shell sandbox. This remains an accepted
  risk of the experimental CLI and is one reason the CLI is not classified as
  a stable public API.
- The B608 findings are fixed table names, identifiers validated before
  quoting, boolean-selected constant clauses, or generated placeholder lists.
  Payload values remain parameter-bound. Review found no untrusted SQL
  identifier interpolation in those paths.

A second medium/high, medium-confidence pass excluding only the two reviewed
rule classes (`B602,B608`) reported no additional issues. The scan contained
zero `# nosec` skips. This triage is not a substitute for an independent
penetration test or a deployment-specific security review.

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

The tag workflow must repeat the complete deterministic, PostgreSQL,
benchmark, documentation, clean-install, and artifact-integrity gates on the
final source revision before publication. Any non-local deployment still
requires its own independently scoped security review and deployment-specific
threat model.
