# Очистка payload в точном scope (кандидат v1)

`MemoryWriter.payload_readback()` и `MemoryWriter.purge_payloads()` —
экспериментальный host-only контракт удаления для одного уже закреплённого
`MemoryScope`. Это ещё не стабильная гарантия 1.0: релиз вправе заявлять этот
контракт только при наличии собственной документированной local/backend
верификации.

Операция нужна, когда хост должен удалить live payload Ledger для одной
границы клиента/контекста, не перебирая предварительно record ID. Это намеренно
не заявление об удалении целой БД, backup, переписки или данных у провайдера.

## Узкая публичная поверхность

Scope фиксируется, когда доверенный хост создаёт writer; ни один из методов не
принимает tenant, user, thread или произвольный scope от модели либо plugin.

```python
# ``writer`` создан trusted host code ровно для одного MemoryScope.
before = writer.payload_readback()

receipt = writer.purge_payloads(
    "deletion-request:opaque-host-id",
    reason_code="scope_payload_purged",  # значение по умолчанию
)

assert receipt.readback.is_empty
assert writer.payload_readback().payload_record_count == 0
```

`payload_readback()` возвращает content-free `ScopePayloadReadback`: opaque,
детерминированный fingerprint scope и число записей с payload, существующих в
данный момент в **точном** scope. Он не перечисляет record ID, sources,
evidence, content hash, plaintext или поля scope.

`purge_payloads(operation_id, *, reason_code="scope_payload_purged")`
возвращает content-free `ScopePayloadPurgeReceipt`. В receipt есть только
host-minted opaque operation ID, агрегатные счётчики удаления, тот же opaque
fingerprint scope и его финальный readback. В нём нет содержимого памяти или
source reference.

`operation_id` — durable ключ повтора, а не идентификатор для пользователя.
Создавайте его в trusted host code, сохраняйте рядом с запросом на удаление и
повторно используйте **то же** значение после неопределённого timeout, падения
процесса или restart. Зафиксированная операция сверяется по своему content-free
durable receipt; нельзя выпускать второй ID лишь потому, что первый ответ не
был получен. В другом scope тот же ID живёт в отдельном operation namespace:
Ledger не объединяет и не cross-read-ит такие операции. В том же точном scope
повтор ID для другой команды — conflict. Хосту всё равно стоит mint-ить
глобально уникальные ID, чтобы собственный deletion log не спутал два scope.

## Что удаляется атомарно

В одной Ledger write transaction операция находит все записи с payload в точном
scope writer. Она охватывает все логические lifecycle states:

| State | Включается, если payload ещё есть? |
|---|---|
| `candidate` | да |
| `active` | да |
| `superseded` | да |
| `retracted` | да |
| `expired` | да |
| `quarantined` | да |

Для каждой такой записи применяется обычный forgotten lifecycle path и
удаляются локальные plaintext/content payload, source/evidence payload data,
scoped source lookup data и relations. Content-free lifecycle/audit history,
нужная для предотвращения тихого resurrection, намеренно не становится
payload-export. Запись, у которой payload уже был forgotten, не получает его
снова.

Команда commit-ится, только если её финальный readback для точного scope пуст.
Ошибка команды или смерть процесса до commit оставляют после reopen
наблюдаемым состояние до команды; успешный aggregate receipt для частичной
очистки недопустим. Поэтому у успешного receipt на границе его transaction
выполняется `receipt.readback.payload_record_count == 0`.

Это логическая операция над live Ledger, а не `erase()` всех audit/event rows.
`erase()` остаётся явным per-record hard-delete escape hatch со своей
семантикой.

## Обязательная deletion fence хоста

Ledger сериализует собственную мутацию, но не владеет identity system,
очередями, browser sessions или ingress adapters вашего приложения. До purge
хост обязан создать собственную deletion/ingress fence для соответствующих
principal и scope:

1. остановить или отклонить новые candidate/admission writes для этого scope;
2. завершить либо отменить in-flight host writes, которые могли бы добавить
   payload сразу после transaction Ledger; и
3. сохранить deletion request и operation ID, затем сверить финальный
   receipt/readback до объявления успеха клиенту.

Без этой границы другой trusted writer может создать payload уже после commit
purge transaction. Такая новая запись не означает сбой exact-scope readback:
это application-level race, которую обязан закрыть хост.

Не выдавайте `MemoryWriter`, `MemoryReviewGate`, ingress object writer или
генерацию `operation_id` модели, browser client либо недоверенному in-process
plugin. Scope и старт удаления определяет хост, а не LLM.

## Явные ограничения

Операция **не** удаляет задним числом текст, уже отправленный модели,
сохранённый в provider request/response, показанный в chat UI или попавший в
conversation archive приложения. Для этих систем нужен собственный workflow
удаления.

Она также удаляет лишь live canonical Ledger payload и локальные derived rows.
Она не обещает физического стирания из SQLite WAL/journal, PostgreSQL WAL,
replica, snapshot, backup, дисков, логов или physical media. Если это свойство
важно, используйте encryption и policy хранения backup/уничтожения ключей.

В этом контракте у core Ledger нет external vector/FTS projection. Если
адаптер пишет в другой index, он обязан удалить данные и получить durable
acknowledgement этого удаления до заявления хоста о полном end-to-end erasure.
