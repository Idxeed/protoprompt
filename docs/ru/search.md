# Elasticsearch и OpenSearch

`ElasticsearchStore` и `OpenSearchStore` реализуют единый асинхронный контракт
векторного хранилища. Сохраняются семантика replace-on-add, metadata-фильтры,
порог cosine similarity и исходный `doc_id` в metadata.

Адаптер делает только dense-vector retrieval. Гибридный sparse+dense поиск
остаётся в research backlog до появления устойчивого общего контракта.

## Установка и создание индекса

```bash
pip install "protoprompt[elasticsearch]"  # клиент Elasticsearch 9.x
# или
pip install "protoprompt[opensearch]"     # async-клиент OpenSearch 3.1
```

Конструктор не меняет схему сервера. `setup()` вызывается явно при деплое или
миграции:

```python
from protoprompt.integrations import ElasticsearchStore

store = ElasticsearchStore(
    "https://search.example.com:9200",
    index_name="protoprompt-memory-v1",
    dimensions=1536,
    api_key="...",  # передаётся официальному клиенту
)
await store.setup()
```

Параметры официального OpenSearch-клиента передаются аналогично. Для AWS signing
создайте `AsyncOpenSearch` с утверждённым signer и передайте через `client=`.
Внедрённым клиентом владеет host: `close()` закрывает только клиент, созданный
самим адаптером.

Строковые metadata отображаются как `keyword`, поэтому equality и `$in` работают
с точными значениями. OpenSearch использует Lucene HNSW: этот engine поддерживает
inline-фильтрацию k-NN. Оба адаптера пересчитывают cosine similarity по сохранённому
вектору, поэтому `score_threshold` одинаков на обоих серверах.

## Локальный live-тест

В compose отключена аутентификация — конфигурация предназначена **только для
тестов**:

```bash
docker compose -f docker-compose.search.yml up -d --wait
PROTOPROMPT_ELASTICSEARCH_URL=http://localhost:9200 \
PROTOPROMPT_OPENSEARCH_URL=http://localhost:9201 \
pytest -m integration tests/integration/test_search_live.py
docker compose -f docker-compose.search.yml down
```

Пример запускается командой `python examples/search_vector_store.py`. Для порта
`9201` задайте `SEARCH_BACKEND=opensearch`.

## Миграция и откат

Создайте версионированный индекс, заполните его через `DocumentIndexer`, сравните
число записей и выборку запросов, затем переключите конфигурацию приложения. Не
подключайте адаптер к существующему индексу с другой размерностью векторов.

Откат — переключение конфигурации на прошлый индекс. Старый индекс остаётся
read-only до конца периода наблюдения; его удаление — отдельное решение оператора.
`setup()` никогда не меняет существующий mapping.

Поддерживаемые версии клиентов зафиксированы extras. Обновление зависимости
требует contract suite и обоих opt-in live-тестов. Несовместимая линия сервера
получает отдельное ограничение версии или dialect, а не тихое изменение поведения.
