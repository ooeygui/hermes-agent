"""Zenoh session wrapper — pub/sub/queryable for the shared blackboard."""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ZenohBlackboard:
    """Manages a Zenoh peer session for the shared blackboard.

    The caller must call ``open()`` before any pub/sub operation and
    ``close()`` when finished.  ``ensure_open()`` opens on first use.

    A background thread runs the queryable loop, serving history from the
    local SQLite cache to any peer that asks.
    """

    def __init__(
        self,
        namespace: str = "hermes/blackboard",
        mode: str = "peer",
        connect_endpoints: Optional[List[str]] = None,
        cache=None,  # BlackboardCache instance, injected
    ) -> None:
        self._namespace = namespace
        self._mode = mode
        self._connect_endpoints = connect_endpoints or []
        self._cache = cache

        self._session = None
        self._publishers: Dict[str, Any] = {}  # slug → zenoh.Publisher
        self._subscribers: Dict[str, Any] = {}  # key_expr → zenoh.Subscriber
        self._queryable = None
        self._query_thread: Optional[threading.Thread] = None
        self._shutdown_event = threading.Event()
        self._open_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Return True when the eclipse-zenoh package is importable."""
        try:
            import zenoh  # noqa: F401
            return True
        except ImportError:
            return False

    def ensure_open(self) -> None:
        """Open the Zenoh session if not already open (lazy init)."""
        with self._open_lock:
            if self._session is not None:
                return
            self._open()

    def _open(self) -> None:
        import zenoh

        conf = zenoh.Config()
        conf.insert_json5("mode", f'"{self._mode}"')
        if self._connect_endpoints:
            endpoints_json = json.dumps(self._connect_endpoints)
            conf.insert_json5("connect/endpoints", endpoints_json)

        self._session = zenoh.open(conf)
        self._shutdown_event.clear()

        # Serve history queries from cache
        queryable_key = f"{self._namespace}/**"
        self._queryable = self._session.declare_queryable(queryable_key, complete=False)
        self._query_thread = threading.Thread(
            target=self._serve_queries, name="bb-queryable", daemon=True
        )
        self._query_thread.start()
        logger.info("ZenohBlackboard opened (mode=%s)", self._mode)

    def close(self) -> None:
        """Shut down publishers, subscribers, queryable, and the session."""
        self._shutdown_event.set()

        # Undeclare publishers
        for slug, pub in list(self._publishers.items()):
            try:
                pub.undeclare()
            except Exception:
                pass
        self._publishers.clear()

        # Undeclare subscribers
        for key_expr, sub in list(self._subscribers.items()):
            try:
                sub.undeclare()
            except Exception:
                pass
        self._subscribers.clear()

        # Undeclare queryable (unblocks the recv() loop in background thread)
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

        logger.info("ZenohBlackboard closed")

    # ------------------------------------------------------------------
    # Queryable background thread
    # ------------------------------------------------------------------

    def _serve_queries(self) -> None:
        """Background thread: serve history queries from the local cache."""
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
                        matches = self._cache.get_matching_for_query(
                            selector, self._namespace
                        )
                        for key_expr, payload_bytes in matches:
                            query.reply(key_expr, payload_bytes)
                    else:
                        query.reply(str(query.key_expr), b"{}")
            except Exception as exc:
                if self._shutdown_event.is_set():
                    break
                logger.debug("Queryable recv error (will retry): %s", exc)
                time.sleep(0.05)

    # ------------------------------------------------------------------
    # Publisher helpers
    # ------------------------------------------------------------------

    def _get_publisher(self, slug: str):
        """Return (or lazily declare) a publisher for a topic slug."""
        if slug not in self._publishers:
            key_expr = f"{self._namespace}/entries/{slug}/**"
            self._publishers[slug] = self._session.declare_publisher(
                f"{self._namespace}/entries/{slug}"
            )
        return self._publishers[slug]

    # ------------------------------------------------------------------
    # Topic operations
    # ------------------------------------------------------------------

    def create_topic(
        self,
        slug: str,
        name: str,
        description: str,
        created_by: str,
    ) -> Dict[str, Any]:
        """Publish an index announcement and persist to cache."""
        self.ensure_open()
        payload = {
            "topic_name": name,
            "slug": slug,
            "description": description,
            "created_by": created_by,
            "created_at": _now_iso(),
        }
        index_pub = self._session.declare_publisher(f"{self._namespace}/index")
        index_pub.put(json.dumps(payload).encode())
        index_pub.undeclare()

        if self._cache is not None:
            self._cache.store_topic(payload)
        return payload

    # ------------------------------------------------------------------
    # Entry operations
    # ------------------------------------------------------------------

    def write_entry(
        self,
        slug: str,
        content: str,
        author: str,
        role: str = "contributor",
    ) -> Dict[str, Any]:
        """Publish an entry and persist to cache."""
        self.ensure_open()
        import time as _time
        entry_id = f"{int(_time.time() * 1000)}-{abs(hash(content + author)) % 0xFFFF:04x}"
        payload = {
            "id": entry_id,
            "content": content,
            "author": author,
            "role": role,
            "timestamp": _now_iso(),
        }
        key = f"{self._namespace}/entries/{slug}/{entry_id}"
        pub = self._session.declare_publisher(key)
        pub.put(json.dumps(payload).encode())
        pub.undeclare()

        if self._cache is not None:
            self._cache.store_entry(slug, payload)
        return payload

    # ------------------------------------------------------------------
    # Metadata operations
    # ------------------------------------------------------------------

    def update_metadata(
        self,
        slug: str,
        key: str,
        value: Any,
        updated_by: str,
    ) -> Dict[str, Any]:
        """Publish a metadata KV update and persist to cache."""
        self.ensure_open()
        payload = {
            "value": value,
            "updated_by": updated_by,
            "timestamp": _now_iso(),
        }
        meta_key = f"{self._namespace}/meta/{slug}/{key}"
        pub = self._session.declare_publisher(meta_key)
        pub.put(json.dumps(payload).encode())
        pub.undeclare()

        if self._cache is not None:
            self._cache.store_metadata(slug, key, payload)
        return payload

    # ------------------------------------------------------------------
    # Subscriber helper (for watch tool)
    # ------------------------------------------------------------------

    def subscribe(
        self,
        key_expr: str,
        callback: Callable,
    ) -> Any:
        """Declare a subscriber and return it. Keeps a reference to undeclare later."""
        self.ensure_open()
        sub = self._session.declare_subscriber(key_expr, callback)
        self._subscribers[key_expr] = sub
        return sub

    def unsubscribe(self, key_expr: str) -> None:
        sub = self._subscribers.pop(key_expr, None)
        if sub is not None:
            try:
                sub.undeclare()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # History query
    # ------------------------------------------------------------------

    def query_history(
        self,
        selector: str,
        timeout: float = 2.0,
    ) -> List[Dict[str, Any]]:
        """Query peers for historic data via Zenoh get."""
        self.ensure_open()
        results: List[Dict[str, Any]] = []
        seen_ids: set = set()

        replies = self._session.get(selector, timeout=timeout)
        for reply in replies:
            try:
                ok = reply.ok
                raw = json.loads(ok.payload.to_bytes())
                # Deduplicate by entry id when multiple peers respond
                entry_id = raw.get("id")
                if entry_id:
                    if entry_id not in seen_ids:
                        seen_ids.add(entry_id)
                        results.append(raw)
                else:
                    results.append(raw)
            except Exception as exc:
                # reply.err or parse failure
                logger.debug("Skipping query reply: %s", exc)

        return results
