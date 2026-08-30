from __future__ import annotations

from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from protoprompt.readers import (
    DocumentReadError,
    LocalDocumentReader,
    ReaderLimits,
    from_llamaindex,
    from_unstructured,
    read_document,
)


def test_text_markdown_and_source_readers_add_provenance(tmp_path):
    root = tmp_path / "allowed"
    root.mkdir()
    markdown = root / "contract.md"
    markdown.write_text("# Renewal\n\n15 May", encoding="utf-8")
    source = root / "policy.py"
    source.write_text("RENEWAL = '15 May'", encoding="utf-8")
    reader = LocalDocumentReader(allowed_root=root)

    first = reader.read(markdown)
    second = reader.read(source, doc_id="source-policy", metadata={"owner": "legal"})
    protected = reader.read(
        source,
        metadata={"reader": "spoofed", "source_uri": "https://attacker.test"},
    )

    assert first.text == "# Renewal\n\n15 May"
    assert first.doc_id.startswith("file_")
    assert first.metadata["source_kind"] == "local_file"
    assert first.metadata["source_name"] == "contract.md"
    assert first.metadata["source_uri"].startswith("file:")
    assert second.doc_id == "source-policy"
    assert second.metadata["owner"] == "legal"
    assert protected.metadata["reader"] == "text"
    assert protected.metadata["source_uri"].startswith("file:")


def test_reader_rejects_url_root_escape_binary_unsupported_and_limits(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    binary = root / "bad.txt"
    binary.write_bytes(b"text\x00binary")
    unknown = root / "archive.bin"
    unknown.write_bytes(b"abc")
    large = root / "large.md"
    large.write_text("x" * 20, encoding="utf-8")
    reader = LocalDocumentReader(
        allowed_root=root,
        limits=ReaderLimits(max_bytes=15, max_chars=10),
    )

    with pytest.raises(DocumentReadError, match="URI"):
        reader.read("https://example.com/document.txt")
    with pytest.raises(DocumentReadError, match="outside allowed_root"):
        reader.read(outside)
    with pytest.raises(DocumentReadError, match="NUL"):
        reader.read(binary)
    with pytest.raises(DocumentReadError, match="unsupported"):
        reader.read(unknown)
    with pytest.raises(DocumentReadError, match="bytes"):
        reader.read(large)


def test_html_reader_removes_active_and_hidden_content(tmp_path):
    path = tmp_path / "page.html"
    path.write_text(
        "<html><style>secret-style</style><script>steal()</script>"
        "<body><h1>Contract</h1><p>Renews 15 May.</p></body></html>",
        encoding="utf-8",
    )

    document = read_document(path)

    assert "Contract" in document.text
    assert "15 May" in document.text
    assert "steal" not in document.text
    assert "secret-style" not in document.text
    assert document.metadata["reader"] == "html"


def test_docx_reader_extracts_paragraphs_and_tables(tmp_path):
    docx = pytest.importorskip("docx")
    path = tmp_path / "contract.docx"
    source = docx.Document()
    source.add_paragraph("Contract renewal")
    table = source.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Date"
    table.cell(0, 1).text = "15 May"
    source.save(path)

    document = read_document(path)

    assert "Contract renewal" in document.text
    assert "Date\t15 May" in document.text
    assert document.metadata["reader"] == "docx"


def test_docx_reader_rejects_external_relationships(tmp_path):
    path = tmp_path / "external.docx"
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr("word/document.xml", "<document/>")
        archive.writestr(
            "word/_rels/document.xml.rels",
            '<Relationship TargetMode="External" Target="https://evil.test"/>',
        )

    with pytest.raises(DocumentReadError, match="external relationships"):
        read_document(path)


def test_pdf_reader_handles_blank_and_rejects_encrypted_without_password(tmp_path):
    pypdf = pytest.importorskip("pypdf")
    plain = tmp_path / "blank.pdf"
    writer = pypdf.PdfWriter()
    writer.add_blank_page(width=100, height=100)
    with plain.open("wb") as stream:
        writer.write(stream)
    assert read_document(plain).metadata["reader"] == "pdf"

    encrypted = tmp_path / "encrypted.pdf"
    writer.encrypt("correct")
    with encrypted.open("wb") as stream:
        writer.write(stream)
    with pytest.raises(DocumentReadError, match="encrypted"):
        read_document(encrypted)
    assert read_document(encrypted, password="correct").metadata["reader"] == "pdf"


def test_pdf_reader_caps_compressed_stream_before_expansion(tmp_path, monkeypatch):
    pypdf = pytest.importorskip("pypdf")
    from pypdf import filters
    from pypdf.generic import EncodedStreamObject, NameObject

    path = tmp_path / "compressed.pdf"
    writer = pypdf.PdfWriter()
    page = writer.add_blank_page(width=100, height=100)
    stream = EncodedStreamObject()
    stream[NameObject("/Filter")] = NameObject("/FlateDecode")
    # pypdf's writer needs an initial encoded payload before ``set_data`` can
    # create its decoded representation. The resulting on-disk stream is tiny
    # but would expand well past this test reader's 128-byte ceiling.
    stream._data = b""  # type: ignore[attr-defined]
    stream.set_data(b"q 1 0 0 1 0 0 cm Q\n" * 200)
    page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as output:
        writer.write(output)

    observed_limits: list[int] = []
    original_decompress = filters._decompress_with_limit

    def observe_limit(data: bytes) -> bytes:
        observed_limits.append(filters.ZLIB_MAX_OUTPUT_LENGTH)
        return original_decompress(data)

    monkeypatch.setattr(filters, "_decompress_with_limit", observe_limit)
    # pypdf documents zero as an opt-out for its global ceiling. A bounded
    # LocalDocumentReader must still impose its own configured limit.
    monkeypatch.setattr(filters, "ZLIB_MAX_OUTPUT_LENGTH", 0)
    original_limit = filters.ZLIB_MAX_OUTPUT_LENGTH
    reader = LocalDocumentReader(
        limits=ReaderLimits(max_pdf_stream_bytes=128),
    )

    with pytest.raises(DocumentReadError, match="content streams"):
        reader.read(path)

    assert observed_limits
    assert all(limit == 128 for limit in observed_limits)
    assert filters.ZLIB_MAX_OUTPUT_LENGTH == original_limit


def test_pdf_reader_rejects_malformed_pdf_as_document_read_error(tmp_path):
    path = tmp_path / "malformed.pdf"
    path.write_bytes(b"%PDF-1.7\ntruncated")

    with pytest.raises(DocumentReadError, match="malformed"):
        read_document(path)


def test_framework_converters_preserve_ids_text_and_metadata():
    class LlamaDocument:
        doc_id = "llama-1"
        metadata = {"file_name": "a.md", "source_framework": "spoofed"}

        def get_content(self):
            return "Llama text"

    class ElementMetadata:
        def to_dict(self):
            return {"page_number": 3}

    class NarrativeText:
        id = "element-1"
        text = "Unstructured text"
        metadata = ElementMetadata()

    llama = from_llamaindex([LlamaDocument()])[0]
    element = from_unstructured([NarrativeText()])[0]

    assert (llama.doc_id, llama.text) == ("llama-1", "Llama text")
    assert llama.metadata["source_framework"] == "llamaindex"
    assert (element.doc_id, element.text) == ("element-1", "Unstructured text")
    assert element.metadata["page_number"] == 3
    assert element.metadata["element_type"] == "NarrativeText"


def test_reader_limits_validate_positive_values():
    with pytest.raises(ValueError, match="max_bytes"):
        ReaderLimits(max_bytes=0)
