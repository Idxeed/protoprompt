# ProtoPrompt 0.20.0 release candidate security review

**Release-gate status:** release-candidate review. This is not an external
penetration test, certification, managed-database recovery approval, or a claim
that 0.20.0 has already been published.

This review covers the v0.20 delta relative to the verified v0.19.0 tag: the
narrow `protoprompt.api` candidate boundary, its packaged manifest and tests,
explicit PostgreSQL setup delegation, version metadata, and documentation.

## Result

No confirmed P0 or P1 issue was found in the delta under the documented local
and disposable-database test models. The change adds no network endpoint,
credential reader, model-controlled scope field, storage plugin registry, SQL
command, or new persistence format.

The security-relevant result is a smaller promise:

- only reviewed core planning/memory names enter `protoprompt.api`;
- the internal `_LedgerCommandBackend` remains nominal and private, so a
  manifest entry cannot authorize an arbitrary third-party backend;
- result-only constructors and private members are explicitly outside the
  contract, reducing accidental reliance on payload-bearing internals;
- custom admission/recall languages, task resume, agent lineage, integrations,
  and apps remain explicitly experimental;
- reading the manifest performs no storage/network/credential operation and
  imports no optional provider SDK;
- a detached manifest object prevents callers from mutating shared process
  contract state.

## Threat review

### Manifest substitution and false trust

`V1_API_MANIFEST_SHA256` covers the canonical decoded JSON structure and CI
checks it against the packaged resource. This catches accidental or partial
source drift. It is **not** a cryptographic publisher signature: an attacker
who can replace both Python code and JSON can replace the constant too. Hosts
must still trust PyPI/GitHub distribution channels and verify published
`SHA256SUMS` where their supply-chain policy requires it.

### API overexposure

The contract test rejects exports whose implementation lives in a declared
experimental namespace. Public dataclass fields are enumerated while `_`
fields are ignored deliberately. `MemoryPolicy.safe_default()` is included;
custom component construction is not. Built-in backend classes expose only
their operational setup/capability/close boundary as stable candidates; normal
lifecycle mutations remain scope-pinned through `MemoryWriter`.

The historical modules remain importable for compatibility. Python cannot
prevent an application from importing private or experimental names; the
boundary is a compatibility and review contract, not a sandbox.

### Optional dependency and import side effects

The contract module imports PostgreSQL type code without importing `psycopg`.
Automated tests replace optional SDK imports with fail-fast guards, then reload
`protoprompt.api` and read its manifest. Wheel/sdist clean-install checks repeat
the lazy-import boundary.

### Scope, authorization, provenance, PII, and erasure

This delta does not change runtime semantics established in prior releases:
`MemoryWriter` pins one non-empty host scope; trust and provenance are distinct;
model/browser input must not receive writer, admission-gate, storage, or task
control-plane authority; payload erasure retains only documented content-free
evidence. `protoprompt.api` is an in-process library boundary, not an auth
system. Network adapters must authenticate a principal and derive scope on the
server rather than accept tenant/user/thread from request bodies.

The contract does not make plaintext memory non-sensitive. Hosts remain
responsible for data minimization, access control, retention policy, encryption
and key custody, logs/backups/WAL/external projection deletion, and applicable
PII obligations. `explain()` safety claims apply only to the documented
content-free receipts, not arbitrary host telemetry or `MemoryRecord.content`.

## Evidence

- Clean Python 3.12 core run: `773 passed, 3 skipped, 32 deselected`.
- Disposable PostgreSQL 17/pgvector Ledger and recovery run: `23 passed, 1`
  environment-specific collation skip.
- Deterministic CLI/Ollama suites: `321 passed, 50` platform/optional skips,
  `10` integration deselected.
- Strict RU/EN docs and wheel/sdist + Twine checks passed; both archives contain
  `protoprompt/api_contract_v1.json`.
- Bandit 1.9.4 on changed production modules found zero issues in the new
  `api.py`. The PostgreSQL file retained two low-severity destructor cleanup
  catches and one previously reviewed B608 query built from a validated,
  quoted owned-schema identifier; the new explicit delegate methods add no SQL.
- GitHub Actions branch run `34729177663` concluded `success` for exact
  pre-version commit `a6ddbe5ed819f61c2f6bd644cc1905a3558ca2dd`.

## Remaining exclusions

This review does not prove managed PostgreSQL backup/restore, PITR, failover,
replica consistency, WAL or physical-media erasure; prompt-injection immunity;
automatic memory truth; unlimited context; a network-service authorization
model; or third-party backend safety. Those claims remain outside 0.20.0 and
the corresponding 1.0 evidence gates remain open.

The final tag workflow must repeat deterministic tests, live PostgreSQL
recovery/concurrency, frozen benchmarks, strict docs, package/clean-import
checks, and artifact digest reconciliation before publication.
