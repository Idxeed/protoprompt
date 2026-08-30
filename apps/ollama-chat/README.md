# ProtoPrompt Ollama Chat

Локальный reference-интерфейс для Ollama: чат, PDF RAG и долговременная
память диалога. Это не отдельный hosted-сервис и не заменяет аутентификацию
или multi-tenant policy: по умолчанию сервер слушает только `127.0.0.1`.

Главная идея приложения — хранить историю долго, но перед каждым ответом
собирать новый ограниченный запрос через
`TokenBudgetedContextBuilder.plan_messages()`. В интерфейсе видно, сколько
планировочных токенов заняты, какой reserve оставлен ответу и были ли
подтянуты PDF или память диалога.

## Быстрый старт

Нужны Python 3.11+ и запущенная локальная [Ollama](https://ollama.com/).

```bash
ollama pull llama3.1
ollama pull nomic-embed-text

git clone https://github.com/Idxeed/protoprompt.git
cd protoprompt
python -m pip install -e ".[documents,fastapi,ollama,dev]"
python -m pip install -e "apps/ollama-chat[dev]"
pp-ollama-chat
```

Откройте `http://127.0.0.1:8000`. Для установки reference-приложения из
конкретного тега репозитория используйте его `subdirectory` после установки
совместимой версии `protoprompt`:

```bash
python -m pip install "protoprompt[documents,fastapi,ollama]==0.15.0"
python -m pip install "git+https://github.com/Idxeed/protoprompt.git@v0.15.0#subdirectory=apps/ollama-chat"
```

Приложение проверяется вместе с релизом `protoprompt`, но пока поставляется
из исходного репозитория, а не как отдельный PyPI-релиз.

## Что сохраняется и что отправляется модели

По умолчанию данные находятся в `%LOCALAPPDATA%\ProtoPrompt\ollama-chat` на
Windows и в `$XDG_DATA_HOME/protoprompt/ollama-chat` на Linux/macOS:

| Файл/каталог | Назначение |
|---|---|
| `chat.db` | полный локальный transcript, список PDF и durable-memory ledger |
| `memory.db` | SQLite-векторный индекс PDF и архивных фрагментов диалогов |
| `uploads/` | загруженные PDF |

На POSIX приложение создаёт каталог данных и `uploads/` с режимом `0700`, а
основные SQLite-файлы и новые PDF — с `0600`. На Windows стандартный путь
располагается в профиле пользователя (`LOCALAPPDATA`); при указании своего
`OLLAMA_CHAT_DATA_DIR` убедитесь, что доступ к нему ограничен на уровне ОС.

Каждый завершённый ответ добавляет очередной oldest-unarchived сегмент
транскрипта в локальный векторный индекс (по умолчанию по 10 сообщений).
Сегменты не перезаписывают друг друга; pending ledger записывается до
индексации, поэтому сбой между шагами не делает вектор «забытым» для удаления
или следующей попытки. Удаление диалога удаляет transcript и его архивные
вектора; удаление PDF очищает файл, метаданные и векторный индекс.

Полная история не отправляется в Ollama. На один ход планировщик выбирает
релевантные PDF/архивные фрагменты, недавнюю историю и обязательное новое
сообщение, затем оставляет reserve для ответа. Это **durable memory**, а не
обещание perfect recall или бесконечного активного контекста.

Токеновый индикатор использует детерминированный `RegexTokenCounter`; это
планировочная оценка, а не токенайзер любой выбранной модели. Тот же полный
лимит передаётся Ollama как `num_ctx`, а reserve — как `num_predict`. Если
модель не поддерживает заданный размер окна, уменьшите лимит или выберите
другую модель.

## PDF RAG

Поддерживаются обычные PDF с текстовым слоем. Сканированные документы без
текста сначала нужно прогнать через OCR. До индексации приложение проверяет
расширение, размер, PDF-сигнатуру и читает файл только из изолированной папки
uploads. Значение по умолчанию — до 10 MiB на файл; pypdf также получает
лимит на распаковку content streams до того, как они развернутся в памяти.

Фрагменты PDF и памяти рассматриваются как недоверенные справочные данные:
system prompt запрещает исполнять команды или смену роли из их содержимого.

## Конфигурация

| Переменная | По умолчанию | Назначение |
|---|---:|---|
| `OLLAMA_HOST` | `http://127.0.0.1:11434` | Ollama endpoint |
| `OLLAMA_CHAT_ALLOW_REMOTE` | unset | Только `1` разрешает non-loopback Ollama |
| `OLLAMA_CHAT_MODEL` | `llama3.1` | модель чата |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | модель embeddings |
| `OLLAMA_CHAT_DATA_DIR` | platform data dir | папка локальных данных |
| `OLLAMA_CHAT_REQUEST_MAX_TOKENS` | `8192` | `num_ctx` и лимит планировщика |
| `OLLAMA_CHAT_OUTPUT_RESERVE` | `1024` | reserve/`num_predict`; меньше request limit |
| `OLLAMA_CHAT_HISTORY_MESSAGES` | `80` | максимум недавних сообщений-кандидатов |
| `OLLAMA_CHAT_MEMORY_INTERVAL` | `10` | число сообщений в одном архивном сегменте |
| `OLLAMA_CHAT_MEMORY_MESSAGE_CHARS` | `20000` | максимум символов одной записи в semantic archive |
| `OLLAMA_CHAT_MAX_UPLOAD_BYTES` | `10485760` | лимит PDF в байтах |

Например, для 4k-окна:

```bash
OLLAMA_CHAT_REQUEST_MAX_TOKENS=4096 OLLAMA_CHAT_OUTPUT_RESERVE=512 pp-ollama-chat
```

## Безопасность и сеть

- Сервер по умолчанию привязан к loopback. Non-loopback bind требует явного
  `pp-ollama-chat --host 0.0.0.0 --allow-network`; при этом приложение не
  добавляет аутентификацию — не публикуйте его в интернет как есть.
- Remote Ollama запрещена по умолчанию. Чтобы сознательно разрешить её,
  установите `OLLAMA_CHAT_ALLOW_REMOTE=1`. При таком режиме **текст сообщений
  и содержимое PDF уходят на удалённый endpoint**; интерфейс показывает
  предупреждение.
- Нет CORS, есть базовые CSP/anti-framing headers, а API-ответы не кэшируются.
  Это defence-in-depth для локального приложения, не замена auth boundary.
- До разбора multipart/JSON приложение ограничивает raw body: PDF — размером
  файла с небольшим multipart-overhead, а запрос чата — размером, достаточным
  для документированного лимита сообщения. Это не даёт чужому телу запроса
  занять память до валидации схемы.
- PDF имеет лимиты по размеру, страницам, тексту и распаковке content streams,
  но парсер не является полноценной песочницей: для недоверенных файлов из
  сети оставляйте UI на loopback и проверяйте их отдельным защитным контуром.
- Не коммитьте каталог данных. В репозитории он уже исключён из Git.

## Проверка разработки

```bash
python -m pytest -q apps/ollama-chat/tests
```

Тесты используют fake Ollama и не требуют сети или скачанных моделей. Они
проверяют request receipt, переданный `num_ctx`, успешный PDF→RAG путь,
pre-parser limits для PDF/чата, durable-архив после оконной истории, отменённый
turn, удаление векторов/файлов (включая crash-остатки) и миграцию локальной БД
предыдущего reference-приложения.
