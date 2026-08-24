# Session compression

Long chats eventually overflow the context window. `Pipeline` solves
this by periodically replacing the oldest turns with a vector-friendly
summary block.

## Strategies

### HeuristicStrategy

Pure Python, no LLM cost. Splits the session into three regions:

- **head** — first `head_count` messages (anchors intent).
- **tail** — last `tail_count` messages (recent context).
- **important** — middle messages that contain a keyword and are at
  least `min_length` characters.

Best for: small models, latency-sensitive chat, predictable behaviour.

```python
from protoprompt import HeuristicStrategy
strat = HeuristicStrategy(head_count=3, tail_count=5, min_length=80)
```

### LLMSummaryStrategy

Calls the LLM once per rolling window to produce a compact summary.
Each window is capped at `target_chars_per_block` characters.

```python
from protoprompt import LLMSummaryStrategy
strat = LLMSummaryStrategy(
    model="llama3.1",
    window_size=8,
    max_blocks=3,
    target_chars_per_block=600,
    language="ru",
    fallback=HeuristicStrategy(),
)
```

!!! warning "Fallback on failure"
    `LLMSummaryStrategy` swallows LLM errors and delegates to the
    fallback strategy. This is intentional: a chat must not break
    because the model timed out. Configure the fallback explicitly to
    control the degraded-mode behaviour.

Best for: chatty assistants, code-review bots, anything where the
mid-context carries real signal that keywords miss.

## Wiring it in

```python
from protoprompt import Pipeline

pipeline = Pipeline(
    store, llm,
    strategy=strat,
    compress_every_n=12,
    embedding_model="nomic-embed-text",
)

# At the end of every chat turn:
if pipeline.should_compress(len(session.messages)):
    await pipeline.compress_and_store(session)
```

The pipeline uses a write-then-delete pattern internally to avoid
leaving the store in a half-updated state on crash.

## Choosing `compress_every_n`

| Session length    | Suggested value |
|-------------------|-----------------|
| ≤ 20 turns        | never           |
| 20–60 turns       | 10              |
| 60+ turns         | 8               |
| Code-review bots  | 6               |

Lower numbers mean more frequent summarisation (more LLM cost) but
tighter working context. Higher numbers are cheaper but risk overflow.
