# User profile

Session memory lives within a single dialogue. The
profile is what persists **between** sessions: who the user is, their
stack, role, communication style, and output-format preferences. It
accumulates incrementally and survives restarts.

`protoprompt.profile` is a layer symmetric to the others: sources
(`ProfileProtocol`) → delta (`ProfileDelta`) → merge → storage
(`ProfileStore`) → render into `ContextInput`.

## How it works

```
signals (message / tool_result / feedback / ...)
   →  a source extracts a delta
   →  merge folds it into the existing profile (version += 1)
   →  the profile is saved to a ProfileStore
   →  render() turns it into system_prompt text
```

A profile consists of:

- **`facts`** — an open dictionary of durable facts (`name`, `role`,
  `tech_stack`, ...), mutated via explicit `add` / `update` / `forget` ops.
- **`traits`** — typed communication traits (`style`, `expertise`,
  `verbosity`, `formality`) with strict enum values.
- **`preferences`** — output preferences (`format`, `language`, `topics`).
- **`summary`** — a free-form distillation.

Each merge bumps `version` and sets `updated_at`/`source`, so a profile can
be inspected as a versioned history.

## Quickstart

```python
import asyncio
from protoprompt.profile import ProfileManager, RuleProfileSource, Signal
from protoprompt.profile.store import SqliteProfileStore

async def main():
    store = SqliteProfileStore("profiles.db")
    manager = ProfileManager(store, RuleProfileSource())

    profile = await manager.update("u1", [
        Signal(user_id="u1", kind="message", role="user",
               text="I'm a backend dev, I write Python and SQLite."),
    ])
    print(profile.facts)          # accumulated facts
    print(profile.version)        # 1

    # the next call merges into the existing profile
    profile = await manager.update("u1", [
        Signal(user_id="u1", kind="feedback", text="I like short answers."),
    ])
    print(profile.version)        # 2

asyncio.run(main())
```

## Sources

`ProfileProtocol` is the source contract: `extract(user_id, signals) ->
ProfileDelta`. Built-ins:

| Source                   | What it does                                                          |
|--------------------------|-----------------------------------------------------------------------|
| `LLMProfileSource`       | Asks the model for strict JSON (facts + traits). One retry on bad output, then fallback. |
| `RuleProfileSource`      | Deterministic zero-LLM rules: message length → verbosity, alphabet → language, markers → formality. |
| `CompositeProfileSource` | Runs several sources and folds their deltas (first/last non-empty).   |

```python
from protoprompt.profile import LLMProfileSource

manager = ProfileManager(store, LLMProfileSource(llm, language="en"))
```

## Storage

`ProfileStore` is a key-value store by `user_id`, separate from the vector
store (profiles are read by exact key, not by meaning):

- `InMemoryProfileStore` — tests and prototypes;
- `SqliteProfileStore` — persistent, no external services;
- `as_async_profile(store)` — wrapper for `asyncio` (worker threads).

## Rendering into context

`ContextInput` accepts a structured profile directly:

```python
from protoprompt import ContextBuilder, ContextInput

out = await builder.build(ContextInput(
    query="...",
    system_prompt="You are an assistant.",
    include_profile=True,
    profile=profile,       # structured UserProfile
    language="en",         # localizes the section header
))
```

`profile` takes precedence over raw `profile_text`; an empty profile adds no
section at all. Section headers (profile, conversation history) are
localized via `protoprompt.i18n`.
