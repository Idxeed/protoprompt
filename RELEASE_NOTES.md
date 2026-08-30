# protoprompt 0.15.0

ProtoPrompt 0.15 is an RC-evidence release. It adds no provider, model, or
public runtime API. Instead, it makes one narrow strict-Ledger recall contract
reproducible across SQLite and PostgreSQL before the project can consider a
stable `1.0.0` promise.

## Highlights

- **Frozen dual-backend evidence** — `benchmarks/fixtures/v1.0/` contains an
  immutable synthetic corpus, a content-free normalized expected report, and a
  SHA-256-bound manifest. A verified run requires the same semantic result
  from fresh SQLite and PostgreSQL Ledgers; normalizing the backend identifier
  is the only permitted difference.
- **18 deterministic cases** — the fixed matrix exercises delayed active
  recall at 100, 500, and 1,000 records under 1k/2k/4k token budgets and
  independent byte budgets. Each depth also covers supersession, retraction,
  and source revocation.
- **Contract checks, not prompt folklore** — every case verifies strict
  lifecycle selection, tenant/user/thread isolation, whole-record packing,
  token and UTF-8-byte budget compliance, plan/resolve receipt reconciliation,
  content-free `explain()`, and source-revocation payload/source-metadata
  scrubbing.
- **Release-gate wiring** — integration and tag-release CI invoke
  `--suite v1.0 --ledger-backend all --verify`; package verification also
  confirms that the suite, expected baseline, manifest, and runner are present
  in the sdist.

## Compatibility

There are no required application-code or data migrations in 0.15.0. The
core remains zero-required-dependency. PostgreSQL is optional at runtime and
is required only for the two-backend evidence command. The local Ollama/PDF
reference application is released in lockstep:

```bash
pip install "protoprompt[documents,fastapi,ollama]==0.15.0"
pip install "git+https://github.com/Idxeed/protoprompt.git@v0.15.0#subdirectory=apps/ollama-chat"
pp-ollama-chat
```

## Reproduce the evidence gate

Use a disposable local PostgreSQL 17 database and an installed PostgreSQL
extra. The DSN below is illustrative; do not publish a production DSN.

```bash
pip install -e ".[postgres,dev]"
export PROTOPROMPT_POSTGRES_DSN=postgresql://protoprompt:protoprompt@localhost:5432/protoprompt_test
python scripts/run_memory_benchmark.py --suite v1.0 --ledger-backend all --verify
```

`--verify` refuses `sqlite` or `postgres` alone. The two individual backend
reports must exactly match the normalized frozen output, so this is also an
explicit SQLite/PostgreSQL semantic-parity check.

## What the result means

The expected fixture outcome contains 18/18 passing cases with 10/10 checks
per case. Its active delayed-recall denominator is 9 cases: strict Ledger
selects the named target in 9/9 and the declared 20-record tail baseline in
0/9. That number is only **target availability in this named synthetic lexical
fixture**: the query contains target terms and filler records deliberately do
not. It is not a claim about model answers, general semantic recall, external
frameworks, latency, throughput, a 10k-record runtime, prompt-injection
immunity, or unlimited memory.

`v1.0` is the version of the evidence protocol, not a package `1.0.0`
release. The remaining roadmap gates still include a separately measured
10k reference-hardware performance protocol, a held-out quality/conflict
protocol, migration proof, and independently reviewed integrations.

## Explicit boundaries

- This suite tests one strict admission/read path. It does not auto-wire
  `LedgerContextComposer`, `pp-agent`, `pp-ollama-chat`, provider messages, or
  an agent loop.
- Source-revocation evidence proves retraction/scrubbing/exclusion for its
  one fixture record. Atomic multi-record revocation, re-ingest denial, and
  cross-scope source behavior remain covered by the separate Ledger
  conformance/property suite, not by a widened claim here.
- The report deliberately omits payloads, scope values, record IDs, DSNs,
  temporary paths, timestamps, and schema names.
- A local PostgreSQL service is an integration dependency for the proof. The
  release claim is made only after the dedicated PostgreSQL CI service passes.
