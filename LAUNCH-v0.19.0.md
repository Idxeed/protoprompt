# ProtoPrompt 0.19.0 PostgreSQL recovery evidence launch kit

Publish this copy only after the `v0.19.0` tag workflow, PyPI files, GitHub
Release assets, and `SHA256SUMS` have all been independently verified.

## Canonical links

- Release: <https://github.com/Idxeed/protoprompt/releases/tag/v0.19.0>
- PyPI: <https://pypi.org/project/protoprompt/0.19.0/>
- Documentation: <https://idxeed.github.io/protoprompt/>
- PostgreSQL guide: <https://idxeed.github.io/protoprompt/en/postgres/>
- Roadmap: [ROADMAP.md](ROADMAP.md)
- Security record: [SECURITY_REVIEW-v0.19.0.md](SECURITY_REVIEW-v0.19.0.md)

## Russian announcement

**ProtoPrompt 0.19.0 проверяет память агента там, где локальные тесты обычно
заканчиваются: при реальном обрыве PostgreSQL-соединения в середине удаления.**

Новая integration matrix завершает server backend после payload delete и
после записи aggregate receipt, затем открывает fresh connection и требует
точный rollback + идемпотентный retry всей host-команды. Отдельно проверяются
потеря ответа после commit, конкурентные duplicate операции, sibling-scope
isolation и отсутствие зависшего advisory lock.

Это узкое доказательство transaction/reconnect semantics, а не обещание
managed PITR, exactly-once orchestration, throughput или «бесконечной памяти».
Runtime API относительно 0.18.0 не расширялся.

Install:

```bash
python -m pip install "protoprompt==0.19.0"
```

## English announcement

**ProtoPrompt 0.19.0 tests agent memory where ordinary local tests often stop:
a real PostgreSQL connection loss in the middle of deletion.**

The new integration matrix terminates the server backend after payload
deletion and after aggregate-receipt insertion, then reconnects and requires
exact rollback plus an idempotent retry of the whole host command. It also
covers lost responses after commit, concurrent duplicate operations,
sibling-scope isolation, and advisory-lock release.

This is narrow transaction/reconnect evidence, not a managed-PITR,
distributed-exactly-once, throughput, or infinite-memory claim. The runtime API
has not expanded relative to 0.18.0.

Install:

```bash
python -m pip install "protoprompt==0.19.0"
```

## Release-owner checklist

- [ ] Tag workflow completed successfully for the exact annotated tag commit.
- [ ] PyPI exposes one wheel and one sdist at version 0.19.0.
- [ ] PyPI SHA-256 digests equal the GitHub Release copies.
- [ ] GitHub Release contains core wheel/sdist, CLI wheel/sdist, this version's
  security record, and `SHA256SUMS`.
- [ ] `master`, tag dereference, workflow SHA, and published artifacts identify
  the same source revision.
- [ ] Only then publish the announcement text above.
