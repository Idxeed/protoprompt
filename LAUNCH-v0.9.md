# ProtoPrompt 0.9 — launch kit

## RU

**ProtoPrompt 0.9 уже здесь: надёжная память агента теперь может попадать в
контекст предсказуемо, с лимитом и проверяемым объяснением.**

`LedgerRecallPlanner` — первый experimental read-path из durable Memory Ledger
к текущей задаче агента. Он выбирает только `active`, host-confirmed и ещё
валидные записи из точного scope, локально ранжирует их без LLM, embeddings,
vector query или сети и возвращает отдельный JSON data lane в точном
token/UTF-8-byte бюджете.

Главное: перед возвратом контекста planner повторно проверяет lifecycle
snapshot. Если факт успели забыть, отозвать, заменить, удалить или он утратил
валидность, операция fail-closed и требует нового плана — в запрос не уходит
тихо устаревшая память. `forget()` не блокируется пользовательским счётчиком
токенов: тяжёлый рендер и accounting происходят вне SQLite writer-lock, а
финальная проверка занимает короткую atomic boundary.

Это не «бесконечный контекст» и не скрытая вставка в system prompt. Это
опциональный, explainable data lane, с которого можно честно строить память
агента, recovery и future context composition.

- Документация: https://idxeed.github.io/protoprompt/ru/ledger-recall/
- Релиз: https://github.com/Idxeed/protoprompt/releases/tag/v0.9.0
- PyPI: https://pypi.org/project/protoprompt/0.9.0/
- Репозиторий: https://github.com/Idxeed/protoprompt

## EN

**ProtoPrompt 0.9 is out: reliable agent memory can now enter context through
a bounded, explainable, and lifecycle-checked path.**

`LedgerRecallPlanner` is the first experimental read path from the durable
Memory Ledger to an agent's current task. It reads only `active`,
host-confirmed, still-valid records from one exact scope; ranks locally without
an LLM, embeddings, vector query, or network call; and returns a separate JSON
data lane within an exact token and UTF-8-byte budget.

Before context is returned, the planner validates the current lifecycle
snapshot again. If a memory was forgotten, retracted, superseded, erased, or
expired, resolution fails closed and requires a new plan instead of silently
sending stale memory. A custom token counter never holds the SQLite writer
lock: rendering/accounting happen outside it, followed by a short final atomic
validation boundary.

This is not a claim of unlimited context or a hidden system-prompt injection.
It is an opt-in, explainable data lane: a dependable foundation for agent
memory, recovery, and future context composition.

- Docs: https://idxeed.github.io/protoprompt/en/ledger-recall/
- Release: https://github.com/Idxeed/protoprompt/releases/tag/v0.9.0
- PyPI: https://pypi.org/project/protoprompt/0.9.0/
- Repository: https://github.com/Idxeed/protoprompt
