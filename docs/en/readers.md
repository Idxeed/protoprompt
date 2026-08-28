# Local document readers

`protoprompt.readers` converts allow-listed local text, Markdown, source, HTML,
PDF, and DOCX files into the core `Document` type. The reader deliberately does
not fetch URLs, follow DOCX external relationships, run scripts, or perform OCR.

```bash
pip install "protoprompt[documents]"  # only needed for PDF/DOCX/HTML
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

Resolved paths, including symlinks, must stay under `allowed_root`. The extension
must be on the text/source allow-list or be HTML, PDF, or DOCX. Text rejects NUL
bytes and invalid encoding. HTML removes script, style, template, and noscript
content. PDF parsing is strict, encrypted files require an explicit password, and
page/content/character limits apply. DOCX validates the ZIP package, entry count,
uncompressed size, compression ratio, required files, and external relationships.

Every document receives a stable opaque ID and trusted provenance: local source,
file name/URI, media type, byte size, and reader. Caller metadata cannot override
those fields. Treat extracted text as untrusted input even when the parser accepts
it; ingestion does not make document instructions authoritative.

The optional converters do not import either framework:

```python
from protoprompt.readers import from_llamaindex, from_unstructured

documents = from_llamaindex(llama_documents)
elements = from_unstructured(unstructured_elements, doc_id="contract")
```

IDs, text, and metadata are retained while `source_framework` and element
provenance are set by the converter.

## Boundaries and rollback

PDF extraction is text-only and may return little or no content for scans. The
reader does not infer MIME from remote headers and never performs SSRF-prone URL
fetches. Put any future remote reader behind a separate downloader with DNS/IP
policy, redirect limits, MIME verification, size/time limits, and quarantine.

Migrate an ingestion job by reading a sampled corpus, comparing extracted text
and provenance, then reindexing into a versioned collection. Rollback selects the
old collection and reader pipeline; it does not overwrite the source files.

Parser dependency updates require malformed/encrypted/archive-bomb fixtures and
the deterministic reader suite. New formats need explicit resource limits and a
security review before joining the allow-list.
