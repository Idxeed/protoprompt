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

## Full request budgeting

`build()` limits only the assembled system context. When an application adds
history and the current turn itself, use `build_messages()` as the safe
request-level API. It accounts for system context, provider framing, retained
history items, and the mandatory final turn under one limit.

```python
messages = await builder.build_messages(
    ContextInput(query=question, system_prompt="Answer concisely."),
    history=history,
    user_message=question,
    output_reserve=1_024,
)
```

`output_reserve` leaves room for the model's response. If the mandatory final
turn plus the reserve does not fit, `TokenBudgetExceededError` is raised rather
than silently trimming it. For integrations where the current turn contains
multiple portable message items (for example a tool call), pass
`final_messages`. Those items must use JSON-compatible values: structured
content, `tool_calls`, `tool_call_id`, and other fields are included in the
estimate.

Retained OpenAI-style history keeps an assistant `tool_calls` item and its
matching `tool` results as one atomic group. Agents/Responses history is a
contiguous dependency graph: call/output pairs (including hosted MCP approvals
and SDK-valid anonymous server-side tool searches), streamed shell/tool-search
outputs, program-owned children, and their preceding reasoning items are
retained or omitted together. This
also supports interleaved program invocations without reordering items. Input
controls such as `compaction_trigger` and `item_reference` are omitted from
optional history, and reasoning is kept only with a real model-emitted
follower.

When the mandatory final input starts with a tool output, its complete trailing
history graph is reserved as required context. If that dependency cannot fit,
the builder raises `TokenBudgetExceededError` instead of emitting an orphaned
final output. Anonymous **server-side** tool-search outputs are paired with
history calls by SDK order across that boundary; client-side searches require a
`call_id`.

## Counting tokens

The default `RegexTokenCounter` is fast, dependency-free, and
multilingual. The optional `TiktokenCounter` provides a model-aware local
estimate for text plus deterministic message framing:

```bash
pip install "protoprompt[tiktoken]"
```

```python
from protoprompt.tokens import TiktokenCounter

counter = TiktokenCounter(model="gpt-4o-mini")
# or
counter = TiktokenCounter(encoding="cl100k_base")
```

The ceiling is hard in the units of the counter you select. Provider wire
formats and model limits can change, so use the provider's native
`count_tokens` API for exact or billable counts at the request boundary, and
set the provider's response limit to match `output_reserve`.

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
