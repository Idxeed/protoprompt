from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class ChromaStore:
    def __init__(self, collection_name: str = "protoprompt", persist_dir: str | None = None) -> None:
        import chromadb
        if persist_dir is None:
            self._client = chromadb.Client()
        else:
            self._client = chromadb.PersistentClient(path=persist_dir)
        self._collection = self._client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"},
        )

    def add(
        self,
        doc_id: str,
        chunks: list[str],
        embeddings: list[list[float]],
        metadata: dict | None = None,
    ) -> None:
        meta = metadata or {}
        ids = [f"{doc_id}_chunk_{i}" for i in range(len(chunks))]
        metadatas = [{**meta, "chunk_index": i, "doc_id": doc_id} for i in range(len(chunks))]
        self._collection.add(
            ids=ids,
            embeddings=embeddings,
            documents=chunks,
            metadatas=metadatas,
        )

    def query(
        self,
        embedding: list[float],
        top_k: int = 5,
        where: dict | None = None,
        score_threshold: float | None = None,
    ) -> list[dict]:
        kwargs: dict = {"query_embeddings": [embedding], "n_results": top_k}
        if where:
            kwargs["where"] = where
        results = self._collection.query(**kwargs)
        output: list[dict] = []
        ids = results.get("ids", [[]])[0]
        docs = results.get("documents", [[]])[0]
        metas = results.get("metadatas", [[]])[0]
        dists = results.get("distances", [[]])[0] if results.get("distances") else None
        for i in range(len(ids)):
            distance = dists[i] if dists is not None else None
            if score_threshold is not None and distance is not None:
                similarity = 1.0 - distance
                if similarity < score_threshold:
                    continue
            output.append({
                "id": ids[i],
                "document": docs[i],
                "metadata": metas[i],
                "distance": distance,
            })
        return output

    def delete(self, doc_id: str) -> None:
        self._collection.delete(where={"doc_id": doc_id})

    def count(self) -> int:
        return self._collection.count()
