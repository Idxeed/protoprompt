# ProtoPrompt 0.16.1 — corrective release launch kit

Use this page only after the `v0.16.1` tag and its verified release artifacts
are published. The prior `v0.16.0` candidate did not publish artifacts because
its Linux release gate failed; it is intentionally not presented as a release.

## Release links

- Release: <https://github.com/Idxeed/protoprompt/releases/tag/v0.16.1>
- PyPI: <https://pypi.org/project/protoprompt/0.16.1/> (link only after the
  artifact is actually published)
- `pp-agent`: matching wheel and sdist are GitHub Release assets, not a
  separate PyPI upload.
- Notes: [RELEASE_NOTES.md](RELEASE_NOTES.md)
- Security record: [SECURITY_REVIEW-v0.16.1.md](SECURITY_REVIEW-v0.16.1.md)
- Roadmap: [ROADMAP.md](ROADMAP.md)

## RU

**ProtoPrompt 0.16.1 закрывает Linux release-gate для безопасного локального
agent runtime.** Jailed `edit` использует оптимистичный полный snapshot перед
атомарной заменой файла: нормальный `ctime` от rename не вызывает ложный отказ,
а проверяемый результат выдаётся только когда identity можно подтвердить.
Неоднозначная конкурентная замена получает явный uncertain-outcome с recovery
path, а не ложный success.

Это корректирующий выпуск границы эксплуатации для memory runtime, а не новый
benchmark, обещание «бесконечного контекста» или claim о качестве модели.

## EN

**ProtoPrompt 0.16.1 closes the Linux release gate for a safe local agent
runtime.** Jailed `edit` uses an optimistic full snapshot before atomic file
replacement: normal rename-induced `ctime` changes do not reject a valid
operation, while a result is verified only when entry identity remains
provable. An ambiguous concurrent replacement gets an explicit uncertain
outcome with a recovery path instead of a false success.

This is an operational corrective release for the memory runtime, not a new
benchmark, an "infinite context" claim, or a model-quality claim.

## Maintainer checklist

1. Attach the [security review](SECURITY_REVIEW-v0.16.1.md) and artifact
   checksums to the release before using this text publicly.
2. Add the PyPI link only after the package upload and checksum verification
   complete.
3. Keep the stated boundaries intact in posts, README updates, and demos.
