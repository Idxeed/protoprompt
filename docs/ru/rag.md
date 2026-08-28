# RAG по документам

RAG (retrieval-augmented generation) — поиск нужных кусков документов по
смыслу и подмешивание их в промпт. В `protoprompt` это отдельный слой
`protoprompt.rag` с двумя половинами:

- **загрузка** — разрезать документ на чанки и положить в стор;
- **чтение** — найти по вопросу лучшие чанки (с provenance).

## Загрузка: DocumentIndexer

Раньше нарезку и эмбеддинги приходилось делать вручную. Теперь всё в одном
вызове:

```python
from protoprompt.rag import DocumentIndexer, FixedSizeChunker

indexer = DocumentIndexer(store, llm, chunker=FixedSizeChunker(512))
await indexer.index("handbook", "Париж — столица Франции. ...")
```

`DocumentIndexer` разбивает текст на чанки, эмбеддит их и кладёт в стор,
помечая каждый чанк меткой `kind="document"` (чтобы при поиске по всему
стору не мешать документы с памятью сессии).

### Чанкеры

| Чанкер              | Принцип                                          |
|---------------------|--------------------------------------------------|
| `FixedSizeChunker`  | Окна фиксированной длины (символы) с перекрытием |
| `ParagraphChunker`  | По пустым строкам, длинные абзацы режет дальше   |
| `TokenChunker`      | Собирает слова до токен-бюджета (нужен `TokenCounter`) |

Все реализуют `ChunkerProtocol.split(text) -> list[str]` — взаимозаменяемы.

## Чтение: Retriever

```python
from protoprompt.rag import Retriever

retriever = Retriever(store, llm)
chunks = await retriever.retrieve(
    "Какая столица Франции?",
    top_k=5,
    doc_ids=["handbook"],      # или None = весь стор
    score_threshold=0.5,       # отсечь слабые совпадения
)
```

Возвращает `RetrievedChunk` — текст **плюс provenance**: `doc_id`,
`index` (номер чанка), `score` (похожесть). Так UI может показать, откуда
пришёл каждый блок.

- `doc_ids=[...]` — искать только в этих документах;
- `doc_ids=None` — искать по всему стору (только `kind="document"`);
- `score_threshold` — отбрасывать чанки с похожестью ниже порога.

### Переранжирование

Векторный top-k — дешёвый первый проход. `RerankerProtocol` уточняет
порядок:

- `NoOpReranker` (по умолчанию) — порядок не меняет, ноль затрат;
- `LLMReranker` — просит модель отсортировать кандидатов; при сбое молча
  возвращает исходный порядок.

```python
from protoprompt.rag import Retriever, LLMReranker

retriever = Retriever(store, llm, reranker=LLMReranker(llm))
```

## В контексте

`ContextBuilder` использует `Retriever` сам — `ContextInput` просто получил
новые поля:

```python
from protoprompt import ContextBuilder, ContextInput

out = await builder.build(ContextInput(
    query="Какая столица Франции?",
    doc_ids=["handbook"],        # None = весь стор
    score_threshold=0.5,
))
print(out.rag_chunks)            # [RetrievedChunk(...), ...] с provenance
```

`out.rag_blocks` остался для совместимости (список текстов).
