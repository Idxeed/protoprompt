# ProtoPrompt 0.17.0 — task-episode resume launch kit

Use this page only after the `v0.17.0` tag and its verified release artifacts
are published.

## Release links

- Release: <https://github.com/Idxeed/protoprompt/releases/tag/v0.17.0>
- PyPI: <https://pypi.org/project/protoprompt/0.17.0/> (link only after the
  artifact is actually published)
- `pp-agent`: matching wheel and sdist are GitHub Release assets, not a
  separate PyPI upload.
- Notes: [RELEASE_NOTES.md](RELEASE_NOTES.md)
- Security record: [SECURITY_REVIEW-v0.17.0.md](SECURITY_REVIEW-v0.17.0.md)
- Task-resume guide: [docs/en/task-resume.md](docs/en/task-resume.md)
- Roadmap: [ROADMAP.md](ROADMAP.md)

## RU

**ProtoPrompt 0.17.0 добавляет узкую и проверяемую границу возобновления
задачи из Ledger-памяти.** Trusted host подтверждает typed `TaskEpisode`,
привязывает opaque task reference к отдельному scope и HMAC-sealed selection,
а перед каждым resume заново проверяются provenance, lifecycle, payload и
фиксированный data lane. Живой запрос, включая PDF RAG, остаётся текущим
запросом; замороженный descriptor не подменяет его.

Это фундамент для host-owned continuation, а не workflow engine, checkpoint
агента, permission для tool execution или обещание «бесконечной памяти».
`TaskProcedure` пока не выполняется и не участвует в selection. Интеграция не
подключается автоматически к browser-facing Ollama/PDF app или CLI.

## EN

**ProtoPrompt 0.17.0 adds a narrow, verifiable task-resume boundary for Ledger
memory.** A trusted host confirms typed `TaskEpisode` data, binds an opaque
task reference to a dedicated scope and HMAC-sealed selection, and freshly
checks provenance, lifecycle, payload, and the fixed data lane on every
resume. The live request, including PDF RAG, remains the current query; the
frozen descriptor does not replace it.

This is a foundation for host-owned continuation, not a workflow engine, agent
checkpoint, tool-execution permission, or an "infinite memory" promise.
`TaskProcedure` is neither selected nor executed yet. There is no automatic
connection to the browser-facing Ollama/PDF app or CLI.

## Maintainer checklist

1. Attach the [security review](SECURITY_REVIEW-v0.17.0.md) and artifact
   checksums before using this text publicly.
2. Add the PyPI link only after upload and checksum verification succeed.
3. Keep the host-ownership and non-goal wording intact in posts, demos, and
   integration examples.
