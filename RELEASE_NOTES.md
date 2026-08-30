# protoprompt 0.8.0

This release adds a durable-memory foundation with an explicit lifecycle:
facts may persist locally, but they do not become recallable until a trusted
host confirms them, and every later lifecycle decision is scope-bound,
revision-checked, and explainable.

## Highlights

- **Experimental Memory Ledger** — the new opt-in SQLite ledger provides a
  host-pinned `MemoryWriter`, typed candidate/active/superseded/retracted/
  expired/quarantined lifecycle, optimistic revisions, retry-safe event IDs,
  provenance, and explicit confirmation before default recall.
- **Deletion semantics that say exactly what they do** — `forget()` removes
  plaintext and local provenance payloads, `forget_by_source()` atomically
  revokes an opaque source inside one scope, and `erase()` supplies a narrow
  local hard-erase path with replay barriers. These are live-ledger guarantees;
  they do not claim deletion from an external vector store, SQLite backups,
  WAL/journals, or storage media.
- **Fail-closed SQLite boundary** — exact table/index definitions, reserved
  schema object names, rejection of unowned triggers on ledger tables,
  case-insensitive identifier handling, and post-lock validation prevent a
  shared database from silently changing memory lifecycle or copying payloads.
- **No surprise migration** — the ledger remains separate from legacy vector,
  profile, and session components. It has explicit setup, dry-run, backup, and
  export APIs, so hosts can evaluate it before connecting a later adapter.

## Upgrade

```bash
pip install --upgrade protoprompt
```

Existing public context, RAG, profile, and session APIs remain supported. To
evaluate the ledger, set it up explicitly and keep its writer inside trusted
host code:

```python
from protoprompt.ledger import MemoryWriter, SqliteMemoryLedger
from protoprompt.scope import MemoryScope

ledger = SqliteMemoryLedger("memory-ledger.db")
ledger.setup()
writer = MemoryWriter(
    ledger,
    scope=MemoryScope(tenant="local", user="alice", thread="chat-42"),
)
candidate = writer.propose(
    kind="preference",
    content="The user prefers answers in Russian.",
    source_ref="turn:42",
)
writer.confirm(candidate.record_id, expected_revision=candidate.revision)
```

For the local Ollama reference app, install compatible local sources and keep
the server on loopback unless you add an authentication boundary:

```bash
pip install "protoprompt[documents,fastapi,ollama]==0.8.0"
pip install "git+https://github.com/Idxeed/protoprompt.git@v0.8.0#subdirectory=apps/ollama-chat"
pp-ollama-chat
```

The full transcript remains local, while the active request is bounded. The
app therefore provides durable recall, not a promise of perfect recall or an
unbounded model context window.

## Release gate

The tag workflow checks the tag/version pair, deterministic library, agent,
benchmark, and Ollama-app tests; builds Russian and English docs; validates
library and reference-app wheels; imports the wheels from a clean environment;
then publishes the library artifacts and creates the GitHub release from these
notes.
