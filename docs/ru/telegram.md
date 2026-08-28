# Telegram-бот с памятью

Демо на aiogram 3 — полноценное polling-приложение с постоянной памятью в
SQLite. Оно автоматически сохраняет завершённые реплики, достаёт релевантные
старые диалоги, держит небольшое hot-окно и показывает безопасную диагностику
без содержимого сообщений.

## Установка и запуск

Выберите один модельный backend:

=== "Ollama"

    ```bash
    pip install "protoprompt[telegram,ollama]"
    ollama pull llama3.1 nomic-embed-text
    export TELEGRAM_BOT_TOKEN="..."
    export PROTOPROMPT_PROVIDER="ollama"
    python examples/telegram_memory_bot.py
    ```

=== "OpenAI"

    ```bash
    pip install "protoprompt[telegram,openai]"
    export TELEGRAM_BOT_TOKEN="..."
    export OPENAI_API_KEY="..."
    export PROTOPROMPT_PROVIDER="openai"
    python examples/telegram_memory_bot.py
    ```

В PowerShell используйте `$env:NAME="value"`. Путь к SQLite задаёт
`PROTOPROMPT_DB`, по умолчанию — `telegram_memory.db`. Модели и endpoint можно
переопределить переменными `OPENAI_*` или `OLLAMA_*`, перечисленными в исходнике
примера.

Не используйте прежнюю базу после перехода на embedding-модель с другой
размерностью вектора. Создайте новую базу или переиндексируйте память.

## Команды и приватность

- `/memory` показывает количество записей в текущем и всех тредах, а также
  размер hot-окна;
- `/why` показывает id и similarity score последнего recall, но не текст;
- `/forget` объясняет необратимое действие;
- `/forget confirm` удаляет зарегистрированную долгую память пользователя во
  всех Telegram-чатах.

Хост строит `MemoryScope` из доверенных Telegram user/chat id. Текст модели не
может выбрать другого пользователя или tenant. Реестр удаления хранит только
поля scope и непрозрачные id; содержимое диалогов остаётся в vector store. По
умолчанию бот не логирует токены и сообщения.

## Воспроизводимая проверка длинного диалога

Детерминированный офлайн-сценарий кладёт код доступа в третью реплику диалога
из 100 сообщений и ограничивает FIFO/LRU ёмкостью 12:

```bash
python examples/telegram_long_dialog.py
```

Ожидаемый результат: оба ограниченных baseline теряют старый факт, к которому
не обращались, а semantic memory возвращает `turn-2`. Это сравнение retention и
retrieval, а не обещание буквально бесконечного хранилища или безошибочного
recall для любой embedding-модели.
