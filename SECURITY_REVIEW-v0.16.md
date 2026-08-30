# ProtoPrompt 0.16.0 — internal security review

**Release-gate status:** passed on 2026-08-31.

This is an internal, source-level release review of the `v0.16.0` candidate.
It is not an external penetration test or a certification. Its scope is the
working-tree change set from `7b379e338d132d1c22441aabc3dc631d5c7e89a0` to the
release candidate, with emphasis on the local `pp-agent` authority boundary.

## Result

No residual P1, P2, or P3 issue was found after remediation and an independent
final pass. The review specifically rechecked these previously reportable P2
classes:

- `/git` now uses the permissioned, descriptor-pinned `ToolRunner` path rather
  than a separate pathname-based subprocess.
- A confirmed jailed Bash command cannot start after its project root is
  replaced: Linux uses an inherited pinned-FD working directory; Windows and
  non-Linux POSIX deliberately fail closed.
- Project identity combines filesystem identity with stable creation generation
  and a live root descriptor, preventing state inheritance after inode reuse.
- Consent renders every executable action field completely, rejects oversized
  requests and rechecks the action fingerprint after asynchronous input.
- Model, tool, file, trace and startup-menu text crosses a terminal-safe
  boundary before it can be displayed next to a consent prompt.
- Provider credentials remain constrained to documented `PP_*` variables; the
  reusable HTTP/Ollama clients default to `trust_env=False`.

## Evidence

The final candidate passed the following local, non-integration checks:

- `272 passed, 42 skipped, 10 deselected` — full `pp-agent` suite;
- `601 passed, 3 skipped, 26 deselected` — core suite;
- `24 passed, 1 skipped` — Ollama/PDF reference-app suite;
- targeted independent security suite: `201 passed, 42 skipped`;
- `py_compile`, `git diff --check` (only expected CRLF conversion warnings),
  wheel/sdist `twine check`, and clean-venv `pp-agent --version` / `pip check`.

The release workflow rebuilds the core and CLI artifacts, verifies their
checksums, and attaches this review record alongside the public artifacts.

## Boundaries of this review

- It does not claim universal prompt-injection protection, sandboxing of an
  approved shell command, model quality, or unlimited context/memory.
- Docker runtime behavior was not available in the local validation
  environment and is not a release performance or runtime claim.
- Live provider integrations, third-party infrastructure, and an external
  adversarial assessment remain outside this offline source-review scope.
