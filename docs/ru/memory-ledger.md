# Экспериментальный memory ledger

v0.10 превращает долговременную память из безличного vector chunk в запись с
прикреплённым scope, явным жизненным циклом, неизменяемым происхождением входа
и host-owned admission-решением. Это намеренно **opt-in** и пока
экспериментально: новый ledger не меняет `MemoryService`, профиль, сжатие
сессий, vector recall и `ContextPlan`, пока хост не подключит отдельный
адаптер. В v0.11 таким отдельным адаптером стал узкий experimental
`LedgerContextComposer` для admitted Ledger recall; он не включает Ledger
глобально и не меняет legacy paths. Schema v6 из v0.12 добавляет optional
sealed recall-checkpoint manifest для этого явного lane, но не сериализует
agent state и не подключает Ledger к приложению автоматически.

Такое разделение принципиально: текст из PDF, tool result, транскрипта или
LLM extraction не может стать доверенным фактом с system-priority лишь потому,
что его сохранили.

![Жизненный цикл Memory Ledger: candidate, trusted confirmation, active recall, lifecycle exit](assets/memory-ledger-lifecycle.svg)

## Быстрый старт

Инициализация схемы — действие оператора, а не побочный эффект импорта:

```python
from protoprompt.ledger import (
    MemoryAdmissionAction,
    MemoryAdmissionPolicy,
    MemoryKind,
    MemoryOrigin,
    MemoryReviewGate,
    MemoryWriter,
    SqliteMemoryLedger,
)
from protoprompt.scope import MemoryScope

ledger = SqliteMemoryLedger("memory-ledger.db")
print(ledger.dry_run_setup())  # ничего не записывает
ledger.setup()                 # явный, идемпотентный setup schema

writer = MemoryWriter(
    ledger,
    scope=MemoryScope(tenant="local", user="alice", thread="chat-42"),
)

# Хост фиксирует authority-bearing поля до поступления недоверенного текста.
# Пустой MemoryAdmissionPolicy по умолчанию quarantine-ит любой origin.
policy = MemoryAdmissionPolicy(
    policy_id="local-document-v1",
    policy_version="1",
    allowed_origins=(MemoryOrigin.DOCUMENT,),
    minimum_confidence=0.8,
)
gate = MemoryReviewGate(writer, origin=MemoryOrigin.DOCUMENT, policy=policy)
document_ingress = gate.ingress(
    kind=MemoryKind.PREFERENCE,
    source_ref="turn:42",          # opaque ID от хоста, не исходный текст
    evidence_refs=("turn:42:line:1",),
    confidence=0.9,
)
candidate = document_ingress.submit("Пользователь предпочитает ответы на русском.")

# review() ничего не пишет; sealed result применяет только trusted host code.
review = gate.review(candidate.record_id)
assert review.action is MemoryAdmissionAction.ALLOW
active = gate.confirm(review, event_id="admission:turn-42:allow")

assert writer.list_active() == [active]
```

`MemoryWriter` создаётся с одним непустым `MemoryScope`, принадлежащим хосту.
Его mutating-методы не принимают tenant, user, thread, lifecycle state или
trust level. У низкоуровневого `SqliteMemoryLedger` этот же точный scope нужен
для каждого действия. Это API-ограничение внутри доверенного процесса хоста,
а не Python sandbox и не authorization boundary: не выдавайте ни writer, ни
ledger недоверенному коду.

## PostgreSQL deployment (экспериментальный)

`PostgresMemoryLedger` даёт ту же доверенную синхронную command-поверхность
Ledger, что и `SqliteMemoryLedger`, но это отдельно provisioned backend.
Установите `protoprompt[postgres]`, запустите его явные `dry_run_setup()` и
`setup()` из migration job и выделите пустую PostgreSQL schema только для него.
Он принимает лишь свежую schema v6: старый PostgreSQL Ledger не мигрируется, а
SQLite Ledger file не импортируется.

Каждая запись в этой schema получает transaction-scoped advisory lock, чтобы
lifecycle, admission, strict recall и checkpoint validation сохранили точную
семантику при PostgreSQL MVCC. Это намеренно сериализует записи; хост обязан
обработать `LedgerConflictError`, повторив целую идемпотентную команду с тем же
стабильным `event_id`, а не SQL-фрагмент. Адаптер синхронный, поэтому в
async-приложении ему нужна worker/thread boundary. Используйте backup policy
платформы или `pg_dump`: его `backup()` намеренно не реализует file-copy
backup.

В [руководстве PostgreSQL и pgvector](postgres.md) описаны обязательные
настройки schema, прав, backup, restore, retention и capacity. `forget()` или
`erase()` затрагивает только live Ledger rows; операция сама по себе не
стирает предыдущие WAL, replica, backup или внешние projection.

## Admission boundary (v0.10)

`MemoryReviewGate` имеет один фиксированный scope, origin, policy и actor.
Его `ingress()` создаёт узкую host-configured точку входа: переменным входом
остаётся только `submit(content)`. Вызывающий не выбирает scope, origin, kind,
confidence, source/evidence ID, lifecycle state, record ID или event ID.
Закрытые origin: `user_input`, `document`, `tool_output`,
`model_extraction` и `host_assertion`; `unknown` и `legacy_unknown` нельзя
review-ить через gate.

`review()` ничего не пишет. Он создаёт sealed in-process `MemoryReview`,
который применяет только создавший его gate. Действия отображаются строго в
один lifecycle-результат: `allow` → `active`, `quarantine` → `quarantined`,
`reject` → `forget()` с удалением payload. Каждое применённое решение gate
создаёт content-free `MemoryAdmissionAudit`, парный lifecycle event. Record с
concrete v5 origin нельзя сделать recallable через `MemoryWriter.confirm`:
raw confirmation разрешён только для `unknown` или `legacy_unknown`.

Это API-boundary внутри доверенного процесса хоста, а не Python sandbox.
Модель должна получать JSON/RPC tool schema ровно с `{ "content": "..." }`;
scope, origin, source, policy и action выводит host adapter за пределами
модели. Не передавайте `MemoryReviewGate`, `MemoryWriter`,
`SqliteMemoryLedger`, `MemoryReview` или ingress object произвольному
in-process plugin-коду. Если plugin исполняется в том же процессе, сначала
изолируйте адаптер отдельным process/RPC boundary.

Legacy raw writer остаётся trusted-host escape hatch для совместимости и
cleanup. `writer.propose()` / `writer.assert_candidate()` дают происхождение
`unknown`, а raw lifecycle cleanup **не** создаёт admission audit. Это не
model tool и не strict-admission path.

## Жизненный цикл и recall

| Состояние | Как появляется | Default recall |
|---|---|---|
| `candidate` | ingress gate или legacy trusted raw writer | никогда |
| `active` | sealed `allow` gate или legacy raw confirmation | только при валидности и наличии payload |
| `superseded` | явный `supersede(old, replacement)` | никогда |
| `retracted` | `retract()` или `forget()` | никогда |
| `expired` | `expire()` / `expire_due()` | никогда |
| `quarantined` | sealed `quarantine()` gate или trusted cleanup | никогда |

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

Для concrete v5 origin `list_active()` перед выдачей записи в recall проверяет
парный immutable `allow` audit, payload event-а, origin, revision и reason.
Отсутствующий или некорректный audit fail-closed. SQLite всё ещё не
tamper-evident: тот, кто может напрямую менять БД, отключить triggers и
подделать внутренне согласованный event, способен обойти in-database audit.
Защитите файл БД; если такая угроза в scope, нужен внешний signing/key-management
boundary.

Замена факта всегда явная и детерминированная:

```python
# ``new_active`` уже прошёл admission через свой MemoryReviewGate.
new = new_active
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
  lifecycle receipt. Content-free v5 origin metadata и admission audit
  намеренно остаются, чтобы reject-решение можно было аудировать.
- `forget_by_source("pdf:opaque-id")` атомарен для всех найденных records в
  точном scope writer’а. Он также сохраняет scoped opaque source tombstone:
  тот же source нельзя ingest’ить снова в этом scope, но он независим в другом.
- `erase()` — явный необратимый локальный escape hatch: удаляет record,
  payload, relation, v5 provenance/audits и его event receipts. Он также redacts ссылки на этот
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
2. Вызовите `setup()` из явной migration job. v5 помечает только
   payload-bearing records до v5 как `legacy_unknown`; он не придумывает
   современный origin или review audit. Active records до v5 остаются
   recallable ради совместимости. Schema v6 добавляет sealed
   recall-checkpoint manifests и private selection sidecars; он не создаёт
   checkpoint задним числом из старого plan.
3. В strict deployment сначала inventory этих legacy active records,
   quarantine через trusted lifecycle code и re-ingest/review через concrete
   v5 origin; нельзя заявлять, что migrated legacy record прошёл v0.10
   admission.
4. Оставьте legacy readers authoritative, пока проверяете отдельный opt-in
   adapter/importer.
5. Для rollback верните traffic к старым компонентам только через restore
   backup до upgrade в отдельную БД. Старый код отклоняет новые schemas,
   включая v6, поэтому не делайте in-place или destructive downgrade общей БД.

`dry_run_setup()` и `setup()` проверяют schema-v6 checkpoint sidecars и их
relational shape. Они не могут аутентифицировать HMAC manifest-а: стабильный
`checkpoint_secret` намеренно остаётся вне SQLite; seal проверяет строгий
`LedgerRecallPlanner.resume_checkpoint()`, у которого есть этот host secret.

## Recovery после restart

`MemoryReview` намеренно process-local: после restart новый gate не может
повторить ранее sealed review. До применения action сохраните `record_id`,
который вернул `submit()`, и host-minted action `event_id`. При recovery
используйте `writer.events(record_id)` и `writer.admission_audits(record_id)`:

- парный **admission** audit/event означает final decision; не делайте review
  заново;
- lifecycle event concrete-origin admission без парного audit — сигнал
  corruption/нарушенной атомарности: остановитесь;
- если нет admission event или audit и concrete-origin record всё ещё
  candidate, нужен новый sealed review. Hard-erased record и его прежние event
  ID terminal и не должны создаваться заново.

v6 recall checkpoint отделён от `MemoryReview`: resume делайте только через
новый strict `LedgerRecallPlanner` с тем же защищённым `checkpoint_secret`,
совместимыми policy/fingerprint и `counter_id`, а также свежим task. Manifest
содержит лишь opaque continuation identity и selection metadata, но не task
text, plaintext memory или provider messages. Изменение lifecycle выбранной
записи инвалидирует checkpoint и удаляет его selection markers; это не
agent/workflow recovery system.

Importer для profile/session/vector и bridge для facts/episodes/procedures/RAG
evidence — будущая работа. Экспериментальный `LedgerContextComposer` покрывает
только узкий путь admitted Ledger JSON → один bounded request, включая явный
sealed-checkpoint resume, когда его вызывает host. Здесь нет lease,
exactly-once delivery, workflow engine или auto-wiring; Ledger и его recall
lane сохраняют остальное публичное поведение без изменений, пока migration
contracts не будут отдельно доказаны.
