# Локальные document readers

`protoprompt.readers` преобразует разрешённые локальные text, Markdown, source,
HTML, PDF и DOCX файлы в core-тип `Document`. Reader намеренно не скачивает URL,
не следует внешним DOCX relationships, не исполняет scripts и не делает OCR.

```bash
pip install "protoprompt[documents]"  # только для PDF/DOCX/HTML
python examples/read_documents.py ./handbook.pdf
```

```python
from protoprompt.readers import LocalDocumentReader, ReaderLimits

reader = LocalDocumentReader(
    allowed_root="./approved-documents",
    limits=ReaderLimits(max_bytes=10_000_000, max_pages=200),
)
document = reader.read("./approved-documents/contract.docx")
```

Разрешённый путь проверяется после `resolve`, поэтому symlink тоже обязан остаться
внутри `allowed_root`. Расширение должно быть в allow-list текста/исходников либо
быть HTML, PDF или DOCX. Text отвергает NUL и неверную кодировку. Из HTML удаляются
script/style/template/noscript. PDF разбирается в strict mode, encrypted-файл
требует пароль, действуют лимиты страниц, streams и символов. Для DOCX проверяются
ZIP package, число entries, распакованный размер, compression ratio, обязательные
файлы и external relationships.

Документ получает стабильный opaque ID и доверенный provenance: тип локального
источника, имя/URI, media type, byte size и reader. Caller metadata не может
переопределить эти поля. Извлечённый текст всё равно считается untrusted input:
успешный parsing не делает инструкции внутри документа доверенными.

Конвертеры не импортируют сами фреймворки:

```python
from protoprompt.readers import from_llamaindex, from_unstructured

documents = from_llamaindex(llama_documents)
elements = from_unstructured(unstructured_elements, doc_id="contract")
```

ID, text и metadata сохраняются; `source_framework` и element provenance задаёт
сам конвертер.

## Границы и откат

PDF extraction работает только с текстом и может вернуть пустой результат для
сканов. Reader не доверяет remote MIME и полностью исключает SSRF-поверхность URL
fetch. Будущий remote reader должен иметь отдельный downloader с DNS/IP policy,
лимитами redirect/size/time, проверкой MIME и quarantine.

Для миграции прогоните выборку корпуса, сравните текст и provenance, затем
переиндексируйте данные в версионированную collection. Откат выбирает прежнюю
collection и pipeline reader'а, не изменяя исходные файлы.

Обновление parser dependencies требует fixtures для malformed/encrypted/archive
bomb и полного deterministic suite. Новый формат попадает в allow-list только
после явных resource limits и security review.
