# ProtoPrompt 0.12 — launch kit

This versioned file is the source of truth for the public v0.12 message. It
makes no claim about model quality, planning latency, universal prompt-injection
immunity, agent recovery, exactly-once delivery, or an unlimited context window.
Every technical statement below is backed by the released artifact, tests, or
documentation linked here.

## Release evidence

- Release: <https://github.com/Idxeed/protoprompt/releases/tag/v0.12.0>
- PyPI: <https://pypi.org/project/protoprompt/0.12.0/>
- English Ledger Recall and checkpoint guide:
  <https://idxeed.github.io/protoprompt/en/ledger-recall/>
- Russian Ledger Recall and checkpoint guide:
  <https://idxeed.github.io/protoprompt/ru/ledger-recall/>
- Exact context-budget guide:
  <https://idxeed.github.io/protoprompt/en/concepts/budget/>
- Reproducible sealed-checkpoint semantic gate:
  `python scripts/run_memory_benchmark.py --suite v0.3 --verify`
- Canonical fixture SHA-256:
  `38ab32e37d7f736152710108d0df8a60e9782ef0c94e1cc6e2be7a6a1cb4b1b6`

## RU

### Короткий анонс

**ProtoPrompt 0.12: после restart можно продолжить проверенный выбор памяти —
но не притворяться, что сохранён весь агент.**

Experimental sealed Ledger checkpoint сохраняет лишь strict selection manifest:
opaque host references, policy/counter/budget receipt и private markers
выбранных записей. Task, raw memory, provider messages, tool outputs и
process-local plan не попадают в durable storage. Host держит HMAC-secret вне
SQLite; при resume система заново строит plan с прежними budget/policy/counter
constraints и продолжает только при точном совпадении selection и receipt.

Изменение lifecycle выбранной записи инвалидирует checkpoint и очищает его
private selection markers. Возобновлённая композиция привязана к
`ContextInput.query`, сохраняет bounded data lane v0.11 и проходит финальную
проверку до возврата provider request.

Это не checkpoint agent/provider/workflow state, не lease и не exactly-once
delivery. v0.3 — воспроизводимый offline semantic gate из 4 сценариев и 13
контрактных проверок, а не тест latency, качества модели или сравнение
фреймворков.

### Для кого

| Аудитория | Релевантное обещание | Следующий шаг |
|---|---|---|
| Python/agent engineers | После restart можно заново проверить один strict memory selection без сериализации task/payload. | Выполнить documented host flow и v0.3 gate. |
| Команды с LangChain/LlamaIndex | Не нужно менять orchestration или RAG; boundary остаётся явным и host-owned. | Подключить checkpoint только вокруг существующего strict Ledger path. |
| Local Ollama/PDF разработчики | Durable memory не получает system priority и reference app не меняется скрытно. | Сверить data lane, receipt и lifecycle policy в своей интеграции. |
| Security/platform leads | Есть host-held integrity seal, fail-closed re-plan и lifecycle scrub derived markers. | Проверить secret custody, backup/recovery и threat model. |

### Честные границы FAQ

- Checkpoint не auto-wired в `pp-agent`, Ollama app, legacy `WorkingMemory`,
  provider sessions, RAG, profile/session/vector paths и не отправляет запрос
  к provider сам.
- HMAC защищает manifest лишь пока protected host secret остаётся секретом. Это
  не Python sandbox и не обещание абсолютной защиты от prompt injection:
  arbitrary in-process plugins и прямой SQLite access вне модели угроз.
- После отправки к provider поздний `forget` не может отозвать уже переданный
  payload; host должен отправить transient request сразу после final validation.
- v0.3 не измеряет latency, retrieval/model quality и не доказывает
  превосходство над LangChain, LlamaIndex или другим продуктом.

## EN

### Short announcement

**ProtoPrompt 0.12 resumes a verified memory selection after restart—without
pretending to checkpoint an entire agent.**

The experimental sealed Ledger checkpoint persists only a strict selection
manifest: opaque host references, a policy/counter/budget receipt, and private
markers for the selected records. Task text, raw memory, provider messages,
tool outputs, and a process-local plan never enter durable storage. The host
keeps the HMAC secret outside SQLite; resume makes a fresh plan under the
original budget/policy/counter constraints and proceeds only when selection and
receipt match exactly.

A selected-record lifecycle change invalidates the checkpoint and scrubs its
private selection markers. Resumed composition is bound to `ContextInput.query`,
retains the v0.11 bounded data lane, and performs final validation before a
provider request is returned.

This is not agent, provider, or workflow-state recovery; it provides no lease
or exactly-once delivery. v0.3 is a reproducible offline semantic gate with
four cases and thirteen contract checks—not a latency, model-quality, or
framework-comparison benchmark.

### Audience and CTA

| Audience | Relevant promise | Next step |
|---|---|---|
| Python and agent engineers | Revalidate one strict memory selection after restart without serializing task/payload. | Run the documented host flow and v0.3 gate. |
| LangChain/LlamaIndex teams | No orchestration or RAG rewrite is required; the boundary stays explicit and host-owned. | Place the checkpoint only around an existing strict Ledger path. |
| Local Ollama/PDF builders | Durable memory receives no system priority and the reference app does not change implicitly. | Inspect the data lane, receipt, and lifecycle policy in a host integration. |
| Security and platform leads | A host-held integrity seal, fail-closed re-plan, and lifecycle scrubbing of derived markers are available. | Review secret custody, backup/recovery, and the threat model. |

### Honest FAQ boundaries

- Checkpoints are not auto-wired into `pp-agent`, the Ollama app, legacy
  `WorkingMemory`, provider sessions, RAG, profile/session/vector paths, and
  they do not send a provider request themselves.
- HMAC protects a manifest only while the protected host secret stays secret.
  This is not a Python sandbox or a promise of universal prompt-injection
  immunity: arbitrary in-process plugins and direct SQLite access are outside
  the stated threat model.
- After a host sends a request to a provider, a later `forget` cannot retract
  that payload; send the transient request immediately after final validation.
- v0.3 measures neither latency nor retrieval/model quality, and does not prove
  superiority over LangChain, LlamaIndex, or another product.

## Maintainer launch checklist

The tag, PyPI package, GitHub Release, artifact checksums, bilingual docs, and
offline v0.3 semantic evidence must be published before any external post.

1. Reuse the matching RU or EN text above without expanding its documented
   boundary claims.
2. Keep the repository About text precise: `Explainable bounded context and
   host-admitted memory for Python LLM apps.` Suggested topics: `agent-memory`,
   `llm-memory`, `context-engineering`, `context-management`, `ollama`, and
   `sqlite`.
3. Capture GitHub Traffic before launch and at D+14; treat it as awareness
   evidence, not a quality metric or a benchmark result.
4. Collect reproducible adoption feedback in one public issue or Discussion:
   Python version, integration path, successful/failed resume flow, and missing
   documentation. Do not request or publish a user's memory payloads.

Success is a repeatable, well-bounded restart boundary—not an unsupported claim
that ProtoPrompt replaces every agent framework or stores an entire agent.
