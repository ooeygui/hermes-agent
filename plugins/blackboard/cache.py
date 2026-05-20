"""SQLite cache for the blackboard — durable cold storage + query replies."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class BlackboardCache:
    """Thread-safe SQLite cache for topics, entries, and metadata.

    Uses WAL journal mode so multiple readers don't block writers.
    All writes are serialised through a single lock.
    """

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._write_lock = threading.Lock()
        self._local = threading.local()  # per-thread read connections
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ------------------------------------------------------------------
    # Connection helpers
    # ------------------------------------------------------------------

    def _get_read_conn(self) -> sqlite3.Connection:
        """Return (or create) a per-thread read connection."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def _write_conn(self) -> sqlite3.Connection:
        """Return a dedicated write connection (always check_same_thread=False)."""
        if not hasattr(self, "_wconn") or self._wconn is None:
            self._wconn: sqlite3.Connection = sqlite3.connect(
                str(self._db_path), check_same_thread=False
            )
            self._wconn.row_factory = sqlite3.Row
            self._wconn.execute("PRAGMA journal_mode=WAL")
            self._wconn.execute("PRAGMA synchronous=NORMAL")
        return self._wconn

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        with self._write_lock:
            conn = self._write_conn()
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS topics (
                    slug        TEXT PRIMARY KEY,
                    name        TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    created_by  TEXT DEFAULT 'unknown',
                    created_at  TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS entries (
                    id          TEXT PRIMARY KEY,
                    slug        TEXT NOT NULL,
                    content     TEXT NOT NULL,
                    author      TEXT DEFAULT 'unknown',
                    role        TEXT DEFAULT 'contributor',
                    timestamp   TEXT NOT NULL,
                    FOREIGN KEY (slug) REFERENCES topics(slug) ON DELETE CASCADE
                );

                CREATE INDEX IF NOT EXISTS idx_entries_slug
                    ON entries (slug, timestamp);

                CREATE TABLE IF NOT EXISTS metadata (
                    slug        TEXT NOT NULL,
                    key         TEXT NOT NULL,
                    value       TEXT NOT NULL,
                    updated_by  TEXT DEFAULT 'unknown',
                    timestamp   TEXT NOT NULL,
                    PRIMARY KEY (slug, key)
                );
                """
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Topics
    # ------------------------------------------------------------------

    def store_topic(self, topic: Dict[str, Any]) -> None:
        with self._write_lock:
            conn = self._write_conn()
            conn.execute(
                """
                INSERT OR REPLACE INTO topics (slug, name, description, created_by, created_at)
                VALUES (:slug, :name, :description, :created_by, :created_at)
                """,
                {
                    "slug": topic["slug"],
                    "name": topic.get("topic_name", topic.get("name", topic["slug"])),
                    "description": topic.get("description", ""),
                    "created_by": topic.get("created_by", "unknown"),
                    "created_at": topic.get(
                        "created_at", datetime.now(timezone.utc).isoformat()
                    ),
                },
            )
            conn.commit()

    def get_topics(self) -> List[Dict[str, Any]]:
        conn = self._get_read_conn()
        rows = conn.execute(
            "SELECT slug, name, description, created_by, created_at FROM topics ORDER BY created_at"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_topic(self, slug: str) -> Optional[Dict[str, Any]]:
        conn = self._get_read_conn()
        row = conn.execute(
            "SELECT slug, name, description, created_by, created_at FROM topics WHERE slug = ?",
            (slug,),
        ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Entries
    # ------------------------------------------------------------------

    def store_entry(self, slug: str, entry: Dict[str, Any]) -> None:
        with self._write_lock:
            conn = self._write_conn()
            conn.execute(
                """
                INSERT OR REPLACE INTO entries (id, slug, content, author, role, timestamp)
                VALUES (:id, :slug, :content, :author, :role, :timestamp)
                """,
                {
                    "id": entry["id"],
                    "slug": slug,
                    "content": entry.get("content", ""),
                    "author": entry.get("author", "unknown"),
                    "role": entry.get("role", "contributor"),
                    "timestamp": entry.get(
                        "timestamp", datetime.now(timezone.utc).isoformat()
                    ),
                },
            )
            conn.commit()

    def get_entries(
        self,
        slug: str,
        since: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        conn = self._get_read_conn()
        if since:
            rows = conn.execute(
                """
                SELECT id, slug, content, author, role, timestamp
                FROM entries WHERE slug = ? AND timestamp > ?
                ORDER BY timestamp LIMIT ?
                """,
                (slug, since, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, slug, content, author, role, timestamp
                FROM entries WHERE slug = ?
                ORDER BY timestamp LIMIT ?
                """,
                (slug, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    def store_metadata(self, slug: str, key: str, value: Dict[str, Any]) -> None:
        with self._write_lock:
            conn = self._write_conn()
            conn.execute(
                """
                INSERT OR REPLACE INTO metadata (slug, key, value, updated_by, timestamp)
                VALUES (:slug, :key, :value, :updated_by, :timestamp)
                """,
                {
                    "slug": slug,
                    "key": key,
                    "value": json.dumps(value.get("value", value)),
                    "updated_by": value.get("updated_by", "unknown"),
                    "timestamp": value.get(
                        "timestamp", datetime.now(timezone.utc).isoformat()
                    ),
                },
            )
            conn.commit()

    def get_metadata(self, slug: str) -> Dict[str, Any]:
        conn = self._get_read_conn()
        rows = conn.execute(
            "SELECT key, value, updated_by, timestamp FROM metadata WHERE slug = ?",
            (slug,),
        ).fetchall()
        result = {}
        for row in rows:
            try:
                result[row["key"]] = json.loads(row["value"])
            except (json.JSONDecodeError, TypeError):
                result[row["key"]] = row["value"]
        return result

    # ------------------------------------------------------------------
    # Queryable helper — called from ZenohBlackboard background thread
    # ------------------------------------------------------------------

    def get_matching_for_query(
        self, selector: str, namespace: str
    ) -> List[Tuple[str, bytes]]:
        """Return (key_expr, payload_bytes) pairs matching a Zenoh selector.

        Used by the queryable background thread to serve history requests.
        """
        results: List[Tuple[str, bytes]] = []
        # Strip Zenoh query parameters (e.g. "?key=value")
        path_part = selector.split("?")[0].rstrip("/")

        index_key = f"{namespace}/index"
        entries_prefix = f"{namespace}/entries/"
        meta_prefix = f"{namespace}/meta/"
        wildcard_all = f"{namespace}/**"
        wildcard_entries = f"{namespace}/entries/**"
        wildcard_meta = f"{namespace}/meta/**"

        wants_index = path_part in (index_key, wildcard_all)
        wants_entries = path_part in (wildcard_all, wildcard_entries) or path_part.startswith(entries_prefix)
        wants_meta = path_part in (wildcard_all, wildcard_meta) or path_part.startswith(meta_prefix)

        if wants_index:
            for topic in self.get_topics():
                results.append((index_key, json.dumps(topic).encode()))

        if wants_entries:
            remainder = path_part[len(entries_prefix):] if path_part.startswith(entries_prefix) else "**"
            slug_filter = remainder.split("/")[0] if remainder not in ("**", "") else None
            topics_to_query = (
                [slug_filter]
                if slug_filter and slug_filter != "**"
                else [t["slug"] for t in self.get_topics()]
            )
            for slug in topics_to_query:
                for entry in self.get_entries(slug, limit=200):
                    key = f"{namespace}/entries/{slug}/{entry['id']}"
                    results.append((key, json.dumps(entry).encode()))

        if wants_meta:
            remainder = path_part[len(meta_prefix):] if path_part.startswith(meta_prefix) else "**"
            slug_filter = remainder.split("/")[0] if remainder not in ("**", "") else None
            topics_to_query = (
                [slug_filter]
                if slug_filter and slug_filter != "**"
                else [t["slug"] for t in self.get_topics()]
            )
            for slug in topics_to_query:
                meta = self.get_metadata(slug)
                for mk, mv in meta.items():
                    key = f"{namespace}/meta/{slug}/{mk}"
                    results.append((key, json.dumps({"value": mv, "topic_slug": slug}).encode()))

        return results
