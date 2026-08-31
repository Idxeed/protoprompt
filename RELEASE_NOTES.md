# protoprompt 0.17.0

ProtoPrompt 0.17.0 adds an experimental, deliberately narrow foundation for
resuming one host-owned task from durable Ledger memory. It does **not** turn
Ledger into a workflow engine, an agent-state checkpoint, or an "infinite
memory" product.

## What is new

- `TaskEpisode` and `TaskProcedure` are canonical, versioned reference-data
  contracts. Strict decoding rejects malformed JSON, duplicate or unknown
  fields, unsupported schemas, non-finite constants, and mismatched data.
- `TaskResumePlanner` selects only host-confirmed, admitted
  `host_assertion` `TaskEpisode` records. Procedures are typed data only in
  this release; they are not selected or executed.
- A host-minted `task_ref` derives a task-specific Ledger scope from the full
  parent-scope correlation. Equal task references under different parent
  thread/kind scopes cannot cross-read or resume each other.
- A sealed Ledger checkpoint binds the opaque continuation reference. On every
  compose, the adapter freshly verifies the checkpoint, lifecycle, typed
  records, and the composer-owned JSON data lane before returning a request.
- The frozen descriptor remains an in-memory host capability. The live
  `ContextInput.query` remains the current request/RAG query, so a task resume
  does not silently replace current PDF retrieval with stale task text.
- Frozen offline benchmark v0.4 adds five SQLite semantic cases / 21 checks:
  restart reconstruction, strict host-origin typed admission, parent/task
  isolation, continuation/lifecycle rejection, and receipt/lane boundaries.

Full trusted-host integration guidance is available in the English and Russian
[task-resume documentation](docs/en/task-resume.md).

## Host integration and recovery

The public constructor is:

```python
TaskResumePlanner(
    builder,
    recall,
    parent_scope=parent_scope,
    task_ref=task_ref,
    task_descriptor=descriptor,
)
```

The host, not the Ledger checkpoint, must durably retain the mapping:

```text
{ task_ref, descriptor, checkpoint_id }
```

Reconstruct the same parent scope, derived task scope, strict recall policy,
counter identity, checkpoint secret, and descriptor after restart. Do not send
`task_ref`, descriptor, checkpoint IDs, a `MemoryWriter`, review gate, or the
planner through a client request or model tool.

## Compatibility

This is an additive experimental API. Existing `LedgerContextComposer` callers
retain their prior behavior when they do not explicitly pass a host recall
task. No Ledger storage-schema migration is required.

The core package, `protoprompt-cli`, and the local Ollama/PDF reference app
ship at 0.17.0. The task-resume adapter is intentionally **not** auto-wired
into the CLI or browser-facing Ollama app; a trusted host must own admission
and task mapping first.

Install the core from PyPI, the local Ollama/PDF reference app from the
matching tag, and the CLI from the checksum-verified GitHub Release asset:

```bash
python -m pip install "protoprompt[documents,fastapi,ollama]==0.17.0"
python -m pip install "git+https://github.com/Idxeed/protoprompt.git@v0.17.0#subdirectory=apps/ollama-chat"
python -m pip install "https://github.com/Idxeed/protoprompt/releases/download/v0.17.0/protoprompt_cli-0.17.0-py3-none-any.whl"
```

After installing the matching core, the CLI can instead be installed directly
from the tag with
`python -m pip install "git+https://github.com/Idxeed/protoprompt.git@v0.17.0#subdirectory=apps/agent-cli"`.

## Verification

The release gates run the complete non-integration core suite, app suites,
strict Russian/English documentation builds, package smoke checks, and these
offline semantic checks:

```bash
python scripts/run_memory_benchmark.py --suite v0.1 --verify
python scripts/run_memory_benchmark.py --suite v0.2 --verify
python scripts/run_memory_benchmark.py --suite v0.3 --verify
python scripts/run_memory_benchmark.py --suite v0.4 --verify
```

The separate v1.0 evidence protocol remains a dual-backend SQLite/PostgreSQL
Ledger recall gate; it is not a claim that package 1.0.0 has shipped. See the
[benchmark guide](benchmarks/README.md) and the [internal security review
record](SECURITY_REVIEW-v0.17.0.md).

## Explicit boundaries

0.17.0 does not provide automatic extraction/admission, automatic task handoff,
procedure execution, dependency/conflict planning, tool authority, side
effects, exactly-once semantics, provider conversation snapshots, or a
workflow/agent checkpoint. It makes no model-quality, latency, throughput,
prompt-injection-immunity, unlimited-context, or infinite-memory claim.

It is a bounded host-side reference-data continuation boundary on the path
described in [ROADMAP.md](ROADMAP.md), not the final 1.0 release.
