# ProtoPrompt 0.20.0 v1 API boundary launch kit

Publish this copy only after the `v0.20.0` tag workflow, PyPI files, GitHub
Release assets, and `SHA256SUMS` have all been independently verified.

## Canonical links

- Release: <https://github.com/Idxeed/protoprompt/releases/tag/v0.20.0>
- PyPI: <https://pypi.org/project/protoprompt/0.20.0/>
- API boundary: <https://idxeed.github.io/protoprompt/en/api-stability/>
- Documentation: <https://idxeed.github.io/protoprompt/>
- Roadmap: [ROADMAP.md](ROADMAP.md)
- Security record: [SECURITY_REVIEW-v0.20.0.md](SECURITY_REVIEW-v0.20.0.md)

## Russian announcement

**ProtoPrompt 0.20.0 фиксирует узкое ядро API, которое мы ведём к стабильной
линии 1.x.**

Новый `protoprompt.api` отделяет контракты explainable `ContextPlan`, durable
memory lifecycle, scope, policy и встроенного SQLite/PostgreSQL storage от
экспериментальных recall/task-resume/agent/adapters. Устанавливаемый JSON
manifest и contract tests проверяют точные exports, поля, методы, enum,
experimental exclusions и import без optional SDK.

Это v1 candidate, а не преждевременный релиз 1.0. Managed PostgreSQL restore,
independent installs и финальные quality/performance/security gates остаются
явно открытыми.

```bash
python -m pip install "protoprompt==0.20.0"
```

## English announcement

**ProtoPrompt 0.20.0 freezes the narrow API core we are carrying toward the
stable 1.x line.**

The new `protoprompt.api` separates explainable `ContextPlan`, durable memory
lifecycle, scope, policy, and built-in SQLite/PostgreSQL storage contracts from
experimental recall, task-resume, agent, and adapter surfaces. An installed
JSON manifest plus contract tests verify exact exports, fields, methods, enums,
experimental exclusions, and optional-SDK-free import.

This is a v1 candidate, not an early 1.0 claim. Managed PostgreSQL restore,
independent installs, and final quality/performance/security gates remain
explicitly open.

```bash
python -m pip install "protoprompt==0.20.0"
```

## Release-owner checklist

- [ ] Tag workflow completed successfully for the exact annotated tag commit.
- [ ] PyPI exposes one wheel and one sdist at version 0.20.0.
- [ ] PyPI SHA-256 digests equal the GitHub Release copies.
- [ ] GitHub Release contains core wheel/sdist, CLI wheel/sdist, this version's
  security record, and `SHA256SUMS`.
- [ ] `master`, tag dereference, workflow SHA, and published artifacts identify
  the same source revision.
- [ ] Only then publish the announcement text above.
