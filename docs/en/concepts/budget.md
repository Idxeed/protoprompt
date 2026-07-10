# Token budget

`ContextBuilder` will happily assemble a 50 000-token prompt if you
let it. `TokenBudgetedContextBuilder` enforces a hard ceiling.

## Priorities

Sections are filled in this order by default:

1. **system** — never trimmed, raises `TokenBudgetExceededError` if it
   does not fit alone.
2. **profile** — dropped silently if it would overflow.
3. **session** — blocks trimmed at word boundaries.
4. **rag** — same.

Override the order via the `priorities` constructor argument.

## How trimming works

The builder asks the store for `top_k * 2` candidates, then walks them
in score order. Each block that fits whole is kept. The first block
that does not fit is trimmed at the last word boundary that still
fits, then a `…` is appended. Subsequent blocks in that section are
dropped, and so are all later sections.

## Observability

`ContextOutput.budget_report` is always populated by the budgeted
builder:

```python
out = await builder.build(inp)
print(out.budget_report.used_tokens, "/", out.budget_report.budget)
print("dropped:", out.budget_report.dropped_blocks)
print("per-section:", out.budget_report.section_tokens)
```

## Counting tokens

The default `RegexTokenCounter` is fast, dependency-free, and
multilingual. For exact counts install the `tiktoken` extra:

```bash
pip install "protoprompt[tiktoken]"
```

```python
from protoprompt.tokens import TiktokenCounter

counter = TiktokenCounter(model="gpt-4o-mini")
# or
counter = TiktokenCounter(encoding="cl100k_base")
```

You can also plug in your own implementation — the protocol is one
method:

```python
from protoprompt.tokens import TokenCounter

class MyCounter:
    def count(self, text: str) -> int: ...
    def count_messages(self, messages: list[dict]) -> int: ...
```

## When NOT to use it

- For short, single-shot prompts where overflow is impossible — the
  plain `ContextBuilder` is cheaper.
- When the model's actual tokenization is wildly different from any
  reasonable heuristic (e.g. speech-to-text models) — pass a custom
  counter.
