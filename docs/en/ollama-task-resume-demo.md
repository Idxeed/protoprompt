# Local Ollama task-resume demo *(experimental)*

This is a deliberately narrow, **local-only** demonstration of task-resume
memory alongside the existing Ollama chat and PDF RAG interface. It is useful
for showing one important distinction: a model can receive a bounded,
provider-safe reference to a host-confirmed task episode while still answering
against the user's current PDF question.

It is not an authentication layer, a multi-user product, an agent workflow,
or a way for a browser to manage Ledger memory. For the core contract, see
[Task-resume memory](task-resume.md).

!!! danger "Do not expose this demo on a network"

    The mode is intentionally restricted to a loopback browser client and a
    loopback Ollama endpoint. It has no authentication, tenant boundary, or
    remote-operation support. Do not place it behind a proxy, bind it for
    other users, or use remote Ollama for a customer demonstration.

## What it demonstrates

On startup, trusted host code reads one private seed file and creates (or
restores) one conversation-to-task mapping. The browser still sees only the
ordinary chat, PDF upload, and conversation routes.

- The current user question remains the RAG query, so fresh uploaded PDFs are
  retrieved normally.
- The model receives a fixed reduced reference: goal, aggregate completed
  action count, outcome, next action, and lesson.
- It never receives the raw task reference, individual action references,
  frozen descriptor, checkpoint ID, Ledger record ID, source/evidence IDs, or
  checkpoint secret through the integration.
- PDF text, conversation turns, and model output are never admitted to the
  task Ledger automatically. While the task mapping is active, the app also
  excludes and does not create the ordinary transcript semantic archive for
  that conversation.

The reference text itself is untrusted data. A model response is advice only;
it cannot execute a task or alter the host mapping.

## Run it from a source checkout

Install the local app and pull local Ollama models:

```bash
ollama pull llama3.1
ollama pull nomic-embed-text

python -m pip install -e ".[documents,fastapi,ollama,dev]"
python -m pip install -e "apps/ollama-chat[dev]"
```

Copy and edit the host-owned example seed. Do not put secrets in its text
fields: `goal`, `next_action`, and `lesson` are intentionally sent to the
model as reference data.

```bash
cp apps/ollama-chat/examples/task-resume-demo.seed.json ./task-resume-demo.seed.json
pp-ollama-chat --task-resume-demo-seed ./task-resume-demo.seed.json
```

On PowerShell, use `Copy-Item` instead of `cp` if preferred. Open
`http://127.0.0.1:8000`; the seeded conversation is already present. Upload a
text-layer PDF, then ask a question about its current contents.

The command fails closed if any of these conditions is not true:

- the web bind or TCP peer is not loopback;
- `OLLAMA_HOST` is not a loopback endpoint;
- the seed JSON is malformed, oversized, duplicated-key, or has an unknown
  field; or
- the existing signed mapping is tampered with or is already closing.

`--allow-network` and `--task-resume-demo-seed` cannot be used together.

## Demo-safe local profile

Use one local `llama3.1:8b` generation with a 2048-token context ceiling:

```bash
export OLLAMA_CHAT_MODEL="llama3.1:8b"
export OLLAMA_CHAT_REQUEST_MAX_TOKENS=2048
export OLLAMA_CHAT_OUTPUT_RESERVE=1024
pp-ollama-chat --task-resume-demo-seed ./task-resume-demo.seed.json
```

When explicit demo mode is enabled, the application caps every request at
`num_ctx=2048` even if the ordinary app configuration is larger, rejects an
output reserve of 2048 or more, and serializes all local model generations
through one in-process queue. This is a deliberately conservative demo
profile, not a throughput service or a distributed scheduler.

The reference observation for this profile is a Ryzen 5 5600X, 32 GiB RAM,
RX 7600 XT 16 GB, and Ollama 0.33.2: `llama3.1:8b` at `num_ctx=2048` and
`nomic-embed-text` ran fully on GPU; warm generation measured 46.6–48.2
tokens/s. That is one local measurement, not a hardware guarantee, model
quality claim, or customer sizing promise. Do not add 14B, vision, voice, or
new-GPU requirements to this demo profile.

## Seed contract

The seed is host configuration, not a browser payload. Its exact JSON shape
is versioned and rejects unknown fields:

```json
{
  "schema_version": 1,
  "conversation_id": "launch-demo",
  "task_descriptor": "Host-only frozen recall descriptor.",
  "goal": "Safe model-visible task summary.",
  "completed_action_refs": ["action:host-only-reference"],
  "outcome": "interrupted",
  "next_action": "Safe model-visible next discussion step.",
  "lesson": "Safe model-visible lesson."
}
```

`outcome` is one of `succeeded`, `failed`, or `interrupted`. The descriptor
and action references stay host-only; do not treat the visible fields as a
place to hide credentials, customer data, or execution instructions.

## Local persistence and deletion

The app generates a stable 32-byte checkpoint secret in the private data
directory. It is not stored in either SQLite database. The state mapping is
an additive, HMAC-authenticated table in `chat.db`; task Ledger operations use
the separate `task-resume-ledger.db`.

Restarting with the same seed and data directory restores an authenticated
active mapping. A changed seed does not silently replace an existing mapping.
Deleting the seeded conversation first changes its mapping to `closing`, then
forgets the host task source in the Ledger, and only then removes the mapping
and ordinary conversation data. If cleanup fails, the mapping remains
non-resumable and deletion returns an error so it can be retried.

Never copy these data files between machines or users. On POSIX, the app
creates local databases, secret, and uploads owner-readable only; on Windows,
keep the data directory inside a user-restricted profile.

## Deliberate limits for a commercial demo

- It is a local product demonstration, not a deployable shared service.
- It has one host-authored episode per mapped conversation, not autonomous
  extraction or an infinite-memory claim.
- It has no task creation, rebind, review, admission, checkpoint, or resume
  endpoint for the browser or model.
- It has no tool execution, workflow planning, dependency graph,
  exactly-once delivery, or background task recovery.
- PDF RAG is current untrusted evidence; it does not become a trusted Ledger
  record merely because it is retrieved.

For a customer presentation, show the PDF answer and the bounded context
receipt, then state these limits plainly. They are what keep the demo useful
without pretending it is an authorization system.
