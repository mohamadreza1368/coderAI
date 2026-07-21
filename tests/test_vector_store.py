import io
import json
import urllib.error

from vector_store import LocalEmbeddingProvider


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def test_embedding_provider_falls_back_to_legacy_ollama_endpoint(monkeypatch):
    urls = []

    def fake_urlopen(request, timeout):
        urls.append(request.full_url)
        if request.full_url.endswith("/api/embed"):
            raise urllib.error.HTTPError(request.full_url, 404, "Not found", {}, io.BytesIO())
        return _Response({"embedding": [0.1, 0.2]})

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    vectors = LocalEmbeddingProvider().embed(["first", "second"])

    assert vectors == [[0.1, 0.2], [0.1, 0.2]]
    assert urls == [
        "http://127.0.0.1:11434/api/embed",
        "http://127.0.0.1:11434/api/embeddings",
        "http://127.0.0.1:11434/api/embeddings",
    ]
