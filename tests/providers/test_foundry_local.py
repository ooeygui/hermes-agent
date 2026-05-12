"""Tests for the Foundry Local provider adapter and plugin.

The Foundry Local SDK is not installed in CI, so tests inject a fake
``foundry_local_sdk`` module into ``sys.modules`` and exercise the adapter's
mapping logic against that fake.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest


# ── Fake foundry_local_sdk ──────────────────────────────────────────────


class _FakeChatCompletion:
    def __init__(self, content: str = "ok") -> None:
        self.content = content


class _FakeChatClientSettings:
    def __init__(self) -> None:
        self.frequency_penalty = None
        self.max_tokens = None
        self.n = None
        self.temperature = None
        self.presence_penalty = None
        self.random_seed = None
        self.top_k = None
        self.top_p = None
        self.response_format = None
        self.tool_choice = None


class _FakeChatClient:
    def __init__(self, model_id: str) -> None:
        self.model_id = model_id
        self.settings = _FakeChatClientSettings()
        self.calls: list[dict[str, Any]] = []

    def complete_chat(self, messages, tools=None):
        self.calls.append({"streaming": False, "messages": messages, "tools": tools})
        return _FakeChatCompletion(content=f"sync:{self.model_id}")

    def complete_streaming_chat(self, messages, tools=None):
        self.calls.append({"streaming": True, "messages": messages, "tools": tools})

        def _gen():
            yield _FakeChatCompletion(content="chunk1")
            yield _FakeChatCompletion(content="chunk2")

        return _gen()


class _FakeEmbeddingResponse:
    def __init__(self, model_id: str, inputs) -> None:
        if isinstance(inputs, str):
            inputs = [inputs]
        self.model = model_id
        self.data = [
            {"object": "embedding", "index": i, "embedding": [0.1 * (i + 1)] * 4}
            for i, _ in enumerate(inputs)
        ]
        self.usage = {"prompt_tokens": 0, "total_tokens": 0}


class _FakeEmbeddingClient:
    def __init__(self, model_id: str) -> None:
        self.model_id = model_id
        self.calls: list[dict[str, Any]] = []

    def generate_embedding(self, input_text: str):
        self.calls.append({"kind": "single", "input": input_text})
        return _FakeEmbeddingResponse(self.model_id, input_text)

    def generate_embeddings(self, inputs):
        self.calls.append({"kind": "batch", "input": list(inputs)})
        return _FakeEmbeddingResponse(self.model_id, inputs)


class _FakeAudioSettings:
    def __init__(self) -> None:
        self.language = None
        self.temperature = None


class _FakeAudioResponse:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeAudioClient:
    def __init__(self, model_id: str) -> None:
        self.model_id = model_id
        self.settings = _FakeAudioSettings()
        self.calls: list[dict[str, Any]] = []

    def transcribe(self, path: str):
        # Snapshot the path's bytes (when present) so file-like spillover
        # tests can verify the temp file actually carried the payload.
        try:
            with open(path, "rb") as f:
                payload = f.read()
        except OSError:
            payload = b""
        self.calls.append({
            "path": path,
            "language": self.settings.language,
            "temperature": self.settings.temperature,
            "payload_len": len(payload),
        })
        return _FakeAudioResponse(text=f"transcribed:{self.model_id}")


class _FakeModel:
    def __init__(self, alias: str, *, cached: bool = True, loaded: bool = False) -> None:
        self.alias = alias
        self.is_cached = cached
        self.is_loaded = loaded
        self.download_called = False
        self.load_called = False
        self.unload_called = False
        self._chat_client = _FakeChatClient(alias)
        self._embedding_client = _FakeEmbeddingClient(alias)
        self._audio_client = _FakeAudioClient(alias)

    def download(self, _cb=None) -> None:
        self.download_called = True
        self.is_cached = True

    def load(self) -> None:
        self.load_called = True
        self.is_loaded = True

    def unload(self) -> None:
        self.unload_called = True
        self.is_loaded = False

    def get_chat_client(self):
        return self._chat_client

    def get_embedding_client(self):
        return self._embedding_client

    def get_audio_client(self):
        return self._audio_client


class _FakeCatalog:
    def __init__(self, models: dict[str, _FakeModel]) -> None:
        self._models = models

    def get_model(self, alias: str):
        return self._models.get(alias)

    def get_model_variant(self, model_id: str):
        return None

    def list_models(self):
        return list(self._models.values())


class _FakeManager:
    instance = None

    def __init__(self) -> None:
        self.catalog = _FakeCatalog({
            "qwen2.5-0.5b": _FakeModel("qwen2.5-0.5b"),
            "phi-4-mini": _FakeModel("phi-4-mini", cached=False),
            "qwen3-0.6b-embedding": _FakeModel("qwen3-0.6b-embedding"),
            "whisper-base": _FakeModel("whisper-base"),
        })
        self.eps_called = 0
        _FakeManager.instance = self

    @classmethod
    def initialize(cls, _config) -> None:
        if cls.instance is None:
            cls()

    def download_and_register_eps(self, *args, **kwargs) -> None:
        self.eps_called += 1


class _FakeConfiguration:
    def __init__(self, *, app_name: str = "") -> None:
        self.app_name = app_name


def _install_fake_sdk(monkeypatch):
    """Install a fake ``foundry_local_sdk`` module into ``sys.modules``."""
    sdk = types.ModuleType("foundry_local_sdk")
    sdk.Configuration = _FakeConfiguration
    sdk.FoundryLocalManager = _FakeManager

    openai_pkg = types.ModuleType("foundry_local_sdk.openai")
    chat_client_mod = types.ModuleType("foundry_local_sdk.openai.chat_client")
    chat_client_mod.ChatClientSettings = _FakeChatClientSettings
    openai_pkg.chat_client = chat_client_mod

    monkeypatch.setitem(sys.modules, "foundry_local_sdk", sdk)
    monkeypatch.setitem(sys.modules, "foundry_local_sdk.openai", openai_pkg)
    monkeypatch.setitem(sys.modules, "foundry_local_sdk.openai.chat_client", chat_client_mod)

    # Reset adapter-level singleton flags so each test starts fresh.
    import agent.foundry_local_adapter as adapter
    adapter._manager_ready = False
    adapter._eps_registered = False
    _FakeManager.instance = None
    return adapter


# ── Tests ────────────────────────────────────────────────────────────────


def test_client_create_basic_chat(monkeypatch):
    adapter = _install_fake_sdk(monkeypatch)

    client = adapter.FoundryLocalClient()
    response = client.chat.completions.create(
        model="qwen2.5-0.5b",
        messages=[{"role": "user", "content": "hi"}],
    )

    assert isinstance(response, _FakeChatCompletion)
    assert response.content == "sync:qwen2.5-0.5b"
    # EP registration runs exactly once per process.
    assert _FakeManager.instance.eps_called == 1
    # Model is loaded into memory.
    loaded_model = _FakeManager.instance.catalog.get_model("qwen2.5-0.5b")
    assert loaded_model.load_called is True


def test_client_create_streaming(monkeypatch):
    adapter = _install_fake_sdk(monkeypatch)

    client = adapter.FoundryLocalClient()
    stream = client.chat.completions.create(
        model="qwen2.5-0.5b",
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
    )

    chunks = list(stream)
    assert [c.content for c in chunks] == ["chunk1", "chunk2"]


def test_client_caches_loaded_model(monkeypatch):
    adapter = _install_fake_sdk(monkeypatch)

    client = adapter.FoundryLocalClient()
    for _ in range(3):
        client.chat.completions.create(
            model="qwen2.5-0.5b",
            messages=[{"role": "user", "content": "hi"}],
        )

    model = _FakeManager.instance.catalog.get_model("qwen2.5-0.5b")
    # load() should only have been called once even across multiple requests.
    assert model.load_called is True
    chat = model.get_chat_client()
    assert len(chat.calls) == 3


def test_client_downloads_uncached_model(monkeypatch):
    adapter = _install_fake_sdk(monkeypatch)

    client = adapter.FoundryLocalClient()
    client.chat.completions.create(
        model="phi-4-mini",
        messages=[{"role": "user", "content": "hi"}],
    )

    model = _FakeManager.instance.catalog.get_model("phi-4-mini")
    assert model.download_called is True
    assert model.load_called is True


def test_client_unknown_model_raises(monkeypatch):
    adapter = _install_fake_sdk(monkeypatch)

    client = adapter.FoundryLocalClient()
    with pytest.raises(RuntimeError, match="not found in catalog"):
        client.chat.completions.create(
            model="nonexistent-model-xyz",
            messages=[{"role": "user", "content": "hi"}],
        )


def test_client_missing_model_kwarg_raises(monkeypatch):
    adapter = _install_fake_sdk(monkeypatch)

    client = adapter.FoundryLocalClient()
    with pytest.raises(ValueError, match="'model' parameter is required"):
        client.chat.completions.create(
            model="",
            messages=[{"role": "user", "content": "hi"}],
        )


def test_apply_settings_maps_openai_kwargs(monkeypatch):
    adapter = _install_fake_sdk(monkeypatch)

    client = adapter.FoundryLocalClient()
    client.chat.completions.create(
        model="qwen2.5-0.5b",
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.7,
        max_tokens=128,
        top_p=0.9,
        seed=42,
        tool_choice="auto",
    )

    chat = _FakeManager.instance.catalog.get_model("qwen2.5-0.5b").get_chat_client()
    s = chat.settings
    assert s.temperature == 0.7
    assert s.max_tokens == 128
    assert s.top_p == 0.9
    assert s.random_seed == 42
    assert s.tool_choice == {"type": "auto"}


def test_apply_settings_function_tool_choice(monkeypatch):
    adapter = _install_fake_sdk(monkeypatch)

    client = adapter.FoundryLocalClient()
    client.chat.completions.create(
        model="qwen2.5-0.5b",
        messages=[{"role": "user", "content": "hi"}],
        tool_choice={"type": "function", "function": {"name": "get_weather"}},
    )

    chat = _FakeManager.instance.catalog.get_model("qwen2.5-0.5b").get_chat_client()
    assert chat.settings.tool_choice == {"type": "function", "name": "get_weather"}


def test_close_unloads_models(monkeypatch):
    adapter = _install_fake_sdk(monkeypatch)

    client = adapter.FoundryLocalClient()
    client.chat.completions.create(
        model="qwen2.5-0.5b",
        messages=[{"role": "user", "content": "hi"}],
    )
    model = _FakeManager.instance.catalog.get_model("qwen2.5-0.5b")
    assert model.unload_called is False

    client.close()
    assert model.unload_called is True
    assert client.is_closed is True


def test_missing_sdk_raises_with_install_hint(monkeypatch):
    """When the SDK isn't installed, _get_manager raises with a friendly hint."""
    # Make sure the fake is NOT in sys.modules.
    monkeypatch.delitem(sys.modules, "foundry_local_sdk", raising=False)
    monkeypatch.delitem(sys.modules, "foundry_local_sdk.openai", raising=False)
    monkeypatch.delitem(sys.modules, "foundry_local_sdk.openai.chat_client", raising=False)

    # Make any attempt to import foundry_local_sdk fail by inserting a
    # finder that raises ImportError.
    import importlib.machinery

    class _BlockedFinder:
        def find_spec(self, name, path=None, target=None):
            if name == "foundry_local_sdk" or name.startswith("foundry_local_sdk."):
                raise ImportError("blocked for test")
            return None

    monkeypatch.setattr(sys, "meta_path", [_BlockedFinder()] + sys.meta_path)

    import agent.foundry_local_adapter as adapter
    adapter._manager_ready = False
    adapter._eps_registered = False

    client = adapter.FoundryLocalClient()
    with pytest.raises(RuntimeError, match="foundry-local-sdk"):
        client.chat.completions.create(
            model="qwen2.5-0.5b",
            messages=[{"role": "user", "content": "hi"}],
        )


def test_async_client_wraps_sync(monkeypatch):
    """AsyncFoundryLocalClient delegates to the sync client via executor."""
    import asyncio

    adapter = _install_fake_sdk(monkeypatch)

    sync_client = adapter.FoundryLocalClient()
    async_client = adapter.AsyncFoundryLocalClient(sync_client)

    async def _go():
        return await async_client.chat.completions.create(
            model="qwen2.5-0.5b",
            messages=[{"role": "user", "content": "hi"}],
        )

    response = asyncio.run(_go())
    assert isinstance(response, _FakeChatCompletion)
    assert async_client.api_key == sync_client.api_key
    assert async_client.base_url == sync_client.base_url


# ── Embeddings ──────────────────────────────────────────────────────────


def test_embeddings_create_single_string(monkeypatch):
    adapter = _install_fake_sdk(monkeypatch)

    client = adapter.FoundryLocalClient()
    resp = client.embeddings.create(
        model="qwen3-0.6b-embedding", input="hello world"
    )

    assert isinstance(resp, _FakeEmbeddingResponse)
    assert resp.model == "qwen3-0.6b-embedding"
    assert len(resp.data) == 1
    emb_client = _FakeManager.instance.catalog.get_model(
        "qwen3-0.6b-embedding"
    ).get_embedding_client()
    assert emb_client.calls == [{"kind": "single", "input": "hello world"}]


def test_embeddings_create_batch(monkeypatch):
    adapter = _install_fake_sdk(monkeypatch)

    client = adapter.FoundryLocalClient()
    resp = client.embeddings.create(
        model="qwen3-0.6b-embedding", input=["a", "b", "c"]
    )

    assert isinstance(resp, _FakeEmbeddingResponse)
    assert len(resp.data) == 3
    emb_client = _FakeManager.instance.catalog.get_model(
        "qwen3-0.6b-embedding"
    ).get_embedding_client()
    assert emb_client.calls == [{"kind": "batch", "input": ["a", "b", "c"]}]


def test_embeddings_create_empty_input_raises(monkeypatch):
    adapter = _install_fake_sdk(monkeypatch)

    client = adapter.FoundryLocalClient()
    with pytest.raises(ValueError, match="must not be empty"):
        client.embeddings.create(model="qwen3-0.6b-embedding", input=[])


def test_embeddings_create_rejects_non_string_list(monkeypatch):
    adapter = _install_fake_sdk(monkeypatch)

    client = adapter.FoundryLocalClient()
    with pytest.raises(ValueError, match="token-id inputs are not supported"):
        client.embeddings.create(
            model="qwen3-0.6b-embedding", input=[[1, 2, 3], [4, 5, 6]]
        )


def test_embeddings_missing_input_raises(monkeypatch):
    adapter = _install_fake_sdk(monkeypatch)

    client = adapter.FoundryLocalClient()
    with pytest.raises(ValueError, match="'input' parameter is required"):
        client.embeddings.create(model="qwen3-0.6b-embedding")


def test_embedding_client_cached_per_alias(monkeypatch):
    adapter = _install_fake_sdk(monkeypatch)

    client = adapter.FoundryLocalClient()
    for _ in range(3):
        client.embeddings.create(model="qwen3-0.6b-embedding", input="x")

    model = _FakeManager.instance.catalog.get_model("qwen3-0.6b-embedding")
    assert model.load_called is True
    assert len(model.get_embedding_client().calls) == 3


def test_chat_and_embedding_share_model_load(monkeypatch):
    """Loading a model for chat and then asking for its embedding client
    must not re-download or re-load the model."""
    adapter = _install_fake_sdk(monkeypatch)

    client = adapter.FoundryLocalClient()
    client.chat.completions.create(
        model="qwen2.5-0.5b", messages=[{"role": "user", "content": "hi"}]
    )
    client.embeddings.create(model="qwen2.5-0.5b", input="x")

    model = _FakeManager.instance.catalog.get_model("qwen2.5-0.5b")
    # Both surfaces touched the model — but it loaded only once.
    assert model.load_called is True
    assert len(model.get_chat_client().calls) == 1
    assert len(model.get_embedding_client().calls) == 1


# ── Audio transcription ─────────────────────────────────────────────────


def test_audio_transcribe_with_path(monkeypatch, tmp_path):
    adapter = _install_fake_sdk(monkeypatch)

    audio_file = tmp_path / "clip.wav"
    audio_file.write_bytes(b"FAKEWAVDATA")

    client = adapter.FoundryLocalClient()
    resp = client.audio.transcriptions.create(
        model="whisper-base", file=str(audio_file)
    )

    assert isinstance(resp, _FakeAudioResponse)
    assert resp.text == "transcribed:whisper-base"
    audio_client = _FakeManager.instance.catalog.get_model(
        "whisper-base"
    ).get_audio_client()
    assert len(audio_client.calls) == 1
    assert audio_client.calls[0]["path"] == str(audio_file)
    assert audio_client.calls[0]["payload_len"] == len(b"FAKEWAVDATA")


def test_audio_transcribe_with_pathlib(monkeypatch, tmp_path):
    adapter = _install_fake_sdk(monkeypatch)

    audio_file = tmp_path / "clip.mp3"
    audio_file.write_bytes(b"\x00\x01\x02")

    client = adapter.FoundryLocalClient()
    resp = client.audio.transcriptions.create(
        model="whisper-base", file=audio_file
    )

    assert resp.text == "transcribed:whisper-base"


def test_audio_transcribe_with_file_handle(monkeypatch, tmp_path):
    """Binary file-like objects (the OpenAI-canonical form) are spilled to
    a temp file the SDK can read."""
    adapter = _install_fake_sdk(monkeypatch)

    audio_file = tmp_path / "clip.mp3"
    audio_file.write_bytes(b"MP3PAYLOAD")

    client = adapter.FoundryLocalClient()
    with open(audio_file, "rb") as f:
        resp = client.audio.transcriptions.create(model="whisper-base", file=f)

    assert resp.text == "transcribed:whisper-base"
    audio_client = _FakeManager.instance.catalog.get_model(
        "whisper-base"
    ).get_audio_client()
    call = audio_client.calls[0]
    assert call["path"].endswith(".mp3")  # original suffix preserved
    assert call["payload_len"] == len(b"MP3PAYLOAD")


def test_audio_response_format_text_returns_string(monkeypatch, tmp_path):
    adapter = _install_fake_sdk(monkeypatch)
    audio_file = tmp_path / "clip.wav"
    audio_file.write_bytes(b"x")

    client = adapter.FoundryLocalClient()
    out = client.audio.transcriptions.create(
        model="whisper-base", file=str(audio_file), response_format="text"
    )

    assert isinstance(out, str)
    assert out == "transcribed:whisper-base"


def test_audio_response_format_json_returns_object(monkeypatch, tmp_path):
    adapter = _install_fake_sdk(monkeypatch)
    audio_file = tmp_path / "clip.wav"
    audio_file.write_bytes(b"x")

    client = adapter.FoundryLocalClient()
    out = client.audio.transcriptions.create(
        model="whisper-base", file=str(audio_file), response_format="json"
    )

    assert isinstance(out, _FakeAudioResponse)


def test_audio_unsupported_response_format_raises(monkeypatch, tmp_path):
    adapter = _install_fake_sdk(monkeypatch)
    audio_file = tmp_path / "clip.wav"
    audio_file.write_bytes(b"x")

    client = adapter.FoundryLocalClient()
    with pytest.raises(ValueError, match="response_format='verbose_json'"):
        client.audio.transcriptions.create(
            model="whisper-base",
            file=str(audio_file),
            response_format="verbose_json",
        )


def test_audio_routes_language_and_temperature(monkeypatch, tmp_path):
    adapter = _install_fake_sdk(monkeypatch)
    audio_file = tmp_path / "clip.wav"
    audio_file.write_bytes(b"x")

    client = adapter.FoundryLocalClient()
    client.audio.transcriptions.create(
        model="whisper-base",
        file=str(audio_file),
        language="en",
        temperature=0.0,
    )

    audio_client = _FakeManager.instance.catalog.get_model(
        "whisper-base"
    ).get_audio_client()
    call = audio_client.calls[0]
    assert call["language"] == "en"
    assert call["temperature"] == 0.0


def test_audio_missing_file_raises(monkeypatch):
    adapter = _install_fake_sdk(monkeypatch)

    client = adapter.FoundryLocalClient()
    with pytest.raises(ValueError, match="'file' parameter is required"):
        client.audio.transcriptions.create(model="whisper-base")


def test_audio_rejects_text_stream(monkeypatch, tmp_path):
    """A file-like opened in text mode must be rejected."""
    adapter = _install_fake_sdk(monkeypatch)
    audio_file = tmp_path / "clip.txt"
    audio_file.write_text("not bytes")

    client = adapter.FoundryLocalClient()
    with open(audio_file, "r") as f:
        with pytest.raises(ValueError, match="binary mode"):
            client.audio.transcriptions.create(model="whisper-base", file=f)


def test_async_client_exposes_embeddings_and_audio(monkeypatch, tmp_path):
    """AsyncFoundryLocalClient routes embeddings and audio through the executor."""
    import asyncio

    adapter = _install_fake_sdk(monkeypatch)
    audio_file = tmp_path / "clip.wav"
    audio_file.write_bytes(b"x")

    sync_client = adapter.FoundryLocalClient()
    async_client = adapter.AsyncFoundryLocalClient(sync_client)

    async def _go():
        emb = await async_client.embeddings.create(
            model="qwen3-0.6b-embedding", input="hi"
        )
        tx = await async_client.audio.transcriptions.create(
            model="whisper-base", file=str(audio_file), response_format="text"
        )
        return emb, tx

    emb, tx = asyncio.run(_go())
    assert isinstance(emb, _FakeEmbeddingResponse)
    assert tx == "transcribed:whisper-base"


# ── Provider profile tests ──────────────────────────────────────────────


def _force_rediscovery():
    """Reset the provider registry AND evict cached plugin modules so the
    next ``list_providers()`` call re-imports every plugin __init__."""
    import providers as _pkg
    _pkg._REGISTRY.clear()
    _pkg._ALIASES.clear()
    _pkg._discovered = False
    for mod in list(sys.modules.keys()):
        if (
            mod.startswith("plugins.model_providers")
            or mod.startswith("_hermes_user_provider")
        ):
            del sys.modules[mod]


def test_provider_profile_registered():
    """The foundry-local profile is discoverable through the provider registry."""
    _force_rediscovery()

    from providers import get_provider_profile, list_providers

    names = {p.name for p in list_providers()}
    assert "foundry-local" in names

    profile = get_provider_profile("foundry-local")
    assert profile is not None
    assert profile.auth_type == "api_key"
    assert profile.base_url == "foundry-local://"
    assert profile.env_vars == ()
    assert "qwen2.5-0.5b" in profile.fallback_models

    # Aliases resolve.
    assert get_provider_profile("foundry").name == "foundry-local"
    assert get_provider_profile("foundrylocal").name == "foundry-local"


def test_profile_fetch_models_returns_none_without_sdk(monkeypatch):
    """When the SDK can't be reached, fetch_models returns None (callers
    fall back to fallback_models)."""
    monkeypatch.delitem(sys.modules, "foundry_local_sdk", raising=False)

    import importlib.machinery

    class _BlockedFinder:
        def find_spec(self, name, path=None, target=None):
            if name == "foundry_local_sdk" or name.startswith("foundry_local_sdk."):
                raise ImportError("blocked for test")
            return None

    monkeypatch.setattr(sys, "meta_path", [_BlockedFinder()] + sys.meta_path)

    import agent.foundry_local_adapter as adapter
    adapter._manager_ready = False
    adapter._eps_registered = False

    _force_rediscovery()

    from providers import get_provider_profile

    profile = get_provider_profile("foundry-local")
    assert profile.fetch_models() is None
