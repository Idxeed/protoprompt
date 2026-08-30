# FastAPI memory service

Extra `[fastapi]` публикует scoped `MemoryService` через небольшой HTTP API.
Аутентификация, получение scope и создание service задаются обязательными
host-callback'ами; tenant, user и thread отсутствуют в request body.

```bash
pip install "protoprompt[fastapi]"
export PROTOPROMPT_API_KEY='replace-with-a-random-test-key'
python examples/fastapi_memory_service.py
```

```bash
curl -H "Authorization: Bearer $PROTOPROMPT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"memory_id":"renewal","text":"Договор продлевается в мае"}' \
  http://localhost:8000/v1/memories

curl -H "Authorization: Bearer $PROTOPROMPT_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"query":"Когда продлевается договор?"}' \
  http://localhost:8000/v1/memories/search
```

Пример работает офлайн на детерминированных demo embeddings. В production
замените их и SQLite на provider embeddings и асинхронный production store.
SQLite обёрнут через `as_async`, поэтому disk I/O не блокирует event loop.

## Граница безопасности

У `create_fastapi_memory_app(service_factory, scope_resolver, authorize)` нет
небезопасного default, доверяющего `X-Tenant` или пользовательскому вводу. В
production `authorize` проверяет JWT/session/mTLS identity и кладёт доверенные
claims в `request.state`; `scope_resolver` строит `MemoryScope` только из них.
Адаптер дополнительно проверяет, что полученный `MemoryService` закреплён ровно
за этим scope.

Неизвестные поля запрещены; ограничены text, identifiers, `top_k`, размер
metadata и score threshold. Ограничьте общий размер body и request timeout ещё и
на reverse proxy. `/healthz` не раскрывает backend/tenant и остаётся единственным
маршрутом без авторизации. Explain не содержит recalled text.

API включает remember/search/forget, manifest/explain/budget report и
опциональные profile endpoints. Profile signal отвечает `409`, если host не
настроил `ProfileManager`. Если он передан, его host-owned scope должен точно
совпадать со scope у `MemoryService`; при несовпадении конструктор завершится
до первого profile I/O.

## Lifespan и деплой

Передайте FastAPI lifespan context: в нём открываются connection pools до начала
трафика и закрываются только host-owned ресурсы. Локальный пример закрывает
SQLite именно в lifespan.

Kubernetes-рецепт намеренно однорепличный из-за SQLite:

```bash
docker build -f examples/fastapi/Dockerfile -t protoprompt-fastapi:local .
minikube image load protoprompt-fastapi:local
kubectl create secret generic protoprompt-api \
  --from-literal=api-key='replace-with-a-random-test-key'
kubectl apply -f examples/fastapi/k8s.yaml
kubectl port-forward service/protoprompt-memory 8000:80
```

Для нескольких реплик замените SQLite на PostgreSQL/pgvector и общий
profile/session backend. Не масштабируйте этот manifest горизонтально поверх
ReadWriteOnce SQLite volume.

## Миграция и откат

Сначала подключите HTTP app рядом с существующим процессом и направьте на него
маленькую авторизованную когорту. До копирования памяти сравните mapping scope с
текущими auth claims. Для отката уберите route и верните трафик in-process
адаптеру; данные backend не меняются.

FastAPI остаётся optional recipe, а не обязательным core server. Расширение
диапазона версий требует ASGI-тестов isolation, validation, lifespan и
missing-extra. Ломающие изменения маршрутов получают новый prefix `/vN`.
