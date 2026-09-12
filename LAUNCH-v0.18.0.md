# ProtoPrompt 0.18.0 safe task projection launch kit

Use this page only after the `v0.18.0` tag, PyPI artifacts, checksums, and
GitHub Release assets have been verified by the release workflow.

## Release links

- Release: <https://github.com/Idxeed/protoprompt/releases/tag/v0.18.0>
- PyPI: <https://pypi.org/project/protoprompt/0.18.0/>
- `pp-agent`: matching wheel and sdist are GitHub Release assets, not a
  separate PyPI upload.
- Notes: [RELEASE_NOTES.md](RELEASE_NOTES.md)
- Security record: [SECURITY_REVIEW-v0.18.0.md](SECURITY_REVIEW-v0.18.0.md)
- Local demo guide: [docs/en/ollama-task-resume-demo.md](docs/en/ollama-task-resume-demo.md)
- Roadmap: [ROADMAP.md](ROADMAP.md)

## RU

**ProtoPrompt 0.18.0 сокращает подтверждённую task memory до безопасной
provider projection и доказывает этот путь в локальном Ollama/PDF demo.** В
запрос модели попадают только цель, число завершённых действий, результат,
следующее действие и урок. Host identifiers, checkpoint, descriptor, scope,
provenance и secrets структурно остаются за границей provider lane.

Релиз также добавляет явную пару admission/recall policy, sealed storage
conformance receipts, non-destructive v0.6 cutover evidence и SQLite
crash/concurrency coverage. Это всё ещё alpha: PostgreSQL recovery/concurrency,
независимые reference installs и остальные RC-gates остаются обязательными до
1.0.

Это не workflow engine, не authority для tools, не hosted multi-tenant service
и не обещание «бесконечной памяти». Local demo слушает только loopback,
использует synthetic/host-seeded data и не делает automatic admission из PDF,
чата или model output.

## EN

**ProtoPrompt 0.18.0 reduces reviewed task memory to a provider-safe projection
and proves the path in a local Ollama/PDF demo.** Only the goal, completed-action
count, outcome, next action, and lesson enter the model request. Host
identifiers, checkpoint, descriptor, scope, provenance, and secrets remain
structurally outside the provider lane.

The release also adds an explicit admission/recall policy pair, sealed storage
conformance receipts, non-destructive v0.6 cutover evidence, and SQLite
crash/concurrency coverage. It is still alpha: PostgreSQL recovery/concurrency,
independent reference installs, and the remaining RC gates are required before
1.0.

This is not a workflow engine, tool authority, hosted multi-tenant service, or
an infinite-memory promise. The local demo binds only to loopback, uses
synthetic/host-seeded data, and never auto-admits PDF, chat, or model output.

## Maintainer checklist

1. Publish this text only after the tag workflow verifies PyPI and GitHub asset
   digests from the exact release commit.
2. Keep the alpha, local-only, and non-goal wording intact.
3. Link performance or quality claims only to a completed, versioned protocol
   and its immutable result artifact.
