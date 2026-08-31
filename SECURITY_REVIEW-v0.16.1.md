# ProtoPrompt 0.16.1 — internal security review

**Release-gate status:** corrective candidate; publication is permitted only
after the tag workflow verifies the same source and artifact checksums.

This is an internal source-level review of the `v0.16.1` corrective candidate.
It is not an external penetration test or a certification. The `v0.16.0` tag
was not published: its Ubuntu release gate caught the Linux-only failures
remediated here.

## Result

No residual P1 or P2 issue was found in the corrective delta. The bounded
second-churn recovery behavior below is an explicit operational limitation,
not a claim of POSIX compare-and-swap semantics. In particular, the Linux
jailed-edit integrity boundary uses these checks:

- Before `RENAME_EXCHANGE`, the source descriptor must still equal the exact
  content and metadata-change fingerprint that the model inspected. This is
  optimistic validation immediately before the exchange, not a pathname CAS
  primitive.
- When the displaced entry remains observable after `RENAME_EXCHANGE`, the old
  source inode is rechecked by device/inode, size, mtime and SHA-256 digest.
  Its `ctime` is deliberately excluded only there because the exchange itself
  changes it. A failed recheck reverses the exact exchange while both entry
  identities remain observable: this restores either the original source or a
  concurrent target replacement. If the displaced entry disappears or changes
  before that proof, the operation is not reported as a verified commit even
  when the staged inode is still pinned at the target.

POSIX offers no inode-conditional pathname exchange. If a second concurrent
rename, an unlinked displaced entry, or a post-commit filesystem failure
races rollback, the operation reports an uncertain outcome and names the
target plus a generated recovery path for explicit inspection. That path may
already have been removed by the concurrent actor. It makes no
last-writer-wins or transactional-abort claim for that irreducible window.

The review also verifies that interactive startup refuses a non-directory or
missing project path, and that the external-symlink grep test proves content
is not traversed rather than treating a user-supplied search query as leaked
file content.

## Evidence

- Fresh native ext4 WSL run: `308 passed, 14 skipped, 10 deselected` for the
  complete non-integration `pp-agent` suite; the exchange-race regression test
  passed independently.
- Windows source-tree run: `273 passed, 49 skipped, 10 deselected` for the
  complete non-integration `pp-agent` suite.
- The release workflow rebuilds the core and CLI wheel/sdist pairs, validates
  checksums, re-runs the Linux suite, and attaches this record to the GitHub
  Release only after the verified PyPI artifacts are visible.

## Boundaries

- This record does not claim universal prompt-injection protection, shell
  sandboxing, model quality, unlimited context/memory, or an external security
  assessment.
- The post-exchange check protects source identity and content when the
  displaced entry remains observable. It is not an inode-CAS guarantee and
  does not validate metadata-only churn in the final check-to-exchange window;
  an approved `bash` command remains a user-rights process, not a sandbox.
- Docker runtime behavior and live third-party provider infrastructure are
  outside this offline source-review scope.
