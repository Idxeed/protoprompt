# Локальное task-resume демо для Ollama *(experimental)*

Это намеренно узкое **local-only** демо task-resume memory рядом с обычным
интерфейсом Ollama chat и PDF RAG. Оно помогает показать важное различие:
модель может получить bounded provider-safe reference к host-confirmed эпизоду
задачи и одновременно отвечать на текущий вопрос пользователя по PDF.

Это не authentication layer, не multi-user продукт, не agent workflow и не
способ управлять Ledger memory из браузера. Core-контракт описан в
[Возобновлении задачи](task-resume.md).

!!! danger "Не открывайте это демо в сеть"

    Режим намеренно ограничен loopback browser client и loopback Ollama
    endpoint. В нём нет authentication, tenant boundary или remote-operation
    support. Не ставьте его за proxy, не публикуйте для других пользователей и
    не используйте remote Ollama на клиентской демонстрации.

## Что демонстрируется

При старте trusted host code читает один private seed file и создаёт (либо
восстанавливает) одну связь conversation-to-task. Браузер по-прежнему видит
только обычные маршруты чата, загрузки PDF и диалогов.

- Текущий вопрос пользователя остаётся RAG query, поэтому свежие загруженные
  PDF ищутся как обычно.
- Модель получает фиксированную reduced reference: goal, aggregate completed
  action count, outcome, next action и lesson.
- Она не получает raw task reference, отдельные action references, frozen
  descriptor, checkpoint ID, Ledger record ID, source/evidence ID или
  checkpoint secret через эту интеграцию.
- PDF text, conversation turns и model output никогда не допускаются в task
  Ledger автоматически. Пока task mapping активен, приложение также исключает
  и не создаёт обычный semantic archive транскрипта для этого диалога.

Сам reference-текст остаётся недоверенными данными. Ответ модели — только
совет: он не способен выполнить задачу или изменить host mapping.

## Запуск из source checkout

Установите локальное приложение и скачайте локальные Ollama-модели:

```bash
ollama pull llama3.1
ollama pull nomic-embed-text

python -m pip install -e ".[documents,fastapi,ollama,dev]"
python -m pip install -e "apps/ollama-chat[dev]"
```

Скопируйте и отредактируйте host-owned example seed. Не помещайте секреты в
его text fields: `goal`, `next_action` и `lesson` намеренно отправляются
модели как reference data.

```powershell
Copy-Item apps/ollama-chat/examples/task-resume-demo.seed.json ./task-resume-demo.seed.json
pp-ollama-chat --task-resume-demo-seed ./task-resume-demo.seed.json
```

Откройте `http://127.0.0.1:8000`: seeded conversation уже создан. Загрузите
PDF с текстовым слоем и задайте вопрос о его текущем содержимом.

Команда fail-closed, если не выполнено хотя бы одно условие:

- web bind или TCP peer не loopback;
- `OLLAMA_HOST` не loopback endpoint;
- seed JSON malformed, слишком большой, содержит duplicate key или unknown
  field; либо
- существующий подписанный mapping изменён или уже находится в `closing`.

`--allow-network` и `--task-resume-demo-seed` нельзя использовать вместе.

## Demo-safe local profile

Используйте одну локальную генерацию `llama3.1:8b` с context ceiling 2048
токенов:

```powershell
$env:OLLAMA_CHAT_MODEL = "llama3.1:8b"
$env:OLLAMA_CHAT_REQUEST_MAX_TOKENS = "2048"
$env:OLLAMA_CHAT_OUTPUT_RESERVE = "1024"
pp-ollama-chat --task-resume-demo-seed ./task-resume-demo.seed.json
```

При explicit demo mode приложение ограничивает каждый request значением
`num_ctx=2048`, даже если обычная конфигурация больше; отвергает output reserve
2048 и выше и сериализует все локальные model generations через одну
in-process queue. Это намеренно консервативный demo profile, не throughput
service и не distributed scheduler.

Reference observation для этого profile: Ryzen 5 5600X, 32 GiB RAM, RX 7600
XT 16 GB и Ollama 0.33.2. `llama3.1:8b` при `num_ctx=2048` и
`nomic-embed-text` работали полностью на GPU; тёплая генерация составила
46,6–48,2 токена/с. Это одно локальное измерение, а не hardware guarantee,
model-quality claim или customer sizing promise. Не добавляйте в этот demo
profile обязательность 14B, vision, голоса или нового GPU.

## Seed contract

Seed — конфигурация host-а, не browser payload. Его точная JSON-форма
версионирована и отвергает unknown fields:

```json
{
  "schema_version": 1,
  "conversation_id": "launch-demo",
  "task_descriptor": "Host-only frozen recall descriptor.",
  "goal": "Safe model-visible task summary.",
  "completed_action_refs": ["action:host-only-reference"],
  "outcome": "interrupted",
  "next_action": "Safe model-visible next discussion step.",
  "lesson": "Safe model-visible lesson."
}
```

`outcome` — одно из `succeeded`, `failed` или `interrupted`. Descriptor и
action references остаются только у host-а; не используйте видимые поля для
паролей, customer data или execution instructions.

## Локальное хранение и удаление

Приложение создаёт стабильный 32-byte checkpoint secret в private data
directory. Он не хранится ни в одной SQLite database. State mapping —
additive HMAC-authenticated table в `chat.db`; task Ledger operations идут в
отдельный `task-resume-ledger.db`.

Restart с тем же seed и data directory восстанавливает authenticated active
mapping. Изменённый seed не заменяет существующий mapping молча. Удаление
seeded conversation сначала переводит mapping в `closing`, затем забывает
host task source в Ledger и только после этого удаляет mapping и обычные
conversation data. Если cleanup не удался, mapping остаётся non-resumable, а
удаление вернёт ошибку для безопасного повторения.

Не копируйте эти data files между машинами или пользователями. На POSIX
приложение создаёт local databases, secret и uploads с owner-only доступом; на
Windows держите data directory в профиле с ограниченным доступом пользователя.

## Осознанные ограничения для коммерческого демо

- Это local product demonstration, не готовый shared service.
- Здесь один host-authored episode на mapped conversation, а не autonomous
  extraction и не обещание infinite memory.
- В браузере и модели нет endpoint для task creation, rebind, review,
  admission, checkpoint или resume.
- Нет tool execution, workflow planning, dependency graph, exactly-once
  delivery или background task recovery.
- PDF RAG — текущее недоверенное evidence; retrieval не превращает его в
  trusted Ledger record.

На клиентской презентации покажите ответ по PDF и bounded context receipt, а
затем прямо назовите эти ограничения. Именно они делают демо полезным, не
выдавая его за authorization system.
