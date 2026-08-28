# Context layers

`ContextOutput` exposes the four sources of context independently so
the UI can show provenance and the model can be told which is which.

## Layers

1. **System prompt** — always first, always authoritative. Mandatory;
   `TokenBudgetedContextBuilder` raises if it does not fit.
2. **Profile** — derived from the user's prior messages. Optional; if it
   would push the prompt over budget, it is dropped silently.
3. **Session memory** — compressed summary of older turns. Pulled from
   the vector store under the synthetic doc_id `session_{chat_id}`.
4. **RAG documents** — top-k chunks from the documents the user has
   attached to the chat.

In a multi-tenant application, the host pins one `MemoryScope` to the builder.
It applies to both RAG and session memory; switching tenants through
`ContextInput` is deliberately unsupported.

## When to enable what

| Scenario                                     | RAG | Session | Profile |
|----------------------------------------------|-----|---------|---------|
| One-shot Q&A on a document                   | ✅  | ❌      | ❌      |
| Long-running chat with one document          | ✅  | ✅      | ✅      |
| Continuing a conversation (no new context)   | ❌  | ✅      | ✅      |
| Multi-tenant user-specific assistant        | ✅  | ✅      | ✅      |

## Field-by-field reference

`ContextInput` fields:

- `query` (required) — the user's current question.
- `system_prompt` — anchored to the top of the final prompt.
- `doc_ids` — list of integer document IDs to search over.
- `chat_id` — when set, session memory is queried under
  `session_{chat_id}`.
- `embedding_model` — passed to `LLM.embed`.
- `top_k_rag`, `top_k_session` — retrieval depth per layer.
- `include_rag`, `include_session`, `include_profile` — toggles.

`ContextOutput` fields:

- `system_prompt` — the final assembled string.
- `rag_blocks`, `session_blocks` — the actual chunks that went in.
- `profile_used` — whether the profile was included.
- `budget_report` — only set by `TokenBudgetedContextBuilder`.
