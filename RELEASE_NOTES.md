# protoprompt 0.16.1

ProtoPrompt 0.16.1 is the corrective hardening release for the local reference-agent
boundary. It does not expand the public memory or context API. Instead, it
makes the ownership of local state, project paths, jailed file operations, and
provider transport explicit and fail closed where the platform cannot support
the required guarantee.

The `v0.16.0` verification workflow stopped before publishing any PyPI or
GitHub Release artifact after its Linux CLI gate found a real edit-path
regression. The tag remains an unpublished candidate; this patch release does
not rewrite it.

## 0.16.1 corrective changes

- A normal Linux jailed `edit` now succeeds after `RENAME_EXCHANGE`: that
  syscall updates the old inode's `ctime`, so an optimistic exact snapshot is
  checked before the exchange and a ctime-tolerant inode/size/mtime/digest
  check is performed when the displaced entry remains observable. A concurrent
  source write in the exchange window is detected and the atomic exchange is
  rolled back. If a competing writer replaces the target entry in that window,
  the inverse exchange restores the competing entry while its identity remains
  observable. A missing or replaced displaced entry, a second rename, or a
  post-commit filesystem failure reports an uncertain outcome with the target
  and generated recovery path rather than a false success; this low-level
  pathname API cannot promise inode-CAS, last-writer ordering, or a
  transactional abort in that window.
- The startup project chooser accepts only an existing directory.
- Linux symlink-grep coverage now proves that external file contents are not
  returned without mistaking the user-provided search string in a no-match
  message for leaked content.

## Highlights

- **User-owned durable state** — `pp-agent` no longer automatically reads or
  writes `<repository>/.protoprompt`. State, sessions, per-project config and
  durable denials live under the user's application-data directory.
- **Identity-bound projects** — state namespaces include the canonical project
  path, filesystem identity and stable creation generation. Recreating a
  directory at the same pathname, including a filesystem-recycled inode, does
  not inherit its predecessor's sessions, memory, configuration or permissions.
- **Native project discovery on Windows** — project selection, Git-root
  discovery and the `.git` marker are opened component by component without
  reparse traversal. UNC paths and junctions/symlinks are rejected; normal
  Git-worktree `.git` files remain supported.
- **Stricter jailed tools** — Linux requires kernel-backed `openat2` and
  `renameat2` operations. Windows reads a concrete relative file through a
  native root-relative handle and refuses existing-file overwrites and
  recursive tree operations rather than falling back to pathname traversal.
- **Explicit outbound provider contract** — the OpenAI agent path uses the
  configured direct REST client with a fixed `PP_*` credential contract and
  `trust_env=False`; ambient SDK/proxy configuration cannot silently choose a
  different route or secret. The reusable HTTP/Ollama clients also disable
  ambient proxy/CA settings by default; applications must opt in explicitly.
- **Terminal-safe interaction** — model, tool, file, trace and startup-menu
  text is rendered inert before display, so ANSI/OSC, C1, DEL and Unicode
  format/bidi controls cannot modify a later interactive consent prompt.

## Compatibility and migration

There is no core signature or data-schema migration in 0.16.1. There is one
outbound-transport behavior migration: `HttpxLLMClient` and `OllamaClient` now
default to `trust_env=False`. A deployment that intentionally used process
proxy or CA environment settings must pass `trust_env=True` explicitly for a
trusted endpoint. The agent CLI intentionally does **not** import
repository-local `.protoprompt` state or configuration.
Its namespace salt changes from v2 to v3 to include the stable root creation
generation, so pre-v0.16/early-v0.16 user-state folders are deliberately not
auto-imported. Move only the settings you explicitly trust into the new
user-owned configuration directory or pass a trusted file through `--config`;
remove plaintext secrets from old TOML files rather than copying them.

Install the core from PyPI, the local Ollama/PDF reference application from the
matching source tag, and `protoprompt-cli` 0.16.1 from its checksum-verified
GitHub Release asset (it is not a separate PyPI upload):

```bash
python -m pip install "protoprompt[documents,fastapi,ollama]==0.16.1"
python -m pip install "git+https://github.com/Idxeed/protoprompt.git@v0.16.1#subdirectory=apps/ollama-chat"
python -m pip install "https://github.com/Idxeed/protoprompt/releases/download/v0.16.1/protoprompt_cli-0.16.1-py3-none-any.whl"
```

After installing the matching core, the agent can instead be installed directly
from the tag with
`python -m pip install "git+https://github.com/Idxeed/protoprompt.git@v0.16.1#subdirectory=apps/agent-cli"`.

## Explicit boundaries

- `bash` remains an explicitly approved user-process command, not a sandbox.
  In jailed mode it uses a descriptor-pinned Linux working directory and
  deliberately fails closed on Windows and non-Linux POSIX hosts rather than
  run an approved command from a replaceable pathname.
- On Windows, unsafe jailed operations deliberately fail closed: no `bash`,
  recursive `glob`/`grep`, `edit`, or replacement of an existing `write`
  target.
- Linux agent startup also fails closed on a filesystem that cannot report a
  stable `statx` birth time for the project root; path/device/inode alone is
  not enough to protect user-owned state from inode reuse.
- Docker Compose files are configuration artifacts; a Docker runtime is not a
  prerequisite for the non-integration test gate and no container-runtime
  performance claim is made here.
- The release does not claim model quality, universal prompt-injection
  protection, semantic-recall quality, latency, throughput, unlimited active
  memory, or package `1.0.0` readiness.

## Verification

Run the platform-appropriate non-integration suites before using a release
candidate. Linux-only jail behavior requires a host with `openat2`; Windows
coverage verifies the native no-reparse boundary and intentionally fail-closed
operations. This [internal security review record](SECURITY_REVIEW-v0.16.1.md)
and the build-artifact checksums must accompany the published release; neither
is a substitute for an external security assessment.

```bash
python -m pytest -q tests -m "not integration"
python -m pytest -q apps/agent-cli/tests -m "not integration"
python -m pytest -q apps/ollama-chat/tests -m "not integration"
```

`0.16.1` is one RC-hardening step toward the contracts described in
[ROADMAP.md](ROADMAP.md), not the final 1.0 release.
