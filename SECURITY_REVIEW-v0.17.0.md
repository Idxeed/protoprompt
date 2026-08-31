# ProtoPrompt 0.17.0 — internal security review

**Release-gate status:** candidate; publication is permitted only after the
tag workflow verifies the same source and artifact checksums.

This is an internal source-level review of the v0.17 task-episode resume
delta. It is not an external penetration test or a security certification.

## Result

No P1 or P2 issue was found in the reviewed task-resume boundary. The adapter
is intentionally narrower than a generic checkpoint or workflow API:

- It requires exactly admitted `host_assertion` `episode` records, immutable
  admission evidence, and a confidence floor of 0.75. A document origin,
  procedure kind, raw JSON shape, or origin label alone cannot widen the
  selection policy.
- `task_resume_scope()` binds the full parent-scope correlation and host-minted
  task reference into the backend namespace. The adapter rejects a planner or
  builder outside that exact derived scope.
- The task descriptor is retained only as an in-memory adapter capability;
  it is neither persisted in the checkpoint nor accepted as a per-resume
  replacement. Restart needs a host-owned `{task_ref, descriptor,
  checkpoint_id}` mapping.
- Before and after composition the selected data is freshly resolved and
  strictly decoded as matching `TaskEpisode` data. HMAC/checkpoint,
  continuation, lifecycle, revision, malformed-data, cross-task, and stale
  paths fail closed.
- The final validation uses the exact Ledger data lane owned by
  `LedgerComposedRequest`, rather than scanning caller-controlled history,
  user, or final messages. A JSON lookalike cannot substitute the selected
  Ledger content.

## Evidence

- Focused Ledger task-resume, checkpoint, recall, and composition suite:
  `88 passed`.
- Complete core non-integration suite: `650 passed, 3 skipped, 26 deselected`.
- Frozen offline v0.4 SQLite semantic protocol: `5/5` cases and `21/21`
  checks, covering restart/reconstruction with a live RAG query, strict typed
  origin, scope isolation, continuation/lifecycle rejection, and lane/receipt
  boundaries.
- Local non-integration application gates: `pp-agent` `273 passed, 49
  skipped, 10 deselected`; local Ollama/PDF reference app `24 passed, 1
  skipped`.
- The release workflow rebuilds core and CLI artifacts, verifies package
  contents/checksums, repeats benchmark/docs/app gates, and attaches this
  review record only after verified PyPI artifacts are visible.

## Boundaries and remaining responsibility

- This review does not claim arbitrary in-process plugin isolation or safety
  against a principal with direct Ledger/SQLite write access. Stronger
  tamper-evidence requires a separate signing/process-isolation boundary.
- The host must protect checkpoint secrets and the durable task mapping, and
  must never let a model or untrusted client mint task references, descriptors,
  checkpoint IDs, memory records, admission decisions, or planner instances.
- Composed provider messages necessarily contain selected reference data. The
  content-free receipt does not make those transient messages safe to log or
  expose; the host should keep and send them promptly.
- This is not a general prompt-injection defense, a sandbox, tool authority,
  workflow engine, exactly-once guarantee, model-quality claim, or an
  unlimited-context/infinite-memory claim.
