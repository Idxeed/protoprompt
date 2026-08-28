"""Dependency-free converters from common document framework objects."""

from __future__ import annotations

import hashlib
from typing import Any, Iterable

from protoprompt.rag.types import Document


def from_llamaindex(documents: Iterable[Any]) -> list[Document]:
    """Convert LlamaIndex ``Document``/``TextNode``-like objects."""

    output: list[Document] = []
    for index, item in enumerate(documents):
        getter = getattr(item, "get_content", None)
        text = getter() if callable(getter) else getattr(item, "text", "")
        text = str(text or "")
        identity = (
            getattr(item, "doc_id", None)
            or getattr(item, "node_id", None)
            or getattr(item, "id_", None)
            or _derived_id("llamaindex", index, text)
        )
        metadata = dict(getattr(item, "metadata", {}) or {})
        metadata["source_framework"] = "llamaindex"
        output.append(Document(str(identity), text, metadata))
    return output


def from_unstructured(elements: Iterable[Any], *, doc_id: str = "unstructured") -> list[Document]:
    """Convert Unstructured ``Element`` objects, preserving element provenance."""

    output: list[Document] = []
    for index, element in enumerate(elements):
        text = str(getattr(element, "text", element) or "")
        element_id = getattr(element, "id", None) or getattr(element, "element_id", None)
        metadata_object = getattr(element, "metadata", None)
        to_dict = getattr(metadata_object, "to_dict", None)
        if callable(to_dict):
            metadata = dict(to_dict())
        elif isinstance(metadata_object, dict):
            metadata = dict(metadata_object)
        else:
            metadata = {}
        metadata.update({
            "source_framework": "unstructured",
            "element_index": index,
            "element_type": type(element).__name__,
        })
        identity = str(element_id or _derived_id(doc_id, index, text))
        output.append(Document(identity, text, metadata))
    return output


def _derived_id(prefix: str, index: int, text: str) -> str:
    digest = hashlib.blake2b(text.encode("utf-8"), digest_size=12).hexdigest()
    return f"{prefix}_{index}_{digest}"
