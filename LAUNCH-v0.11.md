# ProtoPrompt 0.11 — launch kit

This versioned file is the source of truth for the public v0.11 message. It
makes no claim about model quality, planning latency, universal prompt-injection
immunity, or an unlimited context window. Every technical statement below is
backed by the released artifact, tests, or documentation linked here.

## Release evidence

- Release: <https://github.com/Idxeed/protoprompt/releases/tag/v0.11.0>
- PyPI: <https://pypi.org/project/protoprompt/0.11.0/>
- English Ledger Recall and composition guide:
  <https://idxeed.github.io/protoprompt/en/ledger-recall/>
- Russian Ledger Recall and composition guide:
  <https://idxeed.github.io/protoprompt/ru/ledger-recall/>
- Exact context-budget guide:
  <https://idxeed.github.io/protoprompt/en/concepts/budget/>
- Reproducible semantic boundary gate:
  `python scripts/run_memory_benchmark.py --suite v0.2 --verify`
- Frozen fixture SHA-256:
  `9bc849dd1d441b2c53d0bad558666b1dd22ad4cf4c8302d5ef5c005102f271c1`

## RU

### Короткий анонс

**ProtoPrompt 0.11: память входит в provider request как проверяемые данные, а
не как скрытые инструкции.**

Experimental `LedgerContextComposer` даёт хосту явный путь от admitted
durable memory к одному точно бюджетированному запросу. Для него обязательны
один `MemoryScope`, тот же экземпляр token counter и strict admission policy.
Raw/legacy `unknown` memory исключается. Выбранные данные рендерятся только в
фиксированное JSON-сообщение роли `user`, перед которым стоит статичный guard
без payload; они не становятся частью generated system context.

Весь data lane резервируется раньше RAG, session и history. Перед возвратом
запроса Ledger повторно валидируется: если `forget`, retract, expiry или
revision change выигрывает race, отправка останавливается с
`StaleMemoryPlanError`.

Вместе с релизом есть воспроизводимый offline gate: 5 сценариев и 17
контрактных проверок. Это доказательство свойств memory/context boundary, не
бенчмарк скорости, качества модели или сравнение с другими фреймворками.

### Для кого

| Аудитория | Релевантное обещание | Следующий шаг |
|---|---|---|
| Python/agent engineers | Recall и request composition имеют один проверяемый host-owned boundary. | Запустить safe flow из Ledger Recall guide. |
| Команды с LangChain/LlamaIndex | Не нужно менять orchestration или RAG; bridge подключается явно. | Обернуть существующий memory path composer-ом. |
| Local Ollama/PDF разработчики | Ledger data не получает system priority при recall. | Сверить data lane и receipt в host-owned integration. |
| Security/platform leads | Есть strict provenance, content-free receipts, exact budget и stale-race fail-close. | Проверить threat model и lifecycle policy. |

### Честные границы FAQ

- Composer не auto-wired в `pp-agent`, Ollama app, legacy `WorkingMemory`,
  profile/session/vector paths и не отправляет запрос к provider сам.
- Это не Python sandbox и не обещание абсолютной защиты от prompt injection:
  arbitrary in-process plugins и прямой SQLite access вне модели угроз.
- После отправки к provider поздний `forget` не может отозвать уже переданный
  payload; хост должен отправить transient request сразу после final validation.
- v0.2 — deterministic semantic regression suite. Он не измеряет latency,
  retrieval/model quality и не доказывает превосходство над LangChain,
  LlamaIndex или другим продуктом.

## EN

### Short announcement

**ProtoPrompt 0.11 puts durable memory into a provider request as accountable
data, not hidden instructions.**

The experimental `LedgerContextComposer` gives the host one explicit path from
admitted durable memory to a fully budgeted request. It requires one
`MemoryScope`, the exact same token-counter instance, and a strict admission
policy. Raw and legacy `unknown` memory is excluded. Selected memory is
rendered only in a fixed user-role JSON message, preceded by a static
payload-free guard; it never becomes generated system context.

The complete data lane is reserved before RAG, session, or history. Before the
request is returned, the Ledger is resolved again: a winning `forget`,
retract, expiry, or revision race stops the send with `StaleMemoryPlanError`.

The release also includes a reproducible offline gate: five scenarios and
seventeen contract checks. It is evidence for memory/context boundary
properties—not a timing, model-quality, or framework-comparison benchmark.

### Audience and CTA

| Audience | Relevant promise | Next step |
|---|---|---|
| Python and agent engineers | Recall and request composition share one auditable host-owned boundary. | Run the safe flow in the Ledger Recall guide. |
| LangChain/LlamaIndex teams | No orchestration or RAG rewrite is required; the bridge is explicit. | Wrap the current memory path with the composer. |
| Local Ollama/PDF builders | Ledger data receives no system priority through recall. | Inspect the data lane and receipt in a host-owned integration. |
| Security and platform leads | Strict provenance, payload-free receipts, exact budgets, and stale-race fail-close are available. | Review the threat model and lifecycle policy. |

### Honest FAQ boundaries

- The composer is not auto-wired into `pp-agent`, the Ollama app, legacy
  `WorkingMemory`, profile/session/vector paths, and it does not send a
  provider request itself.
- This is not a Python sandbox or a promise of universal prompt-injection
  immunity: arbitrary in-process plugins and direct SQLite access are outside
  the stated threat model.
- After a host sends a request to a provider, a later `forget` cannot retract
  that payload; send the transient request immediately after final validation.
- v0.2 is a deterministic semantic regression suite. It measures neither
  latency nor retrieval/model quality, and does not prove superiority over
  LangChain, LlamaIndex, or another product.

## Maintainer launch checklist

The tag, PyPI package, GitHub Release, artifact checksums, bilingual docs, and
offline v0.2 semantic evidence are now published. Before any external post:

1. Reuse the matching RU or EN text above without expanding its documented
   boundary claims.
2. Make the repository About text precise: `Explainable bounded context and
   host-admitted memory for Python LLM apps.` Suggested topics: `agent-memory`,
   `llm-memory`, `context-engineering`, `context-management`, `ollama`, and
   `sqlite`.
3. Capture GitHub Traffic before launch and at D+14; treat it as awareness
   evidence, not a quality metric or a benchmark result.
4. Collect reproducible adoption feedback in one public issue or Discussion:
   Python version, integration path, successful/failed composition flow, and
   missing documentation. Do not request or publish a user's memory payloads.

Success is a repeatable, well-bounded integration—not an unsupported claim
that ProtoPrompt replaces every agent framework.
