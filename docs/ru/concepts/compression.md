# Сжатие сессии

Долгие чаты рано или поздно упираются в контекстное окно. `Pipeline`
решает это, периодически заменяя самые старые ходы vector-friendly
блоком-выжимкой.

## Стратегии

### HeuristicStrategy

Чистый Python, без затрат на LLM. Делит сессию на три области:

- **head** — первые `head_count` сообщений (якорят намерение).
- **tail** — последние `tail_count` сообщений (недавний контекст).
- **important** — сообщения из середины, содержащие ключевое слово
  и достигающие `min_length` символов.

Подходит для: маленьких моделей, чат-ботов с жёсткими задержками,
предсказуемого поведения.

```python
from protoprompt import HeuristicStrategy
strat = HeuristicStrategy(head_count=3, tail_count=5, min_length=80)
```

### LLMSummaryStrategy

Вызывает LLM по разу на каждое скользящее окно, чтобы получить
компактную выжимку. Длина каждого окна ограничена `target_chars_per_block`
символами.

```python
from protoprompt import LLMSummaryStrategy
strat = LLMSummaryStrategy(
    model="llama3.1",
    window_size=8,
    max_blocks=3,
    target_chars_per_block=600,
    language="ru",
    fallback=HeuristicStrategy(),
)
```

!!! warning "Откат при ошибке"
    `LLMSummaryStrategy` глотает ошибки LLM и делегирует стратегии
    `fallback`. Это сделано намеренно: чат не должен ломаться из-за
    тайм-аута модели. Настройте fallback явно, чтобы контролировать
    поведение в деградировавшем режиме.

Подходит для: разговорчивых ассистентов, code-review-ботов, всего,
где середина диалога несёт реальный сигнал, который ключевые слова
пропускают.

## Подключение

```python
from protoprompt import Pipeline

pipeline = Pipeline(
    store, llm,
    strategy=strat,
    compress_every_n=12,
    embedding_model="nomic-embed-text",
)

# В конце каждого хода чата:
if pipeline.should_compress(len(session.messages)):
    await pipeline.compress_and_store(session)
```

Внутри pipeline использует шаблон «сначала пишем, потом удаляем»,
чтобы при крэше хранилище не осталось в полуобновлённом состоянии.

## Выбор `compress_every_n`

| Длина сессии     | Рекомендация      |
|------------------|-------------------|
| ≤ 20 ходов       | никогда           |
| 20–60 ходов      | 10                |
| 60+ ходов        | 8                 |
| Code-review-бот  | 6                 |

Меньшие числа — чаще сжатие (больше затрат на LLM), но плотнее рабочий
контекст. Большие числа — дешевле, но выше риск переполнения.

[English version](../../en/concepts/compression.md)
