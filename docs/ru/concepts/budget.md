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

## Подсчёт токенов

`RegexTokenCounter` по умолчанию быстрый, без зависимостей и
мультиязычный. Для точных подсчётов установите extra `tiktoken`:

```bash
pip install "protoprompt[tiktoken]"
```

```python
from protoprompt.tokens import TiktokenCounter

counter = TiktokenCounter(model="gpt-4o-mini")
# или
counter = TiktokenCounter(encoding="cl100k_base")
```

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

[English version](../../en/concepts/budget.md)
