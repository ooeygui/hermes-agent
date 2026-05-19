"""Subscription watcher and notification queue for the Zenoh blackboard."""

from __future__ import annotations

import json
import logging
import queue
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)

# Maximum notifications buffered per session before old ones are dropped.
_MAX_QUEUE = 100


@dataclass
class Notification:
    topic_slug: str
    topic_name: str
    entry: Dict[str, Any]


class NotificationQueue:
    """Per-session notification buffer backed by queue.Queue."""

    def __init__(self, maxsize: int = _MAX_QUEUE) -> None:
        self._q: queue.Queue = queue.Queue(maxsize=maxsize)

    def put(self, notif: Notification) -> None:
        try:
            self._q.put_nowait(notif)
        except queue.Full:
            # Drop oldest, make room
            try:
                self._q.get_nowait()
            except queue.Empty:
                pass
            try:
                self._q.put_nowait(notif)
            except queue.Full:
                pass

    def drain(self, limit: int = 20) -> list:
        items = []
        while len(items) < limit:
            try:
                items.append(self._q.get_nowait())
            except queue.Empty:
                break
        return items

    def empty(self) -> bool:
        return self._q.empty()


class BlackboardWatcher:
    """Manages per-agent subscriptions and routes arrivals to session queues.

    A single instance is shared across all Hermes sessions within the process.
    Notifications are routed to the correct session queue by session_id.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # session_id → {slug → subscriber_handle}
        self._subscriptions: Dict[str, Dict[str, Any]] = {}
        # session_id → NotificationQueue
        self._queues: Dict[str, NotificationQueue] = {}
        # slug → topic_name (for display)
        self._topic_names: Dict[str, str] = {}

    def register_session(self, session_id: str) -> None:
        with self._lock:
            if session_id not in self._queues:
                self._queues[session_id] = NotificationQueue()
                self._subscriptions[session_id] = {}

    def deregister_session(self, session_id: str, blackboard=None) -> None:
        with self._lock:
            subs = self._subscriptions.pop(session_id, {})
            self._queues.pop(session_id, None)
        if blackboard is not None:
            for key_expr in subs:
                blackboard.unsubscribe(key_expr)

    def watch_topic(
        self,
        session_id: str,
        slug: str,
        topic_name: str,
        blackboard,
    ) -> bool:
        """Subscribe this session to a topic's entry stream.

        Returns True if a new subscription was created, False if already watching.
        """
        self.register_session(session_id)
        with self._lock:
            subs = self._subscriptions[session_id]
            if slug in subs:
                return False  # already watching
            self._topic_names[slug] = topic_name

        key_expr = f"{blackboard._namespace}/entries/{slug}/**"
        callback = self._make_callback(session_id, slug)
        sub = blackboard.subscribe(key_expr, callback)

        with self._lock:
            self._subscriptions[session_id][slug] = sub
        return True

    def _make_callback(self, session_id: str, slug: str) -> Callable:
        def _on_sample(sample) -> None:
            try:
                entry = json.loads(sample.payload.to_bytes())
            except Exception:
                return
            topic_name = self._topic_names.get(slug, slug)
            notif = Notification(
                topic_slug=slug, topic_name=topic_name, entry=entry
            )
            with self._lock:
                q = self._queues.get(session_id)
            if q is not None:
                q.put(notif)

        return _on_sample

    def poll(self, session_id: str, limit: int = 10) -> list:
        """Drain and return up to *limit* pending notifications for a session."""
        with self._lock:
            q = self._queues.get(session_id)
        if q is None:
            return []
        return q.drain(limit=limit)

    def format_notifications(self, notifications: list) -> str:
        """Format a list of Notification objects as a readable summary."""
        if not notifications:
            return "No new notifications."
        lines = [f"📋 {len(notifications)} blackboard update(s):"]
        for n in notifications:
            content_preview = n.entry.get("content", "")[:120]
            author = n.entry.get("author", "unknown")
            lines.append(
                f'  🗂 Topic "{n.topic_name}" — new entry from {author}:\n'
                f"    {content_preview!r}"
            )
        lines.append('→ Use blackboard_get_topic("<slug>") to read in full.')
        return "\n".join(lines)
