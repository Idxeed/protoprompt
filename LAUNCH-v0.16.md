# ProtoPrompt 0.16 — superseded candidate launch kit

Do not use this launch kit: `v0.16.0` stopped at its Linux release gate and
published no artifacts. The corrective public-release material is
[LAUNCH-v0.16.1.md](LAUNCH-v0.16.1.md). It deliberately makes no performance,
model-quality, universal prompt-injection, unlimited-memory, or `1.0.0` claim.

## Release links

- Corrective release: <https://github.com/Idxeed/protoprompt/releases/tag/v0.16.1>
- PyPI: <https://pypi.org/project/protoprompt/0.16.1/> (link only after the
  artifact is actually published)
- `pp-agent`: matching wheel and sdist are GitHub Release assets, not a
  separate PyPI upload.
- Notes: [RELEASE_NOTES.md](RELEASE_NOTES.md)
- Corrective kit: [LAUNCH-v0.16.1.md](LAUNCH-v0.16.1.md)
- Roadmap: [ROADMAP.md](ROADMAP.md)

## RU

### Короткий анонс

**ProtoPrompt 0.16 укрепляет границу локального agent runtime.** Состояние и
права больше не берутся из репозитория: они принадлежат профилю пользователя
и привязаны к filesystem identity выбранного проекта. Замена каталога по тому
же пути не наследует его память или разрешения.

Namespace v3 учитывает stable creation generation каталога, поэтому старые
agent state/session директории намеренно не подхватываются автоматически.

На Windows путь проекта, Git root и `.git` marker проходят native no-reparse
проверку до запуска. UNC/junction/symlink отклоняются, а `.git`-файл обычного
Git worktree поддерживается. Jailed операции на Windows, для которых пока нет
безопасной kernel-backed гарантии, честно fail closed; на Linux они требуют
`openat2`/`renameat2`.

Это выпуск про объяснимую и безопасную эксплуатационную границу для memory
runtime, а не про новые цифры качества модели или «бесконечный контекст».

### Честные границы

- Подтверждённый `bash` не является sandbox и выполняется с правами текущего
  пользователя.
- Windows пока не делает jailed overwrite существующего файла, `edit`,
  рекурсивный обход дерева или jailed `bash`; path-based fallback не
  включается.
- Linux host без `statx` birth-time для корня проекта fail closed при запуске.
- Docker runtime/latency/throughput benchmark не является релизным claim.
- До `1.0` остаются performance protocol на reference hardware, held-out
  quality/conflict evidence, migration proof и reviewed integrations.

## EN

### Short announcement

**ProtoPrompt 0.16 hardens the local agent-runtime boundary.** State and
permissions no longer come from the repository: they belong to the user
profile and are bound to the selected project's filesystem identity. Replacing
a directory at the same pathname does not inherit its memory or permissions.

Namespace v3 also includes the root's stable creation generation, so older
agent state/session directories are intentionally not imported automatically.

On Windows, the project path, Git root and `.git` marker receive a native
no-reparse check before startup. UNC paths, junctions and symlinks are
rejected, while a normal Git-worktree `.git` file remains supported. Jailed
operations without a safe kernel-backed primitive deliberately fail closed on
Windows; Linux requires `openat2`/`renameat2`.

This is an operational-boundary release for a reliable memory runtime, not a
new model-quality number or an "infinite context" claim.

### Honest boundaries

- Approved `bash` is not a sandbox; it runs with the current user's rights.
- Windows does not yet perform jailed existing-file overwrite, `edit`,
  recursive tree traversal, or jailed `bash`; no pathname fallback is enabled.
- A Linux host without `statx` birth time for the project root fails closed at
  startup.
- Docker runtime, latency and throughput are not release claims.
- Before `1.0`, the roadmap still requires reference-hardware performance,
  held-out quality/conflict evidence, migration proof and reviewed
  integrations.

## Maintainer checklist

1. Attach [the internal security-review record](SECURITY_REVIEW-v0.16.1.md) and
   the artifact checksums to the release before using this text publicly; keep
   the core and `pp-agent` wheel/sdist pairs in `SHA256SUMS`.
2. Add the PyPI link only after the package was actually uploaded and verified.
3. Keep the stated boundaries intact in every post, README update or demo.
