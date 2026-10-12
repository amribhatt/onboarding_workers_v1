"""The model provider is a table entry, so the app is model agnostic: local, any OpenAI-style gateway, or a cloud
project with a service-account file. No test calls a live model or reads a real credential."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from worker.agent import PROVIDERS, make_model


def test_the_table_names_every_provider_and_only_keyed_ones_have_a_key_name() -> None:
    assert {"openai", "openrouter", "ollama", "compatible", "vertex", "demo"} <= set(PROVIDERS)
    assert PROVIDERS["ollama"].key_env == "" and PROVIDERS["vertex"].key_env == ""
    assert PROVIDERS["openai"].key_env == "OPENAI_API_KEY"


def test_ollama_is_local_with_no_key() -> None:
    m = make_model({"WORKER_MODEL_PROVIDER": "ollama", "WORKER_MODEL": "local-model"})
    assert str(m.openai_api_base).startswith("http://localhost:11434")
    other = make_model({"WORKER_MODEL_PROVIDER": "ollama", "WORKER_MODEL": "x", "OLLAMA_BASE_URL": "http://box:11434/v1"})
    assert str(other.openai_api_base) == "http://box:11434/v1"


def test_a_compatible_service_needs_its_address() -> None:
    with pytest.raises(ValueError, match="WORKER_BASE_URL"):
        make_model({"WORKER_MODEL_PROVIDER": "compatible", "WORKER_MODEL": "m"})
    m = make_model({"WORKER_MODEL_PROVIDER": "compatible", "WORKER_MODEL": "m", "WORKER_BASE_URL": "http://gw/v1"})
    assert str(m.openai_api_base) == "http://gw/v1"


def test_vertex_needs_the_service_account_file_and_uses_its_project(tmp_path: Path, monkeypatch: Any) -> None:
    with pytest.raises(ValueError, match="GOOGLE_APPLICATION_CREDENTIALS"):
        make_model({"WORKER_MODEL_PROVIDER": "vertex", "WORKER_MODEL": "gemini"})
    key = tmp_path / "sa.json"
    key.write_text("{}", encoding="utf-8")
    seen: dict[str, Any] = {}

    class Creds:
        project_id = "file-project"

    from google.oauth2 import service_account

    def fake_from_file(path: str, scopes: list[str]) -> Creds:
        seen["path"], seen["scopes"] = path, scopes
        return Creds()

    monkeypatch.setattr(service_account.Credentials, "from_service_account_file", staticmethod(fake_from_file))

    import langchain_google_genai as G

    class Chat:
        def __init__(self, **kw: Any) -> None:
            seen["chat"] = kw

    monkeypatch.setattr(G, "ChatGoogleGenerativeAI", Chat)
    make_model({"WORKER_MODEL_PROVIDER": "vertex", "WORKER_MODEL": "gemini-x",
                "GOOGLE_APPLICATION_CREDENTIALS": str(key)})
    assert seen["path"] == str(key) and seen["chat"]["project"] == "file-project"
    assert seen["chat"]["vertexai"] is True and seen["chat"]["location"] == "us-central1"


def test_an_unknown_provider_lists_the_known_ones() -> None:
    with pytest.raises(ValueError, match="use one of: openai"):
        make_model({"WORKER_MODEL_PROVIDER": "nope"})
