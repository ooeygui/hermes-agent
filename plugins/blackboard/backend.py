"""BlackboardBackend ABC and LocalBackend (in-process, SQLite-only).

The backend interface abstracts the *transport* layer.
The SQLite cache is always written by BlackboardCore regardless of backend;
backends only add network pub/sub and history-query on top.
"""

from __future__ import annotations

import json
import logging
import threading
from abc import ABC, abstractmethod
from typing import Any, Callable, List

logger = logging.getLogger(__name__)

# Default Zenoh-style key namespace (overridden by config).
DEFAULT_NAMESPACE = "hermes/blackboard"


# ---------------------------------------------------------------------------
# Simple Zenoh-compatible key-expression matcher
# ---------------------------------------------------------------------------

def _key_matches(pattern: str, key: str) -> bool:
    """Return True when *key* matches a Zenoh-style *pattern*.

    Supports ``**`` (multi-segment wildcard) and ``*`` (single-segment wildcard).
    """
    if pattern == key:
        return True
    parts_p = pattern.split("/")
    parts_k = key.split("/")
    return _match_parts(parts_p, parts_k)


def _match_parts(pp: list, kp: list) -> bool:
    if not pp:
        return not kp
    if pp[0] == "**":
        # ** consumes zero or more segments
        for i in range(len(kp) + 1):
            if _match_parts(pp[1:], kp[i:]):
                return True
        return False
    if not kp:
        return False
    if pp[0] == "*" or pp[0] == kp[0]:
        return _match_parts(pp[1:], kp[1:])
    return False


# ---------------------------------------------------------------------------
# Lightweight sample object (mirrors the Zenoh Sample API used by watcher.py)
# ---------------------------------------------------------------------------

class _LocalSample:
    """Minimal Sample lookalike used to fire in-process subscriber callbacks."""

    def __init__(self, key_expr: str, payload_bytes: bytes) -> None:
        self.key_expr = key_expr
        self.payload = _LocalPayload(payload_bytes)


class _LocalPayload:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def to_bytes(self) -> bytes:
        return self._data

    def to_string(self) -> str:
        return self._data.decode()


# ---------------------------------------------------------------------------
# ABC
# ---------------------------------------------------------------------------

class BlackboardBackend(ABC):
    """Transport abstraction for the shared blackboard.

    All methods must be non-blocking or perform their I/O on a background
    thread.  The cache (SQLite) is written by BlackboardCore *before* calling
    any backend method; backends only add network distribution on top.
    """

    @abstractmethod
    def open(self, namespace: str) -> None:
        """Initialise the backend (lazy — called on first tool use)."""

    @abstractmethod
    def close(self) -> None:
        """Release all resources. Called on session end."""

    @abstractmethod
    def publish_topic(self, topic: dict) -> None:
        """Announce a new topic to peers."""

    @abstractmethod
    def publish_entry(self, slug: str, entry: dict) -> None:
        """Broadcast a new entry to peers and fire local subscribers."""

    @abstractmethod
    def publish_metadata(self, slug: str, key: str, value: dict) -> None:
        """Broadcast a metadata update."""

    @abstractmethod
    def subscribe(self, key_pattern: str, callback: Callable) -> Any:
        """Register *callback* for arrivals matching *key_pattern*.

        Returns an opaque handle that can be passed to ``unsubscribe``.
        For LocalBackend the handle is ``(key_pattern, callback)``.
        """

    @abstractmethod
    def unsubscribe(self, handle: Any) -> None:
        """Cancel a subscription by its handle."""

    @abstractmethod
    def query_history(self, selector: str, timeout: float) -> List[dict]:
        """Ask peers for historic records matching *selector*.

        Returns a list of raw dicts (topics or entries).
        LocalBackend always returns ``[]`` — history comes from SQLite.
        """


# ---------------------------------------------------------------------------
# LocalBackend
# ---------------------------------------------------------------------------

class LocalBackend(BlackboardBackend):
    """File-only backend — no network, no extra dependencies.

    - Pub/sub is in-process only: callbacks fire when entries are published
      within the same Python process (useful for delegate_task subagents
      sharing a process, or for tests).
    - ``query_history`` returns ``[]``: all history is read from SQLite.
    - For cross-process notifications or multi-machine sync, use ZenohBackend.
    """

    def __init__(self) -> None:
        self._namespace = DEFAULT_NAMESPACE
        self._lock = threading.Lock()
        # key_pattern → list of (id, callback) pairs
        self._subs: dict = {}

    def open(self, namespace: str) -> None:
        self._namespace = namespace

    def close(self) -> None:
        with self._lock:
            self._subs.clear()

    # ------------------------------------------------------------------
    # Publish — fire matching in-process subscribers asynchronously
    # ------------------------------------------------------------------

    def publish_topic(self, topic: dict) -> None:
        key = f"{self._namespace}/index"
        self._fire(key, topic)

    def publish_entry(self, slug: str, entry: dict) -> None:
        key = f"{self._namespace}/entries/{slug}/{entry.get('id', 'unknown')}"
        self._fire(key, entry)

    def publish_metadata(self, slug: str, key: str, value: dict) -> None:
        full_key = f"{self._namespace}/meta/{slug}/{key}"
        self._fire(full_key, value)

    def _fire(self, key: str, payload: dict) -> None:
        payload_bytes = json.dumps(payload).encode()
        with self._lock:
            targets = [
                cb
                for pattern, callbacks in self._subs.items()
                for cb in callbacks
                if _key_matches(pattern, key)
            ]
        if not targets:
            return
        sample = _LocalSample(key, payload_bytes)
        # Fire callbacks in a background thread to avoid blocking tool calls
        def _dispatch():
            for cb in targets:
                try:
                    cb(sample)
                except Exception as exc:
                    logger.debug("LocalBackend subscriber error: %s", exc)
        threading.Thread(target=_dispatch, name="bb-local-dispatch", daemon=True).start()

    # ------------------------------------------------------------------
    # Subscribe / unsubscribe
    # ------------------------------------------------------------------

    def subscribe(self, key_pattern: str, callback: Callable) -> Any:
        handle = (key_pattern, id(callback), callback)
        with self._lock:
            self._subs.setdefault(key_pattern, []).append(callback)
        return handle

    def unsubscribe(self, handle: Any) -> None:
        if not handle:
            return
        key_pattern, _, callback = handle
        with self._lock:
            callbacks = self._subs.get(key_pattern, [])
            try:
                callbacks.remove(callback)
            except ValueError:
                pass
            if not callbacks:
                self._subs.pop(key_pattern, None)

    # ------------------------------------------------------------------
    # History — delegates entirely to SQLite cache in BlackboardCore
    # ------------------------------------------------------------------

    def query_history(self, selector: str, timeout: float) -> List[dict]:
        return []
