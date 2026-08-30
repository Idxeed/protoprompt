# Экспериментальный memory ledger

Основа v0.8 превращает долговременную память из безличного vector chunk в
запись со scope и явным жизненным циклом. Это намеренно **opt-in** и пока
экспериментально: новый ledger не меняет `MemoryService`, профиль, сжатие
сессий, vector recall и `ContextPlan`, пока хост не подключит отдельный
адаптер.

Такое разделение принципиально: текст из PDF, tool result, транскрипта или
LLM extraction не может стать доверенным фактом с system-priority лишь потому,
что его сохранили.

## Быстрый старт

Инициализация схемы — действие оператора, а не побочный эффект импорта:

```python
from protoprompt.ledger import MemoryWriter, SqliteMemoryLedger
from protoprompt.scope import MemoryScope

ledger = SqliteMemoryLedger("memory-ledger.db")
print(ledger.dry_run_setup())  # ничего не записывает
ledger.setup()                 # явный, идемпотентный setup schema

writer = MemoryWriter(
    ledger,
    scope=MemoryScope(tenant="local", user="alice", thread="chat-42"),
)

candidate = writer.propose(
    kind="preference",
    content="Пользователь предпочитает ответы на русском.",
    source_ref="turn:42",          # opaque ID от хоста, не исходный текст
    evidence_refs=("turn:42:line:1",),
)

# Это решение принимает trusted host policy/reviewer. Не передавайте writer и
# raw ledger недоверенному plugin- или model-tool-коду.
active = writer.confirm(
    candidate.record_id,
    expected_revision=candidate.revision,
)

assert writer.list_active() == [active]
```

`MemoryWriter` создаётся с одним непустым `MemoryScope`, принадлежащим хосту.
Его mutating-методы не принимают tenant, user, thread, lifecycle state или
trust level. У низкоуровневого `SqliteMemoryLedger` этот же точный scope нужен
для каждого действия. Это API-ограничение внутри доверенного процесса хоста,
а не Python sandbox и не authorization boundary: не выдавайте ни writer, ни
ledger недоверенному коду.

## Жизненный цикл и recall

| Состояние | Как появляется | Default recall |
|---|---|---|
| `candidate` | `propose()` / `assert_candidate()` | никогда |
| `active` | явный `confirm()` | только при валидности и наличии payload |
| `superseded` | явный `supersede(old, replacement)` | никогда |
| `retracted` | `retract()` или `forget()` | никогда |
| `expired` | `expire()` / `expire_due()` | никогда |
| `quarantined` | `quarantine()` | никогда |

Каждый переход принимает `expected_revision`: устаревшая команда не сможет
молча затереть новое решение. `forget()` записывает событие `forgotten` и
увеличивает revision даже у уже `retracted` record: удаление payload
инвалидирует stale cache, а `retracted_at` остаётся временем первого выхода из
recall.

Можно хранить `event_id` при retry. Он идемпотентен, пока исходная команда ещё
addressable; повтор завершённого `forget()` вернёт сохранённый erasure receipt.
Повтор candidate после удаления его payload или любая команда после hard erase
намеренно отклоняются, а не воскрешают данные. Повтор одного ID с другим
вводом всегда вызывает conflict.

Замена факта всегда явная и детерминированная:

```python
new = writer.confirm(new_candidate.record_id, expected_revision=1)
old = writer.supersede(
    old_active.record_id,
    replacement_record_id=new.record_id,
    expected_revision=old_active.revision,
    expected_replacement_revision=new.revision,
)
```

У replacement появляется typed relation `supersedes` в том же scope. Нет ни
cross-scope relation, ни неявной эвристики «последний факт победил».

## Retention и удаление

Операционная история событий не содержит plaintext памяти, source ID,
evidence ID или content fingerprint. Сам plaintext и opaque provenance
находятся в отдельной локальной payload-записи; setup v4 явно очищает legacy
creation fingerprints из ранних experimental schema.

- `retract()` сразу исключает record из recall, но оставляет локальный payload
  для ревью.
- `forget()` переводит record в `retracted`, удаляет локальный plaintext,
  source/evidence payload, source lookup и relation, а также redacts его
  stored content fingerprint. Остаются content-free событие `forgotten` и
  lifecycle receipt.
- `forget_by_source("pdf:opaque-id")` атомарен для всех найденных records в
  точном scope writer’а. Он также сохраняет scoped opaque source tombstone:
  тот же source нельзя ingest’ить снова в этом scope, но он независим в другом.
- `erase()` — явный необратимый локальный escape hatch: удаляет record,
  payload, relation и его event receipts. Он также redacts ссылки на этот
  record из событий других records. Остаются scoped opaque replay tombstones
  и hard-erase receipt, чтобы in-flight retry не воскресил тот же record ID;
  для новой памяти используйте новый record ID.

В первом срезе нет адаптера внешней vector/FTS projection. Поэтому ни
`forget()`, ни `erase()` не обещают удаление из отдельной vector DB: адаптер
обязан получить durable подтверждение удаления, прежде чем давать такую
гарантию. Обе операции меняют live rows ledger, но SQLite не гарантирует
стирание из старых backup, WAL/journal-файлов или с физического носителя; если
это важно, нужны encryption/key destruction и политика хранения бэкапов.

## Границы безопасности

- Content ограничен 16 000 символами; opaque ID и references ограничены и не
  принимают whitespace или многострочный raw text.
- `source_ref` и `evidence_refs` — minted хостом ID происхождения. Не кладите
  туда filename, URL с секретом, prompt или тело документа.
- `content_hash` — scope-separated operational fingerprint, а не
  криптографический audit и не password primitive. После `forget()` он
  redacted. SQLite event log не tamper-evident.
- `export()` по умолчанию исключает plaintext; `include_content=True` годится
  только для явного защищённого export flow.

## Миграция и откат

По умолчанию используйте отдельный SQLite-файл ledger. Его можно разместить в
файле с протестированным legacy `SqliteStore`: он никогда автоматически не
импортирует `chunks`, профили или session summaries. Имена таблиц, explicit
index и event-trigger reserved, а не глобально namespaced. Schema extension
points здесь нет: `dry_run_setup()` и `setup()` принимают только точные
ledger-owned table/index definitions и отклоняют внешние indexes или triggers,
которые нацелены на ledger tables, вместо adoption, overwrite или их
незаметного запуска.

1. Запустите `dry_run_setup()` и сделайте backup через `ledger.backup(path)`.
2. Вызовите `setup()` из явной migration job.
3. Оставьте legacy readers authoritative, пока проверяете отдельный opt-in
   adapter/importer.
4. Для rollback остановите записи в ledger и верните traffic к старым
   компонентам; не делайте destructive downgrade общей БД.

Importer для profile/session/vector — следующая работа v0.8. Эта основа
специально сохраняет всё текущее публичное поведение без изменений.
