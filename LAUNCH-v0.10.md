# ProtoPrompt 0.10 — launch kit

This file is the versioned source of truth for the public v0.10 message. It
does not make model-quality, latency, security-certification, or "unlimited
memory" claims. Every technical statement below is backed by the release
artifact, tests, or documentation linked here.

## Release evidence

- Release: <https://github.com/Idxeed/protoprompt/releases/tag/v0.10.0>
- PyPI: <https://pypi.org/project/protoprompt/0.10.0/>
- English Memory Ledger guide:
  <https://idxeed.github.io/protoprompt/en/memory-ledger/>
- Russian Memory Ledger guide:
  <https://idxeed.github.io/protoprompt/ru/memory-ledger/>
- Bounded Ledger Recall guide:
  <https://idxeed.github.io/protoprompt/en/ledger-recall/>
- Reproducible offline regression gate:
  `python scripts/run_memory_benchmark.py --suite v0.1 --verify`

## RU

### Короткий анонс

**ProtoPrompt 0.10: в долгую память больше не попадает всё подряд.**

Experimental Memory Ledger получил host-owned admission boundary: модельный
transport передаёт только `content`, а scope, origin, policy, evidence и
решение о допуске остаются у хоста. Допущенная запись получает immutable
content-free audit; bounded recall возвращает concrete-origin memory только
при совпадающем evidence.

Это не «бесконечная память», не скрытая вставка в system prompt и не Python
sandbox. Legacy data не объявляется доверенной автоматически. ProtoPrompt
можно использовать рядом с LangChain или LlamaIndex: он не заменяет
orchestration или RAG framework, а закрывает узкий слой governed memory ingress
и explainable bounded context.

Документация · Release · PyPI · safe host-flow example — ссылки выше.

### Для кого

| Аудитория | Релевантное обещание | Следующий шаг |
|---|---|---|
| Python/agent engineers | Хост контролирует scope, origin и решение о допуске памяти. | Запустить safe flow из Memory Ledger guide. |
| Команды с LangChain/LlamaIndex | Не нужно переписывать orchestration/RAG. | Подключить Ledger вокруг существующего memory path. |
| Local Ollama/PDF разработчики | PDF/model input не становится trusted memory автоматически. | Поднять loopback reference app и проверить provenance. |
| Security/platform leads | Есть versioned policy, immutable content-free audit и fail-closed recall. | Оценить threat model и strict migration guide. |

### Честные границы FAQ

- Admission boundary — host capability, а не Python authorization sandbox.
- Arbitrary in-process plugins и прямой SQLite access находятся вне этой
  модели угроз; для более сильной tamper-evidence нужны process isolation или
  external signing.
- Legacy v4 records мигрируют как `legacy_unknown`, без выдуманного audit.
  В strict deployment их нужно quarantine/re-admit.
- Бенчмарк v0.1 — offline semantic regression suite. Он не измеряет качество
  модели, latency или превосходство над другими фреймворками.

## EN

### Short announcement

**ProtoPrompt 0.10: durable memory now has an explicit admission boundary.**

The experimental Memory Ledger adds host-owned admission: model-facing
transport carries only `content`, while scope, origin, policy, evidence, and
the final decision stay with the host. An admitted record receives immutable
content-free evidence, and bounded recall accepts concrete-origin memory only
when that evidence matches.

This is not unlimited memory, hidden system-prompt injection, or a Python
sandbox. Legacy data is not retroactively trusted. ProtoPrompt works alongside
LangChain or LlamaIndex: it does not replace orchestration or RAG frameworks;
it is narrowly focused on governed memory ingress and explainable bounded
context.

Docs · Release · PyPI · safe host-flow example — links above.

### Audience and CTA

| Audience | Relevant promise | Next step |
|---|---|---|
| Python and agent engineers | The host controls memory scope, origin, and admission decision. | Run the safe flow in the Memory Ledger guide. |
| LangChain/LlamaIndex teams | No orchestration or RAG rewrite is required. | Add Ledger around the existing memory path. |
| Local Ollama/PDF builders | PDF/model input never becomes trusted memory automatically. | Run the loopback reference app and inspect provenance. |
| Security/platform leads | Versioned policy, immutable content-free audit, and fail-closed recall. | Review the threat model and strict migration guide. |

### Honest FAQ boundaries

- The admission boundary is a host capability, not a Python authorization
  sandbox.
- Arbitrary in-process plugins and direct SQLite access are outside its threat
  model; stronger tamper-evidence requires process isolation or external
  signing.
- v4 data migrates as `legacy_unknown`, with no invented audit. A strict
  deployment must quarantine and re-admit it.
- Benchmark v0.1 is an offline semantic regression suite. It does not measure
  model quality, latency, or superiority over other frameworks.

## Maintainer launch checklist

The tag, PyPI package, GitHub Release, verified checksums, bilingual docs, and
offline benchmark evidence are complete for v0.10. Before a public post:

1. Reuse the matching RU or EN text above without adding claims outside the
   documented boundary.
2. Snapshot GitHub Traffic before launch and at D+14; treat it as a secondary
   awareness signal, not a release-quality measure.
3. Update the repository About text to `Explainable bounded context and
   host-admitted memory for Python LLM apps.` and add relevant topics such as
   `agent-memory`, `llm-memory`, `context-engineering`,
   `context-management`, `ollama`, and `sqlite`.
4. Collect reproducible adoption feedback in one public issue or Discussion:
   Python version, integration path, successful/failed admission flow, and
   missing documentation. Do not solicit or publish user memory payloads.

Success is not a vanity metric: the next milestone needs reproducible external
feedback, independently validated integrations, and zero unresolved release
gates—not an unsupported claim that ProtoPrompt replaces every framework.
