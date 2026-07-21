"""Shared local vector infrastructure for memory and code collections."""

from __future__ import annotations

import json
import math
import os
import urllib.request
import urllib.error
from pathlib import Path


class LocalEmbeddingProvider:
    def __init__(self, model: str | None = None, base_url: str | None = None, timeout: int = 8):
        self.model = model or os.getenv("OLLAMA_EMBEDDING_MODEL", "nomic-embed-text")
        self.base_url = (base_url or os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")).rstrip("/")
        self.timeout = timeout

    def embed(self, texts: str | list[str]) -> list[list[float]]:
        values = [texts] if isinstance(texts, str) else list(texts)
        if not values:
            return []
        body = json.dumps({"model": self.model, "input": values}).encode("utf-8")
        request = urllib.request.Request(f"{self.base_url}/api/embed", data=body, headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            return self._embed_legacy(values)
        embeddings = payload.get("embeddings") or []
        if len(embeddings) != len(values):
            raise RuntimeError(f"Ollama returned {len(embeddings)} embeddings for {len(values)} inputs")
        return [[float(item) for item in vector] for vector in embeddings]

    def _embed_legacy(self, values: list[str]) -> list[list[float]]:
        vectors = []
        for value in values:
            body = json.dumps({"model": self.model, "prompt": value}).encode("utf-8")
            request = urllib.request.Request(f"{self.base_url}/api/embeddings", data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            vector = payload.get("embedding") or []
            if not vector:
                raise RuntimeError("Ollama legacy embeddings endpoint returned no embedding")
            vectors.append([float(item) for item in vector])
        return vectors


class SharedVectorStore:
    """Chroma client shared by named collections; callers provide embeddings."""

    def __init__(self, path: str | Path):
        self.path = Path(path).resolve()
        self.path.mkdir(parents=True, exist_ok=True)
        self.available = False
        self.error = ""
        self._client = None
        try:
            import chromadb

            self._client = chromadb.PersistentClient(path=str(self.path))
            self.available = True
        except Exception as exc:
            self.error = repr(exc)

    def upsert(self, collection: str, ids: list[str], documents: list[str], metadatas: list[dict], embeddings: list[list[float]]) -> None:
        if not self.available or not ids:
            return
        self._client.get_or_create_collection(collection).upsert(
            ids=ids, documents=documents, metadatas=metadatas, embeddings=embeddings,
        )

    def delete(self, collection: str, ids: list[str] | None = None, where: dict | None = None) -> None:
        if not self.available:
            return
        target = self._client.get_or_create_collection(collection)
        if ids:
            target.delete(ids=ids)
        elif where:
            target.delete(where=where)

    def query(self, collection: str, embedding: list[float], limit: int) -> list[dict]:
        if not self.available or not embedding:
            return []
        result = self._client.get_or_create_collection(collection).query(query_embeddings=[embedding], n_results=limit)
        rows = []
        for index, chunk_id in enumerate((result.get("ids") or [[]])[0]):
            distance = ((result.get("distances") or [[]])[0] or [None] * (index + 1))[index]
            rows.append({"id": chunk_id, "score": 1.0 - float(distance or 0.0)})
        return rows


def cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    denominator = math.sqrt(sum(value * value for value in left)) * math.sqrt(sum(value * value for value in right))
    return sum(a * b for a, b in zip(left, right)) / denominator if denominator else 0.0
