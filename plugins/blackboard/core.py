"""BlackboardCore — orchestrates cache + backend for all tool operations."""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class BlackboardCore:
    """Single coordinator used by all 7 tool handlers.

    ``cache`` — BlackboardCache (SQLite, always written first)
    ``backend`` — BlackboardBackend (LocalBackend or ZenohBackend)
    ``namespace`` — Zenoh-style key prefix (e.g. "hermes/blackboard")
    """

    def __init__(self, cache, backend, namespace: str) -> None:
        self._cache = cache
        self._backend = backend
        self._namespace = namespace

    # ------------------------------------------------------------------
    # Topics
    # ------------------------------------------------------------------

    def create_topic(
        self, slug: str, name: str, description: str, created_by: str
    ) -> Dict[str, Any]:
        topic = {
            "slug": slug,
            "topic_name": name,
            "name": name,
            "description": description,
            "created_by": created_by,
            "created_at": _now_iso(),
        }
        self._cache.store_topic(topic)
        self._backend.publish_topic(topic)
        return topic

    def list_topics(self, timeout: float = 1.5) -> List[Dict[str, Any]]:
        try:
            fresh = self._backend.query_history(
                f"{self._namespace}/index", timeout=timeout
            )
            for t in fresh:
                if t.get("slug"):
                    self._cache.store_topic(t)
        except Exception as exc:
            logger.debug("list_topics: peer query failed (using cache): %s", exc)
        return self._cache.get_topics()

    def get_topic(
        self,
        slug: str,
        since: Optional[str] = None,
        limit: int = 50,
        timeout: float = 2.0,
    ) -> Optional[Dict[str, Any]]:
        try:
            fresh = self._backend.query_history(
                f"{self._namespace}/entries/{slug}/**", timeout=timeout
            )
            for e in fresh:
                if e.get("id"):
                    self._cache.store_entry(slug, e)
        except Exception as exc:
            logger.debug("get_topic: peer query failed (using cache): %s", exc)

        topic = self._cache.get_topic(slug)
        if not topic:
            return None
        return {
            "topic": topic,
            "entries": self._cache.get_entries(slug, since=since, limit=limit),
            "metadata": self._cache.get_metadata(slug),
        }

    # ------------------------------------------------------------------
    # Entries
    # ------------------------------------------------------------------

    def write_entry(
        self, slug: str, content: str, author: str, role: str = "contributor"
    ) -> Dict[str, Any]:
        entry_id = f"{int(time.time() * 1000)}-{abs(hash(content + author)) % 0xFFFF:04x}"
        entry = {
            "id": entry_id,
            "content": content,
            "author": author,
            "role": role,
            "timestamp": _now_iso(),
        }
        self._cache.store_entry(slug, entry)
        self._backend.publish_entry(slug, entry)
        return entry

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    def update_metadata(
        self, slug: str, key: str, value: Any, updated_by: str
    ) -> Dict[str, Any]:
        payload = {
            "value": value,
            "updated_by": updated_by,
            "timestamp": _now_iso(),
        }
        self._cache.store_metadata(slug, key, payload)
        self._backend.publish_metadata(slug, key, payload)
        return payload

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._backend.close()
