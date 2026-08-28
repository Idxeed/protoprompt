# Миграция с 0.3 на 0.4

Ветка 0.4 сохраняет публичные вызовы 0.3. Старый composite-клиент с методами
`chat()` и `embed()` по-прежнему принимается везде, а данные без scope остаются
доступны под прежними `doc_id`.

## Раздельные chat и embeddings

Старый код менять не обязательно:

```python
pipeline = Pipeline(store, llm)
```

Если провайдеры разные, передайте две capability явно:

```python
pipeline = Pipeline(
    store,
    chat_client=chat_client,
    embedding_client=embedding_client,
)
```

`LLMClientProtocol` оставлен как composite (`ChatClientProtocol` +
`EmbeddingClientProtocol`). Собрать его вручную можно через
`CompositeLLMClient`.

## Включение MemoryScope

Без `scope` физические ключи не меняются. Для постепенной миграции создайте
scoped-объекты только для новых tenant или потоков:

```python
scope = MemoryScope(tenant="acme", user="u-42", thread="chat-7")
indexer = DocumentIndexer(store, embedding_client, scope=scope)
builder = ContextBuilder(store, embedding_client, scope=scope)
```

Записи, созданные без scope, автоматически не копируются в новый namespace.
Если требуется перенести существующую память, прочитайте её старым объектом и
переиндексируйте scoped-индексатором. Не смешивайте scoped writer с unscoped
reader: отсутствие scope намеренно означает legacy/global namespace.

Для прямого удаления через `StoreProtocol` используйте
`scoped_doc_id(logical_id, scope)`. Высокоуровневые компоненты преобразуют ID
сами.

## Проверка собственного адаптера

Contract kit доступен без pytest:

```python
from protoprompt.testing import check_embedding_client, check_vector_store

await check_embedding_client(my_embedding_client)
await check_vector_store(my_isolated_test_store)
```

Проверки используют временные ключи и удаляют их после выполнения. Для
production-стора всё равно лучше выделить тестовую collection/schema.
