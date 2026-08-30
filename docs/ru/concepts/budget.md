# Токен-бюджет

`ContextBuilder` без ограничений соберёт промпт и на 50 000 токенов,
если позволить. `TokenBudgetedContextBuilder` задаёт жёсткий потолок.

## Приоритеты

Секции заполняются в следующем порядке по умолчанию:

1. **system** — никогда не обрезается; если не влезает один, выбрасывается `TokenBudgetExceededError`.
2. **profile** — молча отбрасывается, если не влезает.
3. **session** — блоки обрезаются по границам слов.
4. **rag** — то же.

Порядок можно переопределить через аргумент `priorities` конструктора.

## Как работает обрезка

Сборщик запрашивает у хранилища `top_k * 2` кандидатов, затем идёт
по ним в порядке убывания score. Блоки, которые влезают целиком,
сохраняются. Первый блок, который не влезает, обрезается до последней
границы слов, где ещё помещается, и в конец добавляется `…`. Остальные
блоки этой секции и все последующие секции отбрасываются.

## Наблюдаемость

`ContextOutput.budget_report` всегда заполняется бюджетированным
сборщиком:

```python
out = await builder.build(inp)
print(out.budget_report.used_tokens, "/", out.budget_report.budget)
print("dropped:", out.budget_report.dropped_blocks)
print("per-section:", out.budget_report.section_tokens)
```

## Полный budget запроса

`build()` ограничивает только собранный системный контекст. Если приложение
само добавляет историю и текущий turn, используйте `build_messages()` — это
безопасный API уровня полного запроса. Он учитывает system context, provider
framing, сохранённые history items и обязательный финальный turn в одном
лимите.

```python
messages = await builder.build_messages(
    ContextInput(query=question, system_prompt="Отвечай кратко."),
    history=history,
    user_message=question,
    output_reserve=1_024,
)
```

`output_reserve` оставляет место для ответа модели. Если обязательный финальный
turn вместе с reserve не помещается, выбрасывается `TokenBudgetExceededError`;
он не обрезается молча. Для интеграций, где текущий turn состоит из нескольких
portable message items (например, tool call), передайте `final_messages`.
Такие items должны содержать JSON-совместимые данные: structured content,
`tool_calls`, `tool_call_id` и прочие поля включаются в оценку.

В сохранённой OpenAI-style history assistant item с `tool_calls` и подходящие
ему результаты `tool` остаются одной атомарной группой. History Agents/Responses
обрабатывается как непрерывный граф зависимостей: пары call/output (включая
hosted MCP approval и допустимый SDK анонимный server-side tool search),
потоковые shell/tool-search outputs, program-owned children и предшествующие
reasoning items сохраняются или отбрасываются вместе. Это поддерживает
переплетённые program invocations без
перестановки items. Input controls `compaction_trigger` и `item_reference` не
попадают в optional history, а reasoning остаётся только с настоящим
model-emitted follower.

Если обязательный final input начинается с tool output, весь его trailing
history graph резервируется как обязательный контекст. Если эта зависимость не
помещается, builder выбрасывает `TokenBudgetExceededError`, а не отдаёт
orphaned final output. Анонимный **server-side** tool-search output
сопоставляется с history call по порядку SDK через эту границу; для client-side
tool search обязателен `call_id`.

## Ledger data lane *(experimental)*

Обычный `TokenBudgetedContextBuilder` намеренно не принимает произвольные
host messages: это защищает public request API от скрытой смены role или
placement. Явный `LedgerContextComposer` — единственный встроенный bridge для
admitted Ledger recall — резервирует фиксированную пару system guard + `user`
JSON data до optional RAG/session/history. Недостаток места для otherwise
допустимого обязательного lane сообщает `TokenBudgetExceededError` с section
`ledger_data`; independently oversized final turn или tool dependency сохраняет
свой обычный section.

В request-plan `ContextPlan.data_lanes` хранит content-free
`ContextDataLaneReceipt`, а `BudgetReport.section_tokens["ledger_data"]`
показывает transport cost. Raw payload существует только в
`plan.render_messages()`, никогда в `plan.explain()`. Точным total остаётся
`ContextRequestReceipt.input_tokens`, а не сумма объяснительных lane costs.

## Объяснимый план и receipt запроса

Для developer UI, audit record или конкурентных запросов используйте новый
additive planning API, а не mutable compatibility-свойство `last_report` после
завершения вызова:

```python
plan = await builder.plan_messages(
    ContextInput(query=question, system_prompt="Отвечай кратко."),
    history=history,
    user_message=question,
    output_reserve=1_024,
)

messages = plan.render_messages()
receipt = plan.receipt
assert receipt is not None
print(receipt.input_tokens, "+", receipt.output_reserve_tokens)
print(plan.explain())
```

`ContextPlan` immutable и хранит глубокий JSON-compatible snapshot итоговых
provider messages. `ContextRequestReceipt` сообщает точный результат
`count_messages()` для этого конкретного запроса, а также totals context,
сохранённой history, final input, reserve и оставшегося budget. Поздний запрос
на том же builder не может изменить уже возвращённые plan или receipt.

`plan.explain()` содержит только decision metadata: id блока, origin,
стабильный reason code, marginal token cost и opaque per-builder reference на
RAG source/score, если они есть. В нём намеренно нет текста prompt/document,
final messages, идентификаторов session и raw document id, поэтому его можно
сериализовать в developer trace.

Для context-only inspection используйте `await builder.plan(inp)` — у такого
плана нет request receipt, а system context доступен через
`plan.render_system_prompt()`. Legacy APIs остаются совместимыми: `build()`
кладёт context-only plan в `ContextOutput.plan`, а `build_messages()` рендерит
ту же projection, что и `plan_messages()`.

## Подсчёт токенов

`RegexTokenCounter` по умолчанию быстрый, без зависимостей и
мультиязычный. Опциональный `TiktokenCounter` даёт model-aware локальную
оценку текста и детерминированный message framing:

```bash
pip install "protoprompt[tiktoken]"
```

```python
from protoprompt.tokens import TiktokenCounter

counter = TiktokenCounter(model="gpt-4o-mini")
# или
counter = TiktokenCounter(encoding="cl100k_base")
```

Потолок жёсткий в единицах выбранного счётчика. Wire format и лимиты модели
могут меняться, поэтому для точных или billable подсчётов на границе запроса
используйте нативный provider `count_tokens`, а response limit provider'а
согласуйте с `output_reserve`.

Можно подключить и собственную реализацию — протокол описывает
ровно два метода:

```python
from protoprompt.tokens import TokenCounter

class MyCounter:
    def count(self, text: str) -> int: ...
    def count_messages(self, messages: list[dict]) -> int: ...
```

## Когда НЕ использовать

- Для коротких одноразовых промптов, где переполнение невозможно — обычный `ContextBuilder` дешевле.
- Когда реальная токенизация модели радикально отличается от любой разумной эвристики (например, модели speech-to-text) — передайте свой счётчик.

[English version](/en/concepts/budget/)
