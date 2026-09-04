# Миграция deployment с 0.6 на линию Ledger 1.0

Это **неразрушающий cutover**, а не импорт «на месте». В `v0.6.1` были
SQLite-векторные/session stores и profile stores, но не было Memory Ledger.
Их содержимое — legacy-данные приложения, а не автоматически подтверждённые
`MemoryRecord`.

Текущий Ledger остаётся experimental до выполнения RC exit gate для 1.0.
Документ описывает проверенное безопасное направление миграции; он не обещает,
что старый profile, summary, PDF или model output можно без host-review
вспоминать как современный факт.

## Что сохраняет cutover

```text
v0.6 SQLite source (read-only) ── snapshot + backup ──> сохранённый rollback source
                                      |
                                      +──> новый отдельный Ledger SQLite file
                                                  |
                                                  +──> явный host review и re-ingest
```

Новый Ledger не импортирует таблицу `chunks`, profile JSON или legacy session
summaries. Его нужно создавать в отдельном SQLite-файле. Старые readers
приложения остаются выбираемыми до конца rollback window.

## Процедура для SQLite

1. Остановите или зафиксируйте запись в v0.6 data file и сделайте backup на
   уровне ОС/БД. Сохраните исходник read-only на rollback window. Защищайте
   его: там могут быть prompt, profile, document или transcript content.
2. Зафиксируйте content и catalog snapshot legacy file. Минимум — SHA-256,
   число строк `chunks` и `profiles`, а также используемый v0.6 package pin.
3. Создайте **другой** путь для Ledger и выполните явную migration job:

   ```python
   from protoprompt.ledger import SqliteMemoryLedger

   ledger = SqliteMemoryLedger("/private/protoprompt-ledger-v1.db")
   print(ledger.dry_run_setup())  # проверить до записи
   ledger.setup()
   ledger.close()
   ```

   Импорт `protoprompt` или создание обычного vector/profile reader не
   выполняет эту операцию.
4. Снова откройте сохранённый v0.6 source его прежними readers и сравните
   snapshot. В Ledger target не должно быть записей, пока trusted host code
   явно не создаст, не проверит и не подтвердит их.
5. Подключайте новое Ledger-backed поведение через host-owned scope и concrete
   admission policy. Не переносите массово profile facts, session summaries,
   PDF text или model output только потому, что они есть в старом store.

Repository gate `tests/test_migration_from_v0_6.py` материализует точную
форму таблиц `chunks` и `profiles` опубликованного v0.6.1, создаёт отдельный
v7 Ledger и доказывает сохранность source bytes/catalog. Он также проверяет,
что копия rollback source читается текущими vector и profile readers.

## Откат

Остановите запись в новый Ledger target, переключите приложение обратно на
сохранённые v0.6 package/configuration и source database, затем отдельно
разберите копию Ledger. **Не** пытайтесь понизить схему Ledger на месте и не
копируйте таблицы Ledger в legacy source database. Откат — выбор источника, а
не обратная миграция схемы.

## Cutover для PostgreSQL

`PostgresMemoryLedger` поддерживает свежую выделенную Ledger schema; он не
умеет автоматически обновлять v0.6 application tables. Сделайте и проверьте
platform backup (`pg_dump` или restore managed DB), создайте отдельную пустую
Ledger schema явной migration role и держите прежнюю application schema
read-only во время shadow-read/canary нового пути. Откат — восстановление
старой schema/configuration; разрушительного downgrade нет.

## Границы

В релизе v0.6 не было нынешнего Ollama/PDF reference application, поэтому для
его app database здесь нет claim об upgrade. Документ доказывает только
legacy-core SQLite cutover. Production migration всё равно требует отдельного
плана retention, backup, authorization и explicit re-ingestion.
