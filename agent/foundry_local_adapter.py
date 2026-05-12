"""OpenAI-SDK-compatible facade over Microsoft Foundry Local's native SDK.

Foundry Local (https://learn.microsoft.com/azure/ai-foundry/foundry-local/)
runs models on-device via Microsoft's native runtime. Its Python SDK
(``foundry_local_sdk``) returns ``openai.types.chat.ChatCompletion`` /
``ChatCompletionChunk`` objects directly — but the call surface is
``model.get_chat_client().complete_chat(messages, tools=...)`` rather than
``client.chat.completions.create(...)``.

This adapter wraps the native SDK in a thin OpenAI-shaped facade so that
``run_agent`` and ``auxiliary_client`` can call::

    client.chat.completions.create(model="qwen2.5-0.5b", messages=[...], stream=True)

and get back the same response shape they'd receive from any other provider.
The integration is **in-process** — there is no localhost HTTP hop.

Lifecycle:
  - ``FoundryLocalManager`` is a process-wide singleton, created on first use.
  - Loaded models + their ``ChatClient`` instances are cached per-alias inside
    ``FoundryLocalClient`` so subsequent requests skip the
    download/load round-trip.
  - Models are downloaded on first use if not present in the local cache;
    progress is logged at INFO level.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


_INSTALL_HINT = (
    "Foundry Local provider selected but the SDK is not installed. "
    "Install with: pip install foundry-local-sdk-winml  (Windows) "
    "or: pip install foundry-local-sdk  (macOS/Linux). "
    "See https://learn.microsoft.com/azure/ai-foundry/foundry-local/ for setup."
)


# Module-level singleton state for FoundryLocalManager. The SDK enforces a
# singleton itself (raises if initialize is called twice), so we cache the
# initialization promise here and serialize it under a lock.
_manager_lock = threading.Lock()
_manager_ready = False
_eps_registered = False


def _get_manager() -> Any:
    """Return the FoundryLocalManager singleton, initializing it once.

    Raises ``RuntimeError`` with a friendly install hint when the SDK is
    not importable.
    """
    global _manager_ready, _eps_registered
    try:
        from foundry_local_sdk import (  # type: ignore[import-not-found]
            Configuration,
            FoundryLocalManager,
        )
    except ImportError as exc:
        raise RuntimeError(_INSTALL_HINT) from exc

    with _manager_lock:
        if FoundryLocalManager.instance is None and not _manager_ready:
            FoundryLocalManager.initialize(Configuration(app_name="hermes-agent"))
            _manager_ready = True
        manager = FoundryLocalManager.instance
        # One-time execution provider download/registration so hardware
        # acceleration is wired up. Quiet — no progress callback.
        if not _eps_registered:
            try:
                manager.download_and_register_eps()
            except Exception as exc:  # pragma: no cover - hardware-dependent
                logger.warning(
                    "Foundry Local: EP registration failed (continuing on CPU): %s",
                    exc,
                )
            _eps_registered = True
        return manager


class FoundryLocalClient:
    """Minimal OpenAI-SDK-compatible facade over Foundry Local's native SDK.

    Construct just like ``openai.OpenAI`` (the keyword args are accepted
    for compatibility but ignored — Foundry Local is in-process and
    requires no API key or base URL).
    """

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        default_headers: Optional[Dict[str, str]] = None,
        timeout: Any = None,
        http_client: Any = None,
        **_: Any,
    ) -> None:
        # Stored for parity with OpenAI() so wrappers (auxiliary_client,
        # _to_async_client) can read .api_key / .base_url without crashing.
        self.api_key = api_key or "none"
        self.base_url = base_url or "foundry-local://"
        self._default_headers = dict(default_headers or {})
        self.is_closed = False

        # alias -> IModel (loaded). Per-kind sub-clients are cached separately
        # because a single loaded model can serve chat, embedding, OR audio
        # depending on its head; keeping the maps split lets us evict one
        # surface without losing the others.
        self._models: Dict[str, Any] = {}
        self._chat_clients: Dict[str, Any] = {}
        self._embedding_clients: Dict[str, Any] = {}
        self._audio_clients: Dict[str, Any] = {}
        self._loaded_lock = threading.Lock()

        self.chat = _FoundryChatNamespace(self)
        self.embeddings = _FoundryEmbeddings(self)
        self.audio = _FoundryAudioNamespace(self)

    def close(self) -> None:
        """Unload all loaded models and mark the client closed."""
        self.is_closed = True
        with self._loaded_lock:
            for alias, model in list(self._models.items()):
                try:
                    model.unload()
                except Exception as exc:  # pragma: no cover - best-effort cleanup
                    logger.debug("Foundry Local: unload(%s) failed: %s", alias, exc)
            self._models.clear()
            self._chat_clients.clear()
            self._embedding_clients.clear()
            self._audio_clients.clear()

    def __enter__(self) -> "FoundryLocalClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    # ── Internal: per-model load + sub-client cache ─────────────────────

    def _get_or_load_model(self, model_alias: str) -> Any:
        """Return a loaded ``IModel`` for ``model_alias``, downloading on demand.

        Shared across the chat / embedding / audio paths.
        """
        if not model_alias:
            raise ValueError("Foundry Local: 'model' parameter is required.")
        with self._loaded_lock:
            cached = self._models.get(model_alias)
            if cached is not None:
                return cached

        manager = _get_manager()
        model = manager.catalog.get_model(model_alias)
        if model is None:
            # Try treating the value as a variant id rather than an alias.
            model = manager.catalog.get_model_variant(model_alias)
        if model is None:
            available = ", ".join(
                sorted(m.alias for m in manager.catalog.list_models())[:10]
            )
            raise RuntimeError(
                f"Foundry Local: model alias '{model_alias}' not found in catalog. "
                f"Some available aliases: {available} "
                f"(run `foundry model list` for the full set, or "
                f"`foundry model download {model_alias}` to make it available)."
            )

        if not getattr(model, "is_cached", False):
            logger.info("Foundry Local: downloading model '%s' (first use)…", model_alias)
            last_pct = -10.0

            def _on_progress(percent: float) -> None:
                nonlocal last_pct
                if percent - last_pct >= 10.0 or percent >= 100.0:
                    logger.info("Foundry Local: %s download %.1f%%", model_alias, percent)
                    last_pct = percent

            model.download(_on_progress)

        if not getattr(model, "is_loaded", False):
            logger.info("Foundry Local: loading model '%s' into memory…", model_alias)
            model.load()

        with self._loaded_lock:
            existing = self._models.get(model_alias)
            if existing is not None:
                return existing
            self._models[model_alias] = model
            return model

    def _get_chat_client(self, model_alias: str) -> Any:
        """Return a cached ``ChatClient`` for ``model_alias``."""
        with self._loaded_lock:
            cached = self._chat_clients.get(model_alias)
            if cached is not None:
                return cached
        model = self._get_or_load_model(model_alias)
        chat_client = model.get_chat_client()
        with self._loaded_lock:
            existing = self._chat_clients.get(model_alias)
            if existing is not None:
                return existing
            self._chat_clients[model_alias] = chat_client
            return chat_client

    def _get_embedding_client(self, model_alias: str) -> Any:
        """Return a cached ``EmbeddingClient`` for ``model_alias``."""
        with self._loaded_lock:
            cached = self._embedding_clients.get(model_alias)
            if cached is not None:
                return cached
        model = self._get_or_load_model(model_alias)
        emb_client = model.get_embedding_client()
        with self._loaded_lock:
            existing = self._embedding_clients.get(model_alias)
            if existing is not None:
                return existing
            self._embedding_clients[model_alias] = emb_client
            return emb_client

    def _get_audio_client(self, model_alias: str) -> Any:
        """Return a cached ``AudioClient`` for ``model_alias``."""
        with self._loaded_lock:
            cached = self._audio_clients.get(model_alias)
            if cached is not None:
                return cached
        model = self._get_or_load_model(model_alias)
        audio_client = model.get_audio_client()
        with self._loaded_lock:
            existing = self._audio_clients.get(model_alias)
            if existing is not None:
                return existing
            self._audio_clients[model_alias] = audio_client
            return audio_client

    # ── Internal: kwargs → ChatClient call ──────────────────────────────

    @staticmethod
    def _apply_settings(chat_client: Any, kwargs: Dict[str, Any]) -> None:
        """Mirror OpenAI-style sampling kwargs onto ``chat_client.settings``.

        Foundry Local's ``ChatClient`` reads request-time tunables off a
        mutable ``settings`` object rather than method kwargs, so we copy
        each known field across before calling ``complete_chat`` /
        ``complete_streaming_chat``.
        """
        try:
            from foundry_local_sdk.openai.chat_client import (  # type: ignore[import-not-found]
                ChatClientSettings,
            )
        except ImportError:
            ChatClientSettings = None  # type: ignore[assignment]

        # Reset to a fresh settings instance per request so kwargs from a
        # previous call don't leak into this one.
        if ChatClientSettings is not None:
            chat_client.settings = ChatClientSettings()

        s = chat_client.settings
        for src, dst in (
            ("temperature", "temperature"),
            ("max_tokens", "max_tokens"),
            ("top_p", "top_p"),
            ("frequency_penalty", "frequency_penalty"),
            ("presence_penalty", "presence_penalty"),
            ("n", "n"),
            ("response_format", "response_format"),
            ("seed", "random_seed"),
        ):
            value = kwargs.get(src)
            if value is not None:
                setattr(s, dst, value)

        # tool_choice: OpenAI passes a string ("auto", "none", "required")
        # or a dict {"type": "function", "function": {"name": "..."}}.
        # Foundry expects a dict {"type": ..., "name": ...?}.
        tool_choice = kwargs.get("tool_choice")
        if isinstance(tool_choice, str):
            s.tool_choice = {"type": tool_choice}
        elif isinstance(tool_choice, dict):
            tc_type = tool_choice.get("type")
            if tc_type == "function":
                fn = tool_choice.get("function") or {}
                name = fn.get("name") if isinstance(fn, dict) else None
                if name:
                    s.tool_choice = {"type": "function", "name": name}
            elif tc_type:
                s.tool_choice = {"type": tc_type}

    def _create_chat_completion(self, **kwargs: Any) -> Any:
        """Implementation of ``chat.completions.create()``."""
        model_alias = kwargs.get("model") or ""
        messages: List[Dict[str, Any]] = list(kwargs.get("messages") or [])
        tools = kwargs.get("tools") or None
        stream = bool(kwargs.get("stream", False))

        chat_client = self._get_chat_client(model_alias)
        self._apply_settings(chat_client, kwargs)

        if stream:
            return chat_client.complete_streaming_chat(messages, tools=tools)
        return chat_client.complete_chat(messages, tools=tools)

    # ── Internal: embeddings ────────────────────────────────────────────

    def _create_embedding(self, **kwargs: Any) -> Any:
        """Implementation of ``embeddings.create()``.

        Maps OpenAI-style ``client.embeddings.create(model, input)`` to
        Foundry's ``EmbeddingClient.generate_embedding(s)``. Returns the
        SDK's native ``CreateEmbeddingResponse`` (the same OpenAI type
        downstream consumers expect).

        Unsupported OpenAI kwargs (``encoding_format``, ``dimensions``,
        ``user``) are accepted and ignored — Foundry has no equivalent
        knob.
        """
        model_alias = kwargs.get("model") or ""
        input_value = kwargs.get("input")
        if input_value is None:
            raise ValueError("Foundry Local: 'input' parameter is required.")

        emb_client = self._get_embedding_client(model_alias)

        if isinstance(input_value, str):
            return emb_client.generate_embedding(input_value)
        if isinstance(input_value, list):
            if not input_value:
                raise ValueError("Foundry Local: 'input' list must not be empty.")
            # OpenAI accepts list[int] (token ids) and list[list[int]] too.
            # Foundry only accepts list[str], so reject anything else with a
            # clear error.
            if not all(isinstance(item, str) for item in input_value):
                raise ValueError(
                    "Foundry Local embeddings: 'input' must be a string or "
                    "list[str]; token-id inputs are not supported."
                )
            return emb_client.generate_embeddings(input_value)
        raise ValueError(
            "Foundry Local embeddings: 'input' must be a string or list[str], "
            f"got {type(input_value).__name__}."
        )

    # ── Internal: audio transcription ───────────────────────────────────

    def _create_transcription(self, **kwargs: Any) -> Any:
        """Implementation of ``audio.transcriptions.create()``.

        Maps OpenAI-style ``client.audio.transcriptions.create(model, file,
        response_format=...)`` to Foundry's ``AudioClient.transcribe(path)``.

        ``file`` may be a path string, a ``pathlib.Path``, or a file-like
        object opened in binary mode (matching OpenAI's surface). File-like
        objects are spilled to a temp file because the native SDK only
        accepts paths.

        ``response_format`` of ``"text"`` returns a bare string (matching
        the OpenAI client's behavior); anything else returns the SDK's
        ``AudioTranscriptionResponse`` whose ``.text`` attribute holds the
        transcript. ``language`` and ``temperature`` are routed onto
        ``AudioClient.settings``. Unsupported formats (``verbose_json``,
        ``srt``, ``vtt``) raise ``ValueError``.
        """
        import os
        import tempfile
        from pathlib import Path

        model_alias = kwargs.get("model") or ""
        file_arg = kwargs.get("file")
        if file_arg is None:
            raise ValueError("Foundry Local: 'file' parameter is required.")
        response_format = kwargs.get("response_format") or "json"
        if response_format not in {"text", "json"}:
            raise ValueError(
                f"Foundry Local audio: response_format='{response_format}' "
                "not supported (use 'text' or 'json')."
            )

        audio_client = self._get_audio_client(model_alias)

        # settings
        try:
            from foundry_local_sdk.openai.audio_client import (  # type: ignore[import-not-found]
                AudioSettings,
            )
            audio_client.settings = AudioSettings()
        except ImportError:
            pass
        if kwargs.get("language") is not None:
            audio_client.settings.language = kwargs["language"]
        if kwargs.get("temperature") is not None:
            audio_client.settings.temperature = kwargs["temperature"]

        # Resolve `file` to a filesystem path. If the caller handed us a
        # binary stream (the OpenAI-canonical form), spill to a temp file
        # the SDK can read.
        cleanup_path: Optional[str] = None
        try:
            if isinstance(file_arg, (str, os.PathLike)):
                audio_path = str(file_arg)
            elif hasattr(file_arg, "read"):
                data = file_arg.read()
                if not isinstance(data, (bytes, bytearray)):
                    raise ValueError(
                        "Foundry Local audio: 'file' stream must be opened in "
                        "binary mode."
                    )
                # Try to preserve the original suffix so the native decoder
                # can detect the format.
                name = getattr(file_arg, "name", None)
                suffix = Path(name).suffix if isinstance(name, str) else ""
                fd, audio_path = tempfile.mkstemp(suffix=suffix or ".bin")
                cleanup_path = audio_path
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
            else:
                raise ValueError(
                    "Foundry Local audio: 'file' must be a path or a binary "
                    f"file-like object, got {type(file_arg).__name__}."
                )

            result = audio_client.transcribe(audio_path)
        finally:
            if cleanup_path:
                try:
                    os.unlink(cleanup_path)
                except OSError:
                    pass

        if response_format == "text":
            return getattr(result, "text", "")
        return result


class _FoundryChatCompletions:
    def __init__(self, client: "FoundryLocalClient") -> None:
        self._client = client

    def create(self, **kwargs: Any) -> Any:
        return self._client._create_chat_completion(**kwargs)


class _FoundryChatNamespace:
    def __init__(self, client: "FoundryLocalClient") -> None:
        self.completions = _FoundryChatCompletions(client)


class _FoundryEmbeddings:
    def __init__(self, client: "FoundryLocalClient") -> None:
        self._client = client

    def create(self, **kwargs: Any) -> Any:
        return self._client._create_embedding(**kwargs)


class _FoundryAudioTranscriptions:
    def __init__(self, client: "FoundryLocalClient") -> None:
        self._client = client

    def create(self, **kwargs: Any) -> Any:
        return self._client._create_transcription(**kwargs)


class _FoundryAudioNamespace:
    def __init__(self, client: "FoundryLocalClient") -> None:
        self.transcriptions = _FoundryAudioTranscriptions(client)


# ── Async wrapper ───────────────────────────────────────────────────────


def _run_in_executor(fn):
    """Wrap a sync callable so awaiting the returned coroutine runs it in
    the default executor."""
    async def _wrapper(*args: Any, **kwargs: Any) -> Any:
        import asyncio

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: fn(*args, **kwargs))

    return _wrapper


class _AsyncFoundryChatCompletions:
    def __init__(self, sync_client: "FoundryLocalClient") -> None:
        self._sync_client = sync_client

    async def create(self, **kwargs: Any) -> Any:
        import asyncio

        loop = asyncio.get_running_loop()
        # Streaming and non-streaming both go through the executor; for
        # stream=True the caller receives the SDK's sync generator and must
        # iterate it with a regular `for`, not `async for`.
        return await loop.run_in_executor(
            None, lambda: self._sync_client._create_chat_completion(**kwargs)
        )


class _AsyncFoundryChatNamespace:
    def __init__(self, sync_client: "FoundryLocalClient") -> None:
        self.completions = _AsyncFoundryChatCompletions(sync_client)


class _AsyncFoundryEmbeddings:
    def __init__(self, sync_client: "FoundryLocalClient") -> None:
        self._sync_client = sync_client

    async def create(self, **kwargs: Any) -> Any:
        import asyncio

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: self._sync_client._create_embedding(**kwargs)
        )


class _AsyncFoundryAudioTranscriptions:
    def __init__(self, sync_client: "FoundryLocalClient") -> None:
        self._sync_client = sync_client

    async def create(self, **kwargs: Any) -> Any:
        import asyncio

        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, lambda: self._sync_client._create_transcription(**kwargs)
        )


class _AsyncFoundryAudioNamespace:
    def __init__(self, sync_client: "FoundryLocalClient") -> None:
        self.transcriptions = _AsyncFoundryAudioTranscriptions(sync_client)


class AsyncFoundryLocalClient:
    """Async wrapper used by ``auxiliary_client._to_async_client``.

    Defers all work to the underlying sync ``FoundryLocalClient`` running in
    the default executor. The Foundry Local SDK has no native async surface;
    the executor hop keeps the asyncio loop responsive.
    """

    def __init__(self, sync_client: FoundryLocalClient) -> None:
        self._sync = sync_client
        self.api_key = sync_client.api_key
        self.base_url = sync_client.base_url
        self.chat = _AsyncFoundryChatNamespace(sync_client)
        self.embeddings = _AsyncFoundryEmbeddings(sync_client)
        self.audio = _AsyncFoundryAudioNamespace(sync_client)

    async def close(self) -> None:
        import asyncio

        await asyncio.get_running_loop().run_in_executor(None, self._sync.close)
