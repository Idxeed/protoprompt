# protoprompt 0.6.1

The reliability and isolation patch for the context runtime.

## Why this release

Context limits are a correctness boundary, not a best-effort hint. This patch
makes the final model-facing request safer when it contains a recent turn,
tool calls, structured content, recall, and an output reservation at the same
time. It also closes a profile-isolation edge case during migration from
unscoped to scoped storage.

## Highlights

- **Full request budgeting** — `build_messages()` accounts for rendered system
  context, retained history, the final user message, provider framing, and an
  optional output reserve before it retrieves or admits context blocks.
- **No hidden tool-call bypass** — built-in token counters now include
  structured content and tool-call payloads in their deterministic estimate.
- **Agents SDK parity** — the OpenAI Agents session callback applies the same
  ceiling to the complete model-facing item list, including the newest input.
- **Protocol-safe history** — retained Chat Completions and Agents/Responses
  call-output graphs, including hosted MCP approvals, interleaved
  program-owned children, streamed shell/tool-search outputs, anonymous
  server-side tool searches, and linked reasoning, are admitted or dropped
  together. Input-only Responses controls
  never enter optional recall; a final output reserves its required history
  graph or raises.
- **Scope-safe profiles** — `InMemoryProfileStore`, `SqliteProfileStore`, and
  their async variants keep logical user ids in the profile while deriving an
  isolated physical key from `MemoryScope`. A legacy literal key that happens
  to resemble a scoped key is never returned, overwritten, or deleted through
  a scoped operation.
- **Bound service/profile scopes** — `MemoryService` now rejects a configured
  `ProfileManager` unless both use the exact same host-owned `MemoryScope`.

## Upgrade

```bash
pip install --upgrade protoprompt
```

`output_reserve` is optional and defaults to zero, preserving the existing
constructor behavior. Use it when the host wants to leave a fixed response
allowance inside a model context window:

```python
builder = TokenBudgetedContextBuilder(
    store,
    embeddings,
    max_tokens=8_192,
    output_reserve=1_024,
)
```

When adopting profile scopes, treat existing unscoped profiles as a separate
namespace. Copy only records that your host explicitly authorizes into the
target `MemoryScope`; implicit adoption is deliberately forbidden.

A non-empty profile scope now requires native `supports_profile_scopes=True`.
Custom, Redis, and Postgres profile stores must implement that capability before
they can serve scoped profiles; `ProfileManager` fails closed before reading
from a store that does not declare it.

## Release gate

The tag workflow verifies the version/tag match, deterministic tests, strict
RU and EN documentation builds, wheel and sdist metadata, then publishes to
PyPI and creates the GitHub release from these notes.
