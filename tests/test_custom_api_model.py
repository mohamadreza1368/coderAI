import web_app
from config import MODE_CUSTOM, MODE_LOCAL


def _set_custom(monkeypatch, model="provider/model-v2"):
    monkeypatch.setitem(web_app.STATE, "conn_mode", MODE_CUSTOM)
    monkeypatch.setitem(web_app.STATE, "model", "local-ollama:latest")
    monkeypatch.setitem(web_app.STATE, "custom_api_model", model)
    monkeypatch.setitem(web_app.STATE, "custom_api_url", "https://api.example.test/v1")
    monkeypatch.setitem(web_app.STATE, "custom_api_key", "sk-test")
    monkeypatch.setattr(web_app, "_use_langchain_runtime", lambda: False)
    monkeypatch.setattr(web_app, "_use_langchain_streaming_runtime", lambda: False)


def test_active_model_is_provider_specific(monkeypatch):
    _set_custom(monkeypatch)
    assert web_app._active_model() == "provider/model-v2"
    assert web_app._runtime_settings().model == "provider/model-v2"

    monkeypatch.setitem(web_app.STATE, "conn_mode", MODE_LOCAL)
    assert web_app._active_model() == "local-ollama:latest"


def test_custom_non_streaming_request_uses_custom_model(monkeypatch):
    _set_custom(monkeypatch, "vendor/code-model")
    captured = {}

    def fake_post(url, payload, headers=None, timeout=None):
        captured.update(url=url, payload=payload, headers=headers)
        return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

    monkeypatch.setattr(web_app, "_post_json", fake_post)
    response = web_app._call_model([{"role": "user", "content": "hello"}])

    assert response["content"] == "ok"
    assert captured["url"] == "https://api.example.test/v1/chat/completions"
    assert captured["payload"]["model"] == "vendor/code-model"
    assert captured["headers"]["Authorization"] == "Bearer sk-test"


def test_custom_streaming_request_uses_custom_model(monkeypatch):
    _set_custom(monkeypatch, "vendor/stream-model")
    captured = {}

    def fake_stream(url, payload, headers=None, timeout=None):
        captured.update(url=url, payload=payload, headers=headers)
        yield {"choices": [{"delta": {"content": "hello"}, "finish_reason": "stop"}]}

    events = []
    monkeypatch.setattr(web_app, "_post_json_stream", fake_stream)
    response = web_app._call_model_stream([{"role": "user", "content": "hello"}], events.append)

    assert response["content"] == "hello"
    assert captured["payload"]["model"] == "vendor/stream-model"
    assert events == [{"type": "token", "content": "hello"}]


def test_custom_model_remains_available_when_model_listing_fails(monkeypatch):
    _set_custom(monkeypatch, "private/deployment-name")
    monkeypatch.setattr(web_app, "_get_json", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("offline")))

    payload = web_app._available_models()

    assert payload["selected_model"] == "private/deployment-name"
    assert payload["models"] == ["private/deployment-name"]
