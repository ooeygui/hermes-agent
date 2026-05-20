"""ZenohBackend — real-time P2P transport via eclipse-zenoh.

Activate by setting ``plugins.blackboard.backend: zenoh`` in config.yaml
and installing ``pip install eclipse-zenoh``.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from plugins.blackboard.backend import BlackboardBackend, DEFAULT_NAMESPACE

logger = logging.getLogger(__name__)


class ZenohBackend(BlackboardBackend):
    """Zenoh peer-to-peer backend.

    Opens a single Zenoh session per process, shared across all tool calls.
    A background thread serves history queries from the local SQLite cache
    so late-joining peers can catch up.
    """

    def __init__(
        self,
        mode: str = "peer",
        connect_endpoints: Optional[List[str]] = None,
        cache=None,  # BlackboardCache injected by BlackboardCore
    ) -> None:
        self._mode = mode
        self._connect_endpoints = connect_endpoints or []
        self._cache = cache
        self._namespace = DEFAULT_NAMESPACE

        self._session = None
        self._queryable = None
        self._query_thread: Optional[threading.Thread] = None
        self._shutdown_event = threading.Event()
        self._open_lock = threading.Lock()
        # key_pattern → list of zenoh.Subscriber handles
        self._subscribers: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @classmethod
    def is_available(cls) -> bool:
        try:
            import zenoh  # noqa: F401
            return True
        except ImportError:
            return False

    def open(self, namespace: str) -> None:
        self._namespace = namespace
        with self._open_lock:
            if self._session is not None:
                return
            self._start_session()

    def _start_session(self) -> None:
        import zenoh

        conf = zenoh.Config()
        conf.insert_json5("mode", f'"{self._mode}"')
        if self._connect_endpoints:
            conf.insert_json5("connect/endpoints", json.dumps(self._connect_endpoints))

        self._session = zenoh.open(conf)
        self._shutdown_event.clear()

        self._queryable = self._session.declare_queryable(
            f"{self._namespace}/**", complete=False
        )
        self._query_thread = threading.Thread(
            target=self._serve_queries, name="bb-zenoh-queryable", daemon=True
        )
        self._query_thread.start()
        logger.info("ZenohBackend opened (mode=%s, ns=%s)", self._mode, self._namespace)

    def close(self) -> None:
        self._shutdown_event.set()

        for key_expr, sub in list(self._subscribers.items()):
            try:
                sub.undeclare()
            except Exception:
                pass
        self._subscribers.clear()

        if self._queryable is not None:
            try:
                self._queryable.undeclare()
            except Exception:
                pass
            self._queryable = None

        if self._query_thread is not None:
            self._query_thread.join(timeout=3.0)
            self._query_thread = None

        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None

        logger.info("ZenohBackend closed")

    # ------------------------------------------------------------------
    # Queryable background thread — serves history from local SQLite cache
    # ------------------------------------------------------------------

    def _serve_queries(self) -> None:
        queryable = self._queryable
        if queryable is None:
            return
        while not self._shutdown_event.is_set():
            try:
                with queryable.recv() as query:
                    if query is None:
                        break
                    selector = str(query.selector)
                    if self._cache is not None:
                        for key_expr, payload_bytes in self._cache.get_matching_for_query(
                            selector, self._namespace
                        ):
                            query.reply(key_expr, payload_bytes)
            except Exception as exc:
                if self._shutdown_event.is_set():
                    break
                logger.debug("ZenohBackend queryable recv error: %s", exc)
                time.sleep(0.05)

    # ------------------------------------------------------------------
    # Publish helpers
    # ------------------------------------------------------------------

    def _pub_once(self, key: str, payload: dict) -> None:
        """Declare a transient publisher, put once, undeclare."""
        pub = self._session.declare_publisher(key)
        pub.put(json.dumps(payload).encode())
        pub.undeclare()

    def publish_topic(self, topic: dict) -> None:
        self._pub_once(f"{self._namespace}/index", topic)

    def publish_entry(self, slug: str, entry: dict) -> None:
        key = f"{self._namespace}/entries/{slug}/{entry.get('id', 'unknown')}"
        self._pub_once(key, entry)

    def publish_metadata(self, slug: str, key: str, value: dict) -> None:
        self._pub_once(f"{self._namespace}/meta/{slug}/{key}", value)

    # ------------------------------------------------------------------
    # Subscribe / unsubscribe
    # ------------------------------------------------------------------

    def subscribe(self, key_pattern: str, callback: Callable) -> Any:
        sub = self._session.declare_subscriber(key_pattern, callback)
        self._subscribers[key_pattern] = sub
        return sub

    def unsubscribe(self, handle: Any) -> None:
        # handle is a zenoh.Subscriber — find and undeclare it
        for key_pattern, sub in list(self._subscribers.items()):
            if sub is handle:
                try:
                    sub.undeclare()
                except Exception:
                    pass
                self._subscribers.pop(key_pattern, None)
                return

    # ------------------------------------------------------------------
    # History query
    # ------------------------------------------------------------------

    def query_history(self, selector: str, timeout: float) -> List[dict]:
        results: List[dict] = []
        seen_ids: set = set()
        for reply in self._session.get(selector, timeout=timeout):
            try:
                raw = json.loads(reply.ok.payload.to_bytes())
                entry_id = raw.get("id")
                if entry_id:
                    if entry_id not in seen_ids:
                        seen_ids.add(entry_id)
                        results.append(raw)
                else:
                    results.append(raw)
            except Exception as exc:
                logger.debug("ZenohBackend: skipping reply: %s", exc)
        return results
